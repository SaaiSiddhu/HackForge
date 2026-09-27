import json
from datetime import datetime
from pathlib import Path
from .database import SessionLocal
from .models import Event, Track, Team, Judge, Project, Review

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures.json"


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed(session, path=FIXTURES):
    data = json.loads(Path(path).read_text())
    event_id = data["event"]["id"]

    def insert(model, key, values):
        if session.get(model, key) is None:
            session.add(model(**values))

    event = dict(data["event"])
    event["submissions_close"] = timestamp(event["submissions_close"])
    insert(Event, event_id, event)
    session.flush()
    for model, group in [(Track, "tracks"), (Team, "teams"), (Judge, "judges")]:
        for row in data[group]:
            insert(model, row["id"], dict(row, event_id=event_id))
        session.flush()
    for row in data["projects"]:
        insert(Project, row["id"], dict(
            id=row["id"], event_id=event_id, team_id=row["team"], track_id=row["track"],
            title=row["title"], summary=row["summary"], repo_url=row["repo_url"],
            submitted_at=timestamp(row["submitted_at"])))
    session.flush()
    for row in data["scores"]:
        insert(Review, (row["judge"], row["project"]), dict(
            judge_id=row["judge"], project_id=row["project"],
            criteria=row["criteria"], comment=row.get("comment", "")))
    session.flush()


if __name__ == "__main__":
    with SessionLocal.begin() as session:
        seed(session)
    print("HackForge: official fixtures loaded; existing records preserved.")
