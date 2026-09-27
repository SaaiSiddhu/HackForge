from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_account, validate_csrf
from app.database import get_db
from app.export_services import (
    generate_assignments_csv,
    generate_judges_calibration_csv,
    generate_judging_results_csv,
    generate_projects_csv,
    generate_reviews_csv,
    generate_teams_csv,
)
from app.judging_services import get_account_judge_for_event, is_account_event_organizer
from app.models import Account, Event, EventResultsSnapshot
from app.scoring_services import (
    calculate_event_results,
    check_snapshot_staleness,
    create_results_snapshot,
    validate_normalization_parameters,
)
from app.templating import get_request_csrf_token, templates

router = APIRouter(tags=["results"])


def check_results_authorization(
    db: Session,
    event_id: str,
    account: Optional[Account],
) -> Account:
    """Authorize access to judging results and exports.

    Enforces:
    - User is authenticated (401).
    - User is an authorized event organizer or platform admin for event_id (403).
    - User is NOT an active judge in this event (Dual-role restriction: 403).
    """
    if not account:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    if not is_account_event_organizer(db, event_id, account.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: You are not an organizer of event '{event_id}'.",
        )
    # Dual-role restriction: active judges in this event cannot view results or exports
    if get_account_judge_for_event(db, event_id, account.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dual-role restriction: Organizers who are active judges in this event cannot view judging results, score aggregates, or exports.",
        )
    return account


def _load_authorized_snapshot(
    db: Session,
    request_event_id: str,
    snapshot_id: str,
    account: Optional[Account],
) -> EventResultsSnapshot:
    """Load a snapshot and strictly verify both request event authorization and stored snapshot.event_id ownership."""
    check_results_authorization(db, request_event_id, account)
    snapshot = db.get(EventResultsSnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="Snapshot not found.")
    # Verify authorization against the snapshot's stored event_id as well
    if snapshot.event_id != request_event_id:
        check_results_authorization(db, snapshot.event_id, account)
        raise HTTPException(status_code=404, detail="Snapshot does not belong to the requested event.")
    check_results_authorization(db, snapshot.event_id, account)
    return snapshot


# --- Organizer Results View & Snapshot Management ---


