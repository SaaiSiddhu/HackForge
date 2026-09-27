"""Judging router: private scoring, assignments, invitations, rubrics, and progress."""

from typing import Optional
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_account, validate_csrf
from app.database import get_db
from app.judging_services import (
    accept_judge_invitation,
    assign_judge_to_project,
    create_judge_invitation,
    create_or_update_event_rubric,
    get_account_judge_for_event,
    get_active_rubric,
    get_invitation_by_token,
    get_judge_assigned_projects,
    get_judge_scores_resource,
    get_organizer_judging_progress,
    get_project_for_judge_evaluation,
    is_account_event_organizer,
    revoke_judge_invitation,
    run_batch_assignment,
    save_judge_evaluation,
    unassign_judge_from_project,
)
from app.models import (
    Account,
    Event,
    Judge,
    JudgeAssignment,
    JudgeInvitation,
    Project,
    Review,
    Rubric,
    Track,
)
from app.services import ConcurrencyConflictError
from app.templating import get_request_csrf_token, templates

router = APIRouter(tags=["judging"])


def require_organizer(db: Session, event_id: str, account: Optional[Account]) -> Account:
    """Ensure the user is authenticated and authorized as organizer for event_id."""
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    if not is_account_event_organizer(db, event_id, account.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: You are not an organizer of event '{event_id}'.",
        )
    return account


# --- DOGFOOD Acceptance Checker & Private Scores Endpoint ---


