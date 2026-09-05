from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import db


INBOX_DIRNAME = ".agent-hub-inbox"

ALLOWED_KINDS = {"github-pr-comment", "slack-message", "jira-comment", "generic"}


def inbox_dir(cwd: str) -> Path:
    return Path(cwd) / INBOX_DIRNAME


def _is_autopilot(schedule_id: Optional[int]) -> bool:
    if schedule_id is None:
        return False
    row = db.q1("SELECT autopilot FROM schedules WHERE id=?", (schedule_id,))
    return bool(row and row["autopilot"])


def _create_row(run_id: Optional[int], kind: str, target: str, payload: dict,
                autopilot: bool) -> int:
    if kind not in ALLOWED_KINDS:
        kind = "generic"
    aid = db.exec_(
        """INSERT INTO approvals (run_id, kind, target, payload, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, kind, str(target).strip() or "(unspecified)",
         json.dumps(payload), "pending", db.now()),
    )
    if autopilot:
        try:
            approve(aid)
        except Exception:  # noqa: BLE001
            pass
    return aid


def scan_inbox(run_id: int, cwd: str, schedule_id: Optional[int] = None) -> int:
    """After a run finishes, scan cwd/.agent-hub-inbox for JSON drops
    and insert one approvals row per file. If the source agent has autopilot,
    also dispatch each approval immediately."""
    d = inbox_dir(cwd)
    if not d.exists() or not d.is_dir():
        return 0
    processed = d / "processed"
    processed.mkdir(exist_ok=True)
    autopilot = _is_autopilot(schedule_id)
    count = 0
    for f in sorted(d.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        kind = str(payload.get("kind", "generic"))
        target = str(payload.get("target", "")).strip() or "(unspecified)"
        _create_row(run_id, kind, target, payload, autopilot)
        try:
            f.rename(processed / f.name)
        except OSError:
            pass
        count += 1
    return count


def maybe_notify_failure(schedule_id: int, run_id: int, error: str) -> None:
    """If the agent has notify_on_failure set, queue a slack-message approval
    (auto-approved if autopilot is on)."""
    row = db.q1("SELECT * FROM schedules WHERE id=?", (schedule_id,))
    if row is None or not row["notify_on_failure"]:
        return
    target = row["notify_target"] or "@me"
    payload = {
        "kind": "slack-message",
        "target": target,
        "body": (
            f"Agent *{row['name']}* (#{schedule_id}) failed on run #{run_id}.\n"
            f"Skill: `{row['skill_name']}`\n"
            f"Error: {error[:600]}"
        ),
        "meta": {
            "schedule_id": schedule_id,
            "schedule_name": row["name"],
            "run_id": run_id,
            "reason": "run_failed",
        },
    }
    _create_row(run_id, "slack-message", target, payload, bool(row["autopilot"]))


def approve(aid: int) -> dict:
    row = db.q1("SELECT * FROM approvals WHERE id=?", (aid,))
    if row is None:
        raise ValueError("approval not found")
    if row["status"] != "pending":
        return {"status": row["status"], "detail": "already resolved"}
    payload = json.loads(row["payload"])
    result = _dispatch(row["kind"], row["target"], payload)
    db.exec_(
        "UPDATE approvals SET status='approved', resolved_at=? WHERE id=?",
        (db.now(), aid),
    )
    return {"status": "approved", "result": result}


def reject(aid: int) -> None:
    db.exec_(
        "UPDATE approvals SET status='rejected', resolved_at=? WHERE id=?",
        (db.now(), aid),
    )


def _dispatch(kind: str, target: str, payload: dict) -> dict:
    """v1: no external calls yet. We only *record* what would have been sent.
    Wire real senders (PyGithub, slack_sdk, jira) once you paste credentials.

    Note: the slack-pr-review-watcher -> pr-review pipeline no longer routes
    github-pr-comment approvals through this queue — pr-review-approval-watcher
    posts those directly via `gh api` after a Slack DM reply. pr-watcher still
    drops github-pr-comment payloads here for its own repos; those remain stuck
    at dry_run until a real sender is wired up.
    """
    return {
        "dry_run": True,
        "kind": kind,
        "target": target,
        "would_send": payload,
        "note": "Wire real sender in approvals._dispatch to actually deliver.",
    }
