from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from . import db, runner
from .timefmt import DEFAULT_TZ


_scheduler: Optional[BackgroundScheduler] = None


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        # Use the display timezone so cron ("daily at 09:30") matches what
        # the user picked — 09:30 IST, not 09:30 UTC.
        _scheduler = BackgroundScheduler(timezone=DEFAULT_TZ)
        _scheduler.start()
    return _scheduler


def parse_cron(expr: str) -> CronTrigger:
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError("Cron must be 5 fields: min hour dom month dow")
    m, h, dom, mon, dow = parts
    return CronTrigger(minute=m, hour=h, day=dom, month=mon, day_of_week=dow,
                       timezone=DEFAULT_TZ)


def get_next_runs() -> list[dict]:
    """Return upcoming scheduled runs sorted ascending. Each entry:
       {schedule_id, name, cron_expr, next_run_time (aware datetime)}"""
    sched = get_scheduler()
    out = []
    for job in sched.get_jobs():
        if not job.id.startswith("schedule-"):
            continue
        try:
            sid = int(job.id.split("-", 1)[1])
        except (ValueError, IndexError):
            continue
        if job.next_run_time is None:
            continue
        row = db.q1("SELECT name, cron_expr FROM schedules WHERE id=?", (sid,))
        if row is None:
            continue
        out.append({
            "schedule_id": sid,
            "name": row["name"],
            "cron_expr": row["cron_expr"],
            "next_run_time": job.next_run_time,
        })
    out.sort(key=lambda x: x["next_run_time"])
    return out


def get_next_run_for(schedule_id: int):
    """Return the next fire time for one schedule, or None."""
    sched = get_scheduler()
    job = sched.get_job(f"schedule-{schedule_id}")
    return job.next_run_time if job else None


def _parse_json_list(raw) -> Optional[list]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return list(val) if isinstance(val, list) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_json_dict(raw) -> Optional[dict]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return dict(val) if isinstance(val, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _steps_for(schedule_id: int, row) -> list:
    """Assemble the ordered step list for an agent.
    Step 0 is always the schedule's primary skill; steps 1+ come from agent_steps."""
    steps = [{
        "skill_name": row["skill_name"],
        "skill_kind": row["skill_kind"],
        "prompt": row["prompt"] or "",
        "continue_on_error": False,
    }]
    extra = db.q(
        "SELECT skill_name, skill_kind, prompt, continue_on_error "
        "FROM agent_steps WHERE schedule_id=? ORDER BY position ASC",
        (schedule_id,),
    )
    for s in extra:
        steps.append({
            "skill_name": s["skill_name"],
            "skill_kind": s["skill_kind"],
            "prompt": s["prompt"] or "",
            "continue_on_error": bool(s["continue_on_error"]),
        })
    return steps


def _kick_off(schedule_id: int, attempt_number: int = 1) -> None:
    """Fire an agent — one run if single-step, a chain if multi-step."""
    row = db.q1("SELECT * FROM schedules WHERE id=?", (schedule_id,))
    if row is None:
        return
    # For scheduled cron ticks, respect enabled=0. For explicit retry
    # invocations we still fire (the user already asked for the attempt).
    if attempt_number == 1 and not row["enabled"]:
        return

    steps = _steps_for(schedule_id, row)
    common = dict(
        schedule_id=row["id"],
        working_directory=row["working_directory"],
        repo_id=row["repo_id"],
        allowed_mcps=_parse_json_list(row["allowed_mcps"]),
        model=row["model"] or None,
        max_turns=int(row["max_turns"]) if row["max_turns"] else None,
        max_cost_usd=float(row["max_cost_usd"]) if row["max_cost_usd"] else None,
        timeout_seconds=int(row["timeout_seconds"]) if row["timeout_seconds"] else None,
        env_vars=_parse_json_dict(row["env_vars"]),
        extra_allowed_tools=_parse_json_list(row["extra_allowed_tools"]),
        attempt_number=attempt_number,
        permission_mode=row["permission_mode"] or None,
    )

    try:
        if len(steps) == 1:
            step = steps[0]
            runner.start_run(
                skill_name=step["skill_name"],
                skill_kind=step["skill_kind"],
                user_prompt=step["prompt"],
                **common,
            )
        else:
            runner.start_chain(steps=steps, **common)
    except Exception as e:  # noqa: BLE001
        db.exec_(
            "INSERT INTO run_logs (run_id, ts, stream, line) VALUES (?, ?, ?, ?)",
            (0, db.now(), "system", f"schedule {schedule_id} failed to start: {e}"),
        )
    db.exec_("UPDATE schedules SET last_fired_at=? WHERE id=?", (db.now(), schedule_id))


def register(schedule_id: int, cron_expr: str) -> None:
    sched = get_scheduler()
    job_id = f"schedule-{schedule_id}"
    if sched.get_job(job_id):
        sched.remove_job(job_id)
    sched.add_job(
        _kick_off,
        trigger=parse_cron(cron_expr),
        args=[schedule_id, 1],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


def unregister(schedule_id: int) -> None:
    sched = get_scheduler()
    job_id = f"schedule-{schedule_id}"
    if sched.get_job(job_id):
        sched.remove_job(job_id)


def schedule_one_shot_retry(schedule_id: int, attempt_number: int, delay_seconds: int) -> None:
    """Fire a single retry for a schedule, `delay_seconds` from now."""
    sched = get_scheduler()
    when = datetime.now(timezone.utc) + timedelta(seconds=max(1, int(delay_seconds)))
    sched.add_job(
        _kick_off,
        trigger=DateTrigger(run_date=when),
        args=[schedule_id, int(attempt_number)],
        id=f"retry-{schedule_id}-{attempt_number}",
        replace_existing=True,
        max_instances=1,
    )


def load_all_from_db() -> None:
    for row in db.q("SELECT id, cron_expr, enabled FROM schedules"):
        if row["enabled"]:
            try:
                register(row["id"], row["cron_expr"])
            except ValueError:
                pass