@router.get("/events/{event_id}/manage/results", summary="Organizer judging results and normalization")
def organizer_results_view(
    request: Request,
    event_id: str,
    cohort_id: Optional[str] = Query(None),
    snapshot_id: Optional[str] = Query(None),
    track_id: Optional[str] = Query(None),
    rubric_id: Optional[str] = Query(None),
    min_sample: int = Query(3),
    epsilon: float = Query(1e-6),
    method: str = Query("zscore_v1"),
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """View defensible normalized scoring, raw score validation, and snapshot history."""
    check_results_authorization(db, event_id, account)

    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found.")

    selected_snapshot = None
    staleness_info = {"is_stale": False, "stale_reasons": []}

    if snapshot_id:
        selected_snapshot = _load_authorized_snapshot(db, event_id, snapshot_id, account)
        results_data = selected_snapshot.results_data
        staleness_info = check_snapshot_staleness(db, selected_snapshot)
    else:
        try:
            clean_min_sample, clean_epsilon, clean_method = validate_normalization_parameters(
                min_sample=min_sample,
                epsilon=epsilon,
                method=method,
            )
            results_data = calculate_event_results(
                db=db,
                event_id=event_id,
                track_id=track_id,
                rubric_id=rubric_id,
                min_sample=clean_min_sample,
                epsilon=clean_epsilon,
                method=clean_method,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # Determine selected cohort (defaults to first comparable track/rubric cohort)
    cohorts = results_data.get("cohorts", [])
    selected_cohort = None
    if cohort_id:
        selected_cohort = next((c for c in cohorts if c.get("cohort_id") == cohort_id), None)
    if not selected_cohort and cohorts:
        selected_cohort = cohorts[0]

    # Preload snapshots list
    snapshots = db.scalars(
        select(EventResultsSnapshot)
        .where(EventResultsSnapshot.event_id == event_id)
        .order_by(EventResultsSnapshot.created_at.desc())
    ).all()

    # Support JSON requests
    accept = request.headers.get("accept", "").lower()
    if "application/json" in accept:
        return JSONResponse(
            status_code=200,
            content={
                "event_id": event_id,
                "snapshot_id": selected_snapshot.id if selected_snapshot else None,
                "is_stale": staleness_info.get("is_stale", False),
                "provenance_status": staleness_info.get("provenance_status", "live"),
                "selected_cohort": selected_cohort,
                "results_data": results_data,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="event_results.html",
        context={
            "event": event,
            "account": account,
            "current_account": account,
            "results_data": results_data,
            "selected_cohort": selected_cohort,
            "selected_snapshot": selected_snapshot,
            "staleness_info": staleness_info,
            "snapshots": snapshots,
            "csrf_token": get_request_csrf_token(request),
        },
    )


@router.post("/events/{event_id}/manage/results/snapshots", summary="Create results snapshot")
async def create_results_snapshot_endpoint(
    request: Request,
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Compute and freeze an immutable results snapshot with canonical manifest provenance."""
    check_results_authorization(db, event_id, account)

    event = db.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail=f"Event '{event_id}' not found.")

    content_type = request.headers.get("content-type", "").lower()
    params: dict[str, Any] = {"min_sample": 3, "epsilon": 1e-6, "method": "zscore_v1"}

    if "application/json" in content_type:
        payload = await request.json()
        if isinstance(payload, dict):
            params.update(payload)
    else:
        form = await request.form()
        validate_csrf(request, csrf_token_form=form.get("csrf_token"))
        if form.get("min_sample") is not None and str(form.get("min_sample")).strip() != "":
            params["min_sample"] = form.get("min_sample")
        if form.get("epsilon") is not None and str(form.get("epsilon")).strip() != "":
            params["epsilon"] = form.get("epsilon")
        if form.get("method") is not None and str(form.get("method")).strip() != "":
            params["method"] = form.get("method")
        if form.get("track_id"):
            params["track_id"] = form.get("track_id")
        if form.get("rubric_id"):
            params["rubric_id"] = form.get("rubric_id")

    try:
        snapshot = create_results_snapshot(
            db=db,
            event_id=event_id,
            creator_account=account,
            method=str(params.get("method", "zscore_v1")),
            parameters=params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if "application/json" in content_type:
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={
                "snapshot_id": snapshot.id,
                "event_id": snapshot.event_id,
                "created_at": snapshot.created_at.isoformat(),
            },
        )

    return RedirectResponse(
        url=f"/events/{event_id}/manage/results?snapshot_id={snapshot.id}",
        status_code=status.HTTP_302_FOUND,
    )


# --- Comprehensive Organizer CSV Exports ---


@router.get("/events/{event_id}/export/judging.csv", summary="Export judging results CSV")
def export_judging_results(
    event_id: str,
    cohort_id: Optional[str] = Query(None),
    track_id: Optional[str] = Query(None),
    rubric_id: Optional[str] = Query(None),
    snapshot_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export normalized and raw judging results with RFC 4180 and formula protection."""
    check_results_authorization(db, event_id, account)
    if snapshot_id:
        snapshot = _load_authorized_snapshot(db, event_id, snapshot_id, account)
        results_data = snapshot.results_data
    else:
        results_data = calculate_event_results(
            db=db,
            event_id=event_id,
            track_id=track_id,
            rubric_id=rubric_id,
        )

    csv_content = generate_judging_results_csv(results_data, cohort_id=cohort_id)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="judging_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/events/{event_id}/export/reviews.csv", summary="Export all reviews CSV")
def export_reviews(
    event_id: str,
    snapshot_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export all reviews with scores, rubric versions, and eligibility reasons."""
    check_results_authorization(db, event_id, account)
    snapshot = None
    if snapshot_id:
        snapshot = _load_authorized_snapshot(db, event_id, snapshot_id, account)
    csv_content = generate_reviews_csv(db=db, event_id=event_id, snapshot=snapshot)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="reviews_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/events/{event_id}/export/assignments.csv", summary="Export judge assignments CSV")
def export_assignments(
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export all judge assignments with review progress status."""
    check_results_authorization(db, event_id, account)
    csv_content = generate_assignments_csv(db=db, event_id=event_id)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="assignments_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/events/{event_id}/export/teams.csv", summary="Export teams CSV")
def export_teams(
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export all teams and members for the event."""
    check_results_authorization(db, event_id, account)
    csv_content = generate_teams_csv(db=db, event_id=event_id)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="teams_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/events/{event_id}/export/projects.csv", summary="Export projects CSV")
def export_projects(
    event_id: str,
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export all projects with assignment and review completion counts."""
    check_results_authorization(db, event_id, account)
    csv_content = generate_projects_csv(db=db, event_id=event_id)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="projects_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )


@router.get("/events/{event_id}/export/judges-calibration.csv", summary="Export judges calibration CSV")
def export_judges_calibration(
    event_id: str,
    track_id: Optional[str] = Query(None),
    rubric_id: Optional[str] = Query(None),
    snapshot_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    account: Optional[Account] = Depends(get_current_account),
):
    """Export judge sample counts, means, population standard deviations, and usability flags."""
    check_results_authorization(db, event_id, account)
    if snapshot_id:
        snapshot = _load_authorized_snapshot(db, event_id, snapshot_id, account)
        results_data = snapshot.results_data
    else:
        results_data = calculate_event_results(
            db=db,
            event_id=event_id,
            track_id=track_id,
            rubric_id=rubric_id,
        )

    csv_content = generate_judges_calibration_csv(results_data)
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="judges_calibration_{event_id}.csv"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
        },
    )