@router.get("/events/{event_id}/judges/{judge_id}/scores", summary="Get judge scores resource")
def get_judge_scores_endpoint(
    event_id: str,
    judge_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Access judge score resource with strict backend isolation."""
    try:
        data = get_judge_scores_resource(db, event_id=event_id, judge_id=judge_id, acting_account=account)
        return JSONResponse(status_code=status.HTTP_200_OK, content=data)
    except PermissionError as pe:
        if not account:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": str(pe)})
        return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(pe)})
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))


# --- Judge Portal: Dashboard & Scoring ---


@router.get("/events/{event_id}/judge", summary="Judge assigned projects dashboard")
def judge_dashboard(
    event_id: str,
    request: Request,
    track: Optional[str] = None,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """View assigned projects for the authenticated judge."""
    if not account:
        return RedirectResponse(url=f"/login?next=/events/{event_id}/judge", status_code=status.HTTP_302_FOUND)

    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    judge = get_account_judge_for_event(db, event_id, account.id)
    if not judge:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: Account '{account.email}' is not a registered judge for event '{event.name}'.",
        )

    assigned_projects = get_judge_assigned_projects(
        db, event_id=event_id, judge=judge, track_filter=track, status_filter=status_filter
    )
    tracks = db.scalars(select(Track).where(Track.event_id == event_id)).all()

    return templates.TemplateResponse(
        request=request,
        name="judge_dashboard.html",
        context={
            "event": event,
            "judge": judge,
            "assigned_projects": assigned_projects,
            "tracks": tracks,
            "selected_track": track,
            "selected_status": status_filter,
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@router.get("/events/{event_id}/judge/projects/{project_id}", summary="Judge evaluation form alias")
@router.get("/events/{event_id}/judge/projects/{project_id}/review", summary="Judge evaluation form")
def judge_review_view(
    event_id: str,
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Display evaluation form with project details, active rubric, and existing scores."""
    if not account:
        return RedirectResponse(
            url=f"/login?next=/events/{event_id}/judge/projects/{project_id}/review",
            status_code=status.HTTP_302_FOUND,
        )

    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    judge = get_account_judge_for_event(db, event_id, account.id)
    if not judge:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Account is not a registered judge for this event.",
        )

    try:
        project, review, rubric = get_project_for_judge_evaluation(db, event_id, judge, project_id)
    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))

    return templates.TemplateResponse(
        request=request,
        name="judge_review.html",
        context={
            "event": event,
            "judge": judge,
            "project": project,
            "review": review,
            "rubric": rubric,
            "existing_scores": review.criteria if review else {},
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@router.post("/events/{event_id}/judge/projects/{project_id}/review", summary="Submit evaluation or save draft")
async def judge_review_submit(
    event_id: str,
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Save draft or finalize evaluation with OCC concurrency and rubric bounds validation."""
    if not account:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    judge = get_account_judge_for_event(db, event_id, account.id)
    if not judge:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: Not a registered judge.")

    # Check whether request is JSON or multipart/form
    content_type = request.headers.get("content-type", "").lower()
    is_json = "application/json" in content_type

    form_rubric_id = None
    if is_json:
        payload = await request.json()
        action = str(payload.get("action", "finalize")).lower()
        comment = str(payload.get("comment", "")).strip()
        criteria_inputs = payload.get("criteria", {})
        expected_ver_raw = payload.get("expected_version") or payload.get("version")
        form_rubric_id = payload.get("rubric_id")
    else:
        form = await request.form()
        csrf_token = form.get("csrf_token")
        validate_csrf(request, csrf_token_form=csrf_token)

        action = str(form.get("action", "finalize")).lower()
        comment = str(form.get("comment", "")).strip()
        expected_ver_raw = form.get("expected_version") or form.get("version")
        form_rubric_id = form.get("rubric_id")

        # Collect criteria inputs prefixed with 'criterion_'
        criteria_inputs = {}
        for k, v in form.items():
            if k.startswith("criterion_"):
                ckey = k[len("criterion_"):]
                criteria_inputs[ckey] = v

    expected_version = None
    if expected_ver_raw is not None and str(expected_ver_raw).isdigit():
        expected_version = int(expected_ver_raw)

    is_final = action == "finalize"

    try:
        review = save_judge_evaluation(
            db=db,
            event_id=event_id,
            judge=judge,
            project_id=project_id,
            criteria_inputs=criteria_inputs,
            comment=comment,
            is_final=is_final,
            expected_version=expected_version,
            actor=account,
            form_rubric_id=form_rubric_id,
        )

        if is_json:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "ok",
                    "review_status": review.status,
                    "raw_score": review.raw_score,
                    "version": review.version,
                },
            )
        return RedirectResponse(
            url=f"/events/{event_id}/judge",
            status_code=status.HTTP_302_FOUND,
        )

    except ConcurrencyConflictError as cce:
        if is_json:
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(cce)})
        event = db.get(Event, event_id)
        project = db.get(Project, project_id)
        curr_review = db.scalars(
            select(Review).where(Review.judge_id == judge.id, Review.project_id == project_id)
        ).first()
        if curr_review and curr_review.rubric_id:
            rubric = db.get(Rubric, curr_review.rubric_id) or get_active_rubric(db, event_id)
        else:
            rubric = get_active_rubric(db, event_id)
        return templates.TemplateResponse(
            request=request,
            name="judge_review.html",
            context={
                "event": event,
                "judge": judge,
                "project": project,
                "review": curr_review,
                "rubric": rubric,
                "existing_scores": criteria_inputs,
                "error_msg": str(cce),
                "current_account": account,
                "csrf_token": get_request_csrf_token(request),
            },
            status_code=status.HTTP_409_CONFLICT,
        )

    except PermissionError as pe:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(pe))

    except ValueError as ve:
        if is_json:
            return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(ve)})
        event = db.get(Event, event_id)
        project = db.get(Project, project_id)
        curr_review = db.scalars(
            select(Review).where(Review.judge_id == judge.id, Review.project_id == project_id)
        ).first()
        if curr_review and curr_review.rubric_id:
            rubric = db.get(Rubric, curr_review.rubric_id) or get_active_rubric(db, event_id)
        else:
            rubric = get_active_rubric(db, event_id)
        return templates.TemplateResponse(
            request=request,
            name="judge_review.html",
            context={
                "event": event,
                "judge": judge,
                "project": project,
                "review": curr_review,
                "rubric": rubric,
                "existing_scores": criteria_inputs,
                "error_msg": str(ve),
                "current_account": account,
                "csrf_token": get_request_csrf_token(request),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# --- Judge Invitation Onboarding ---


@router.get("/events/{event_id}/judges/invite/{token}", summary="Judge invitation acceptance page")
def judge_invite_view(
    event_id: str,
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """View invitation details without mutating state."""
    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    invitation = get_invitation_by_token(db, token)
    if not invitation or invitation.event_id != event_id:
        return templates.TemplateResponse(
            request=request,
            name="judge_invite_accept.html",
            context={
                "event": event,
                "invitation": None,
                "error_msg": "Invalid or expired invitation link.",
                "current_account": account,
                "csrf_token": get_request_csrf_token(request),
            },
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Check track names
    track_objs = db.scalars(select(Track).where(Track.id.in_(invitation.tracks))).all()

    return templates.TemplateResponse(
        request=request,
        name="judge_invite_accept.html",
        context={
            "event": event,
            "invitation": invitation,
            "token": token,
            "tracks": track_objs,
            "current_account": account,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@router.post("/events/{event_id}/judges/invite/{token}/accept", summary="Accept judge invitation")
async def judge_invite_accept(
    event_id: str,
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Explicitly accept judge invitation with recipient email validation."""
    if not account:
        return RedirectResponse(
            url=f"/login?next=/events/{event_id}/judges/invite/{token}",
            status_code=status.HTTP_302_FOUND,
        )

    form = await request.form()
    csrf_token = form.get("csrf_token")
    validate_csrf(request, csrf_token_form=csrf_token)

    try:
        accept_judge_invitation(db, raw_token=token, account=account)
        return RedirectResponse(url=f"/events/{event_id}/judge", status_code=status.HTTP_302_FOUND)
    except ValueError as ve:
        event = db.get(Event, event_id)
        invitation = get_invitation_by_token(db, token)
        track_objs = db.scalars(select(Track).where(Track.id.in_(invitation.tracks))).all() if invitation else []
        return templates.TemplateResponse(
            request=request,
            name="judge_invite_accept.html",
            context={
                "event": event,
                "invitation": invitation,
                "token": token,
                "tracks": track_objs,
                "error_msg": str(ve),
                "current_account": account,
                "csrf_token": get_request_csrf_token(request),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


# --- Organizer Administration Endpoints ---


@router.post("/events/{event_id}/manage/judges/invite", summary="Organizer invites judge")
async def organizer_invite_judge(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Organizer invites a judge with selected track permissions."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        email = str(payload.get("email", "")).strip()
        tracks = payload.get("tracks", [])
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        email = str(form.get("email", "")).strip()
        tracks = form.getlist("tracks")

    try:
        invitation, raw_token = create_judge_invitation(
            db=db,
            event_id=event_id,
            email=email,
            tracks=tracks,
            invited_by=account,
        )
        invite_url = f"/events/{event_id}/judges/invite/{raw_token}"
        if "application/json" in content_type:
            return JSONResponse(
                status_code=status.HTTP_201_CREATED,
                content={
                    "invitation_id": invitation.id,
                    "email": invitation.email,
                    "invite_url": invite_url,
                },
            )
        return RedirectResponse(
            url=f"/events/{event_id}/manage?tab=judges&invited={raw_token}",
            status_code=status.HTTP_302_FOUND,
        )
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/events/{event_id}/manage/judges/invitations/{inv_id}/revoke", summary="Organizer revokes judge invite")
async def organizer_revoke_judge_invite(
    event_id: str,
    inv_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Organizer revokes a pending judge invitation."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))

    try:
        revoke_judge_invitation(db, event_id=event_id, invitation_id=inv_id, actor=account)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "revoked"})
        return RedirectResponse(url=f"/events/{event_id}/manage?tab=judges", status_code=status.HTTP_302_FOUND)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/events/{event_id}/manage/assignments", summary="Organizer manually assigns judge")
async def organizer_assign_judge(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Manually assign a judge to a project."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        judge_id = str(payload.get("judge_id", "")).strip()
        project_id = str(payload.get("project_id", "")).strip()
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        judge_id = str(form.get("judge_id", "")).strip()
        project_id = str(form.get("project_id", "")).strip()

    try:
        asgn = assign_judge_to_project(db, event_id=event_id, judge_id=judge_id, project_id=project_id, actor=account)
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_201_CREATED, content={"assignment_id": asgn.id})
        return RedirectResponse(url=f"/events/{event_id}/manage?tab=assignments", status_code=status.HTTP_302_FOUND)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.post("/events/{event_id}/manage/assignments/delete", summary="Organizer unassigns judge")
async def organizer_unassign_judge(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Remove a judge assignment."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        judge_id = str(payload.get("judge_id", "")).strip()
        project_id = str(payload.get("project_id", "")).strip()
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        judge_id = str(form.get("judge_id", "")).strip()
        project_id = str(form.get("project_id", "")).strip()

    unassign_judge_from_project(db, event_id=event_id, judge_id=judge_id, project_id=project_id, actor=account)
    if "application/json" in content_type:
        return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "unassigned"})
    return RedirectResponse(url=f"/events/{event_id}/manage?tab=assignments", status_code=status.HTTP_302_FOUND)


@router.post("/events/{event_id}/manage/assignments/batch", summary="Batch allocate judges to projects")
async def organizer_batch_assignments(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Run deterministic batch assignment algorithm balancing workload and checking eligibility."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        target_reviews = int(payload.get("target_reviews_per_project", 3))
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        target_reviews = int(form.get("target_reviews_per_project", 3))

    try:
        result = run_batch_assignment(
            db=db,
            event_id=event_id,
            target_reviews_per_project=target_reviews,
            actor=account,
        )
        if "application/json" in content_type:
            return JSONResponse(status_code=status.HTTP_200_OK, content=result)
        return RedirectResponse(url=f"/events/{event_id}/manage?tab=assignments", status_code=status.HTTP_302_FOUND)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))


@router.get("/events/{event_id}/manage/progress", summary="Organizer judging progress")
def organizer_progress_view(
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Retrieve event judging progress and coverage metrics."""
    require_organizer(db, event_id, account)
    if account and get_account_judge_for_event(db, event_id, account.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dual-role restriction: Organizers who are active judges in this event cannot view judging progress or aggregates.",
        )
    progress_data = get_organizer_judging_progress(db, event_id=event_id)
    return JSONResponse(status_code=status.HTTP_200_OK, content=progress_data)


@router.get("/events/{event_id}/manage/rubric", summary="Get active rubric")
def organizer_get_rubric(
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """View active evaluation rubric and criteria."""
    require_organizer(db, event_id, account)
    rubric = get_active_rubric(db, event_id)
    if not rubric:
        raise HTTPException(status_code=404, detail="No active rubric found.")

    criteria_list = [
        {
            "key": c.key,
            "name": c.name,
            "description": c.description,
            "weight": c.weight,
            "min_score": c.min_score,
            "max_score": c.max_score,
            "order_idx": c.order_idx,
        }
        for c in rubric.criteria
    ]
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "rubric_id": rubric.id,
            "version": rubric.version,
            "name": rubric.name,
            "criteria": criteria_list,
        },
    )


@router.post("/events/{event_id}/manage/rubric", summary="Create or update rubric")
async def organizer_post_rubric(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Update rubric criteria. Creates new version if existing reviews use the active rubric."""
    require_organizer(db, event_id, account)

    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        payload = await request.json()
        name = str(payload.get("name", "Evaluation Rubric")).strip()
        criteria = payload.get("criteria", [])
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        name = str(form.get("name", "Evaluation Rubric")).strip()
        # Parse single criterion addition from form
        ckey = str(form.get("key", "")).strip()
        cname = str(form.get("criterion_name", "")).strip()
        cdesc = str(form.get("description", "")).strip()
        cweight = form.get("weight", "1.0")
        cmin = form.get("min_score", "1.0")
        cmax = form.get("max_score", "5.0")

        # Load existing rubric criteria and append or replace
        active = get_active_rubric(db, event_id)
        criteria = []
        if active:
            for c in active.criteria:
                if c.key != ckey:
                    criteria.append({
                        "key": c.key,
                        "name": c.name,
                        "description": c.description,
                        "weight": c.weight,
                        "min_score": c.min_score,
                        "max_score": c.max_score,
                        "order_idx": c.order_idx,
                    })
        if ckey:
            criteria.append({
                "key": ckey,
                "name": cname or ckey.title(),
                "description": cdesc,
                "weight": float(cweight),
                "min_score": float(cmin),
                "max_score": float(cmax),
                "order_idx": len(criteria),
            })

    try:
        rubric = create_or_update_event_rubric(
            db=db,
            event_id=event_id,
            name=name,
            criteria_specs=criteria,
            actor=account,
        )
        if "application/json" in content_type:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"rubric_id": rubric.id, "version": rubric.version},
            )
        return RedirectResponse(url=f"/events/{event_id}/manage?tab=rubric", status_code=status.HTTP_302_FOUND)
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ve))
