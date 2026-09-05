from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import json as _json
import subprocess

from . import approvals as approvals_mod
from . import db
from . import mcps as mcps_mod
from . import runner
from . import scheduler as sched_mod
from .settings import APP_ROOT, MANAGED_REPO, PLIST_LABEL_PREFIX
from .skills import discover_skills, find_skill

app = FastAPI(title="Spidey")

templates = Jinja2Templates(directory=str(APP_ROOT / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "app" / "static")), name="static")


def _fromjson(value):
    if not value:
        return []
    try:
        return _json.loads(value)
    except (_json.JSONDecodeError, ValueError, TypeError):
        return []


from . import timefmt
from .settings import DEFAULT_ALLOWED_TOOLS
templates.env.filters["fromjson"] = _fromjson
templates.env.filters["ist"] = timefmt.fmt_ist
templates.env.filters["ist_date"] = timefmt.fmt_ist_date
templates.env.filters["ist_time"] = timefmt.fmt_ist_time
templates.env.filters["rel"] = timefmt.rel_time
templates.env.globals["DISPLAY_TZ_NAME"] = timefmt.DEFAULT_TZ
templates.env.globals["DEFAULT_ALLOWED_TOOLS"] = DEFAULT_ALLOWED_TOOLS


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    _ensure_default_repo()
    sched_mod.load_all_from_db()


def _ensure_default_repo() -> None:
    """Register the configured managed repository on first startup so users always
    have somewhere for agents that don't target a specific project (e.g. an
    agent that fetches remote PR stats and writes a local synopsis file)."""
    path = str(MANAGED_REPO)
    existing = db.q1("SELECT id FROM repos WHERE path=?", (path,))
    if existing:
        return
    try:
        db.exec_(
            "INSERT INTO repos (name, path, created_at) VALUES (?, ?, ?)",
            (f"Managed repo ({MANAGED_REPO.name})", path, db.now()),
        )
    except Exception:  # noqa: BLE001
        pass


# ============================================================
# Shared context
# ============================================================

def _nav_counts() -> dict:
    return {
        "skills": len(discover_skills()),
        "agents": _scalar("SELECT COUNT(*) AS n FROM schedules") or None,
        "running": _scalar("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued','running')") or None,
        "approvals": _scalar("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'") or None,
        "repos": _scalar("SELECT COUNT(*) AS n FROM repos") or None,
    }


def _ctx(request: Request, active: str, **extra) -> dict:
    return {"request": request, "active": active, "nav": _nav_counts(), **extra}


def _resolve_wd(repo_id: Optional[str], working_directory: Optional[str]) -> tuple[Optional[int], str]:
    """Pick the cwd for a run. Priority:
      1. Explicit repo_id → repo.path
      2. Explicit working_directory → resolved absolute path
      3. Fallback: first registered repo (usually the configured managed repo)
    """
    if repo_id and repo_id != "":
        rid = int(repo_id)
        row = db.q1("SELECT path FROM repos WHERE id=?", (rid,))
        if row is None:
            raise HTTPException(400, "Unknown repo")
        return rid, row["path"]
    if working_directory:
        wd = str(Path(working_directory).expanduser().resolve())
        if not Path(wd).is_dir():
            raise HTTPException(400, f"Not a directory: {wd}")
        return None, wd
    root_path = str(MANAGED_REPO)
    fallback = (
        db.q1("SELECT id, path FROM repos WHERE path=?", (root_path,))
        or db.q1("SELECT id, path FROM repos ORDER BY id ASC LIMIT 1")
    )
    if fallback is None:
        raise HTTPException(400, "No repo registered — pick a folder for the agent to run in")
    return fallback["id"], fallback["path"]


def _one_or_404(sql: str, args: tuple, missing: str):
    row = db.q1(sql, args)
    if row is None:
        raise HTTPException(404, missing)
    return row


def _scalar(sql: str, col: str = "n", default=0):
    """Run a single-row aggregate query and return `row[col]`, or `default` if empty."""
    row = db.q1(sql)
    return row[col] if row else default


def _parse_mcps(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    try:
        val = _json.loads(raw)
    except (_json.JSONDecodeError, ValueError):
        return []
    return [str(x) for x in val] if isinstance(val, list) else []


def _parse_tool_patterns(raw: Optional[str]) -> List[str]:
    """One pattern per line, or comma-separated. Blank lines / '#'-comments dropped."""
    if not raw:
        return []
    out: List[str] = []
    for line in raw.replace(",", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _parse_env_vars(raw: Optional[str]) -> dict:
    """Accept both a JSON object (from DB) and a `KEY=value` per-line form input.
    Returns {} on any parse error."""
    if not raw:
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            v = _json.loads(raw)
            return {str(k): str(val) for k, val in v.items()} if isinstance(v, dict) else {}
        except (_json.JSONDecodeError, ValueError):
            return {}
    out: dict = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def _int_or_none(v) -> Optional[int]:
    try:
        n = int(v) if v not in (None, "", "None") else None
        return n if (n is not None and n > 0) else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v) -> Optional[float]:
    try:
        f = float(v) if v not in (None, "", "None") else None
        return f if (f is not None and f > 0) else None
    except (TypeError, ValueError):
        return None


# ---------- Schedule builder ----------
# Users pick a frequency + time/day; we generate the cron string.

WEEKDAY_LABELS = [(0, "Sun"), (1, "Mon"), (2, "Tue"), (3, "Wed"),
                  (4, "Thu"), (5, "Fri"), (6, "Sat")]


def _parse_hhmm(txt: Optional[str], default_h: int = 9, default_m: int = 0) -> tuple:
    if not txt:
        return default_h, default_m
    txt = txt.strip()
    if ":" not in txt:
        return default_h, default_m
    hh, mm = txt.split(":", 1)
    try:
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except ValueError:
        return default_h, default_m


def build_cron(
    sched_type: Optional[str],
    every_n: Optional[str] = None,
    sched_time: Optional[str] = None,
    minute: Optional[str] = None,
    dow: Optional[str] = None,
    dom: Optional[str] = None,
    cron_expr: Optional[str] = None,
) -> str:
    """Turn (frequency + friendly fields) into a 5-field cron string."""
    t = (sched_type or "").strip()
    if t == "custom" or not t:
        v = (cron_expr or "").strip()
        if not v:
            raise ValueError("Custom cron expression is empty")
        return v
    if t == "every_minutes":
        try:
            n = max(1, min(59, int(every_n or 15)))
        except (TypeError, ValueError):
            n = 15
        return f"*/{n} * * * *"
    if t == "hourly":
        try:
            m = int(minute if minute not in (None, "") else 0) % 60
        except (TypeError, ValueError):
            m = 0
        return f"{m} * * * *"
    hh, mm = _parse_hhmm(sched_time)
    if t == "daily":
        return f"{mm} {hh} * * *"
    if t == "weekly":
        try:
            d = int(dow if dow not in (None, "") else 1) % 7
        except (TypeError, ValueError):
            d = 1
        return f"{mm} {hh} * * {d}"
    if t == "monthly":
        try:
            dom_i = max(1, min(31, int(dom or 1)))
        except (TypeError, ValueError):
            dom_i = 1
        return f"{mm} {hh} {dom_i} * *"
    raise ValueError(f"Unknown schedule type: {t}")


def cron_to_builder(cron: str) -> dict:
    """Best-effort reverse-parse of a cron string into builder-friendly fields.
    Falls back to sched_type='custom' when the cron doesn't match any preset."""
    default = {
        "sched_type": "custom",
        "cron_expr": cron,
        "every_n": 15,
        "sched_time": "09:00",
        "minute": 5,
        "dow": 1,
        "dom": 1,
    }
    parts = (cron or "").strip().split()
    if len(parts) != 5:
        return default
    m, h, dom, mon, dow = parts

    def _to_time(hh, mm) -> str:
        return f"{int(hh):02d}:{int(mm):02d}"

    if h == "*" and dom == "*" and mon == "*" and dow == "*" and m.startswith("*/"):
        rest = m[2:]
        if rest.isdigit():
            return {**default, "sched_type": "every_minutes", "every_n": int(rest)}
    if h == "*" and dom == "*" and mon == "*" and dow == "*" and m.isdigit():
        return {**default, "sched_type": "hourly", "minute": int(m)}
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*" and dow == "*":
        return {**default, "sched_type": "daily", "sched_time": _to_time(h, m)}
    if m.isdigit() and h.isdigit() and dom == "*" and mon == "*" and dow.isdigit():
        return {**default, "sched_type": "weekly", "sched_time": _to_time(h, m), "dow": int(dow)}
    if m.isdigit() and h.isdigit() and dom.isdigit() and mon == "*" and dow == "*":
        return {**default, "sched_type": "monthly", "sched_time": _to_time(h, m), "dom": int(dom)}
    return default


def _fetch_runs(status: str, q: str, limit: int = 200) -> list:
    """Filter the runs table by status keyword and free-text `q`."""
    where: list[str] = []
    args: list = []
    if status in ("queued", "running", "completed", "failed"):
        where.append("status = ?")
        args.append(status)
    if q:
        where.append("(skill_name LIKE ? OR working_directory LIKE ?)")
        args.extend([f"%{q}%", f"%{q}%"])
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return db.q(f"SELECT * FROM runs {clause} ORDER BY id DESC LIMIT {int(limit)}", tuple(args))


# ============================================================
# Dashboard
# ============================================================

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    skills_total = len(discover_skills())
    agents = db.q(
        """SELECT s.*, r.name AS repo_name,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id) AS runs_total,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id AND status='completed') AS runs_ok,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id AND status='failed') AS runs_err
             FROM schedules s
             LEFT JOIN repos r ON s.repo_id = r.id
             ORDER BY s.enabled DESC, s.id DESC
             LIMIT 8"""
    )
    activity = db.q(
        "SELECT * FROM runs ORDER BY id DESC LIMIT 12"
    )
    counts = _dashboard_counts()
    counts["skills"] = skills_total
    upcoming = sched_mod.get_next_runs()[:8]
    for u in upcoming:
        row = db.q1("SELECT last_fired_at FROM schedules WHERE id=?", (u["schedule_id"],))
        u["last_fired_at"] = row["last_fired_at"] if row else None
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, "dashboard",
             counts=counts, agents=agents, activity=activity, upcoming=upcoming),
    )


def _dashboard_counts() -> dict:
    return {
        "running": _scalar("SELECT COUNT(*) AS n FROM runs WHERE status IN ('queued','running')"),
        "completed_today": _scalar(
            "SELECT COUNT(*) AS n FROM runs WHERE status='completed' AND started_at >= date('now')"
        ),
        "failed_today": _scalar(
            "SELECT COUNT(*) AS n FROM runs WHERE status='failed' AND started_at >= date('now')"
        ),
        "active_agents": _scalar("SELECT COUNT(*) AS n FROM schedules WHERE enabled=1"),
        "total_agents": _scalar("SELECT COUNT(*) AS n FROM schedules"),
        "pending_approvals": _scalar("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'"),
        "cost_today": float(
            _scalar("SELECT COALESCE(SUM(total_cost_usd),0) AS c FROM runs WHERE started_at >= date('now')", col="c")
        ),
    }


# ============================================================
# Skills
# ============================================================

SYNC_SCRIPT = APP_ROOT / "bin" / "sync-skills.sh"


def _sync_status() -> dict:
    """Run `bin/sync-skills.sh status` and parse a short summary from stdout."""
    if not SYNC_SCRIPT.exists():
        return {"available": False}
    try:
        proc = subprocess.run(
            ["/bin/bash", str(SYNC_SCRIPT), "status"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:  # noqa: BLE001
        return {"available": True, "error": str(e)}
    out = proc.stdout
    state = "unknown"
    if "fully linked" in out and "drift" not in out:
        state = "linked"
    elif "per-file symlinked" in out and "drift" not in out:
        state = "per-file"
    elif "drift" in out:
        state = "drift"
    elif "in sync" in out:
        state = "in_sync"
    daemon = "loaded" if "loaded" in out and f"{PLIST_LABEL_PREFIX}.sync-skills" in out else "off"
    return {
        "available": True,
        "state": state,
        "daemon": daemon,
        "raw": out.strip(),
    }


@app.get("/skills", response_class=HTMLResponse)
def skills_page(request: Request, q: str = "", kind: str = ""):
    skills = discover_skills()
    if q:
        needle = q.lower().strip()
        skills = [s for s in skills if needle in s.name.lower() or needle in s.description.lower()]
    if kind:
        skills = [s for s in skills if s.kind == kind]
    repos = db.q("SELECT * FROM repos ORDER BY name")
    kinds = sorted({s.kind for s in discover_skills()})
    return templates.TemplateResponse(
        "skills.html",
        _ctx(request, "skills",
             skills=skills, repos=repos, q=q, active_kind=kind, kinds=kinds,
             sync=_sync_status(), mcps=mcps_mod.discover_mcps()),
    )


@app.post("/sync/skills")
def sync_skills_action(action: str = Form("sync")):
    if action not in ("sync", "link", "install-daemon", "uninstall-daemon", "install"):
        raise HTTPException(400, f"unknown sync action: {action}")
    if not SYNC_SCRIPT.exists():
        raise HTTPException(500, "sync-skills.sh not found")
    try:
        subprocess.run(
            ["/bin/bash", str(SYNC_SCRIPT), action],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"sync failed: {e}")
    return RedirectResponse("/skills", status_code=303)


# ============================================================
# Agents (scheduled skills as first-class entities)
# ============================================================

@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    agents = db.q(
        """SELECT s.*, r.name AS repo_name,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id) AS runs_total,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id AND status='completed') AS runs_ok,
                  (SELECT COUNT(*) FROM runs WHERE schedule_id = s.id AND status='failed') AS runs_err,
                  (SELECT MAX(started_at) FROM runs WHERE schedule_id = s.id) AS last_run_at,
                  (SELECT status FROM runs WHERE schedule_id = s.id ORDER BY id DESC LIMIT 1) AS last_status,
                  (SELECT COALESCE(SUM(total_cost_usd),0) FROM runs WHERE schedule_id = s.id) AS total_cost,
                  (1 + (SELECT COUNT(*) FROM agent_steps WHERE schedule_id = s.id)) AS step_count
             FROM schedules s
             LEFT JOIN repos r ON s.repo_id = r.id
             ORDER BY s.enabled DESC, s.id DESC"""
    )
    repos = db.q("SELECT * FROM repos ORDER BY name")
    skills = discover_skills()
    mcps = mcps_mod.discover_mcps()
    agents_out = []
    for a in agents:
        d = dict(a)
        d["mcps_list"] = _parse_mcps(a["allowed_mcps"])
        d["next_run_time"] = sched_mod.get_next_run_for(a["id"]) if a["enabled"] else None
        agents_out.append(d)
    return templates.TemplateResponse(
        "agents.html",
        _ctx(request, "agents",
             agents=agents_out, repos=repos, skills=skills, mcps=mcps,
             weekdays=WEEKDAY_LABELS),
    )


@app.get("/agents/{sid}", response_class=HTMLResponse)
def agent_detail(request: Request, sid: int):
    agent = _one_or_404(
        """SELECT s.*, r.name AS repo_name FROM schedules s
             LEFT JOIN repos r ON s.repo_id = r.id
             WHERE s.id=?""",
        (sid,),
        "Agent not found",
    )
    runs = db.q(
        "SELECT * FROM runs WHERE schedule_id=? ORDER BY id DESC LIMIT 100",
        (sid,),
    )
    stats = db.q1(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS ok,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS err,
                  COALESCE(SUM(total_cost_usd),0) AS cost
             FROM runs WHERE schedule_id=?""",
        (sid,),
    )
    all_mcps = mcps_mod.discover_mcps(agent["working_directory"])
    picked = set(_parse_mcps(agent["allowed_mcps"]))
    sched = cron_to_builder(agent["cron_expr"])
    extra_steps = db.q(
        "SELECT * FROM agent_steps WHERE schedule_id=? ORDER BY position",
        (sid,),
    )
    all_skills = discover_skills()
    return templates.TemplateResponse(
        "agent_detail.html",
        _ctx(request, "agents",
             agent=agent, runs=runs, stats=stats,
             mcps=all_mcps, picked_mcps=picked,
             sched=sched, weekdays=WEEKDAY_LABELS,
             extra_steps=extra_steps, all_skills=all_skills,
             inherit_model=_inherit_model(),
             next_run_time=sched_mod.get_next_run_for(sid) if agent["enabled"] else None),
    )


def _inherit_model() -> str:
    """Whatever the run will fall back to when the agent's model is blank.
    Chain: HUB_MODEL env → ~/.claude/settings.json → claude's built-in default.
    Best-effort: we can read the first two but not the last."""
    import os
    env_m = (os.environ.get("HUB_MODEL") or "").strip()
    if env_m:
        return env_m
    try:
        raw = _json.loads(Path(Path.home() / ".claude" / "settings.json").read_text())
        m = (raw.get("model") or "").strip()
        if m:
            return m
    except Exception:  # noqa: BLE001
        pass
    return "opus"  # claude's shipped default


@app.post("/agents")
def create_agent(
    name: str = Form(...),
    skill_name: str = Form(...),
    skill_kind: str = Form(...),
    # Schedule — either builder fields or raw cron_expr.
    sched_type: Optional[str] = Form(None),
    every_n: Optional[str] = Form(None),
    sched_time: Optional[str] = Form(None),
    minute: Optional[str] = Form(None),
    dow: Optional[str] = Form(None),
    dom: Optional[str] = Form(None),
    cron_expr: Optional[str] = Form(None),
    repo_id: Optional[str] = Form(None),
    working_directory: Optional[str] = Form(None),
    prompt: str = Form(""),
    mcps: Optional[List[str]] = Form(None),
    # advanced settings
    model: Optional[str] = Form(None),
    max_turns: Optional[str] = Form(None),
    max_cost_usd: Optional[str] = Form(None),
    timeout_seconds: Optional[str] = Form(None),
    retry_count: Optional[str] = Form(None),
    retry_delay_seconds: Optional[str] = Form(None),
    autopilot: Optional[str] = Form(None),
    notify_on_failure: Optional[str] = Form(None),
    notify_target: Optional[str] = Form(None),
    env_vars: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    permission_mode: Optional[str] = Form(None),
    extra_allowed_tools: Optional[str] = Form(None),
    # Additional steps (parallel arrays)
    step_skill_name: Optional[List[str]] = Form(None),
    step_skill_kind: Optional[List[str]] = Form(None),
    step_prompt: Optional[List[str]] = Form(None),
    step_continue_on_error: Optional[List[str]] = Form(None),
):
    if find_skill(skill_name, skill_kind) is None:
        raise HTTPException(400, f"Unknown skill: {skill_name}")
    rid, wd = _resolve_wd(repo_id, working_directory)

    try:
        computed_cron = build_cron(sched_type, every_n, sched_time, minute, dow, dom, cron_expr)
        sched_mod.parse_cron(computed_cron)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cron_expr = computed_cron

    mcps_clean = [m for m in (mcps or []) if m]
    mcps_json = _json.dumps(mcps_clean) if mcps_clean else None
    env_dict = _parse_env_vars(env_vars)
    env_json = _json.dumps(env_dict) if env_dict else None
    tools_extra = _parse_tool_patterns(extra_allowed_tools)
    tools_json = _json.dumps(tools_extra) if tools_extra else None

    # Extra steps come in as parallel arrays: step_skill_name[], step_skill_kind[],
    # step_prompt[], step_continue_on_error[]. They are read below.
    pmode = (permission_mode or "").strip() or None
    if pmode and pmode not in runner.ALLOWED_PERMISSION_MODES:
        raise HTTPException(400, f"Invalid permission mode: {pmode}")

    sid = db.exec_(
        """INSERT INTO schedules
           (name, skill_name, skill_kind, repo_id, working_directory,
            prompt, cron_expr, enabled, created_at, allowed_mcps,
            model, max_turns, max_cost_usd, timeout_seconds,
            retry_count, retry_delay_seconds,
            autopilot, notify_on_failure, notify_target,
            env_vars, notes, permission_mode, extra_allowed_tools)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?,
                   ?, ?, ?, ?,
                   ?, ?,
                   ?, ?, ?,
                   ?, ?, ?, ?)""",
        (name.strip(), skill_name, skill_kind, rid, wd,
         prompt, cron_expr.strip(), db.now(), mcps_json,
         (model or "").strip() or None,
         _int_or_none(max_turns),
         _float_or_none(max_cost_usd),
         _int_or_none(timeout_seconds),
         _int_or_none(retry_count) or 0,
         _int_or_none(retry_delay_seconds) or 60,
         1 if autopilot else 0,
         1 if notify_on_failure else 0,
         (notify_target or "").strip() or None,
         env_json,
         (notes or "").strip() or None,
         pmode,
         tools_json),
    )
    _insert_extra_steps(sid, step_skill_name, step_skill_kind, step_prompt, step_continue_on_error)
    sched_mod.register(sid, cron_expr)
    return RedirectResponse(f"/agents/{sid}", status_code=303)


def _insert_extra_steps(
    sid: int,
    names: Optional[List[str]],
    kinds: Optional[List[str]],
    prompts: Optional[List[str]],
    coes: Optional[List[str]],
) -> None:
    names = names or []
    kinds = kinds or []
    prompts = prompts or []
    coes = set(coes or [])  # form sends "1" for each index that's checked — we key by full index string below

    # Zip together — any blank skill_name means "skip this row".
    pos = 1
    for i in range(max(len(names), len(kinds))):
        n = (names[i] if i < len(names) else "").strip()
        k = (kinds[i] if i < len(kinds) else "").strip()
        if not n or not k:
            continue
        if find_skill(n, k) is None:
            continue  # silently skip garbage
        p = (prompts[i] if i < len(prompts) else "").strip()
        coe = 1 if str(i) in coes else 0
        db.exec_(
            """INSERT INTO agent_steps
               (schedule_id, position, skill_name, skill_kind, prompt, continue_on_error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, pos, n, k, p, coe, db.now()),
        )
        pos += 1


@app.post("/agents/{sid}/settings")
def update_agent_settings(
    sid: int,
    name: Optional[str] = Form(None),
    # Schedule builder fields (edit-time)
    sched_type: Optional[str] = Form(None),
    every_n: Optional[str] = Form(None),
    sched_time: Optional[str] = Form(None),
    minute: Optional[str] = Form(None),
    dow: Optional[str] = Form(None),
    dom: Optional[str] = Form(None),
    cron_expr: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    max_turns: Optional[str] = Form(None),
    max_cost_usd: Optional[str] = Form(None),
    timeout_seconds: Optional[str] = Form(None),
    retry_count: Optional[str] = Form(None),
    retry_delay_seconds: Optional[str] = Form(None),
    autopilot: Optional[str] = Form(None),
    notify_on_failure: Optional[str] = Form(None),
    notify_target: Optional[str] = Form(None),
    env_vars: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    permission_mode: Optional[str] = Form(None),
    extra_allowed_tools: Optional[str] = Form(None),
):
    row = _one_or_404("SELECT * FROM schedules WHERE id=?", (sid,), "Not found")
    pmode = (permission_mode or "").strip() or None
    if pmode and pmode not in runner.ALLOWED_PERMISSION_MODES:
        raise HTTPException(400, f"Invalid permission mode: {pmode}")
    try:
        if sched_type or cron_expr:
            new_cron = build_cron(sched_type, every_n, sched_time, minute, dow, dom, cron_expr)
        else:
            new_cron = row["cron_expr"]
        sched_mod.parse_cron(new_cron)
    except ValueError as e:
        raise HTTPException(400, str(e))
    env_dict = _parse_env_vars(env_vars) if env_vars is not None else _parse_env_vars(row["env_vars"])
    env_json = _json.dumps(env_dict) if env_dict else None

    # PATCH-style semantics: only overwrite a column when the form actually
    # sent that field. Empty strings still count as "user wants to clear".
    # None = "field not in this request → keep existing value".
    def keep(new, current):
        return current if new is None else new

    tools_json = None
    if extra_allowed_tools is not None:
        tools_extra = _parse_tool_patterns(extra_allowed_tools)
        tools_json = _json.dumps(tools_extra) if tools_extra else None
    db.exec_(
        """UPDATE schedules SET
             name=?, cron_expr=?, prompt=?,
             model=?, max_turns=?, max_cost_usd=?, timeout_seconds=?,
             retry_count=?, retry_delay_seconds=?,
             autopilot=?, notify_on_failure=?, notify_target=?,
             env_vars=?, notes=?, permission_mode=?, extra_allowed_tools=?
           WHERE id=?""",
        ((name or row["name"]).strip(),
         new_cron,
         prompt if prompt is not None else row["prompt"],
         keep((model or "").strip() or None if model is not None else None, row["model"]),
         keep(_int_or_none(max_turns) if max_turns is not None else None, row["max_turns"]),
         keep(_float_or_none(max_cost_usd) if max_cost_usd is not None else None, row["max_cost_usd"]),
         keep(_int_or_none(timeout_seconds) if timeout_seconds is not None else None, row["timeout_seconds"]),
         keep(_int_or_none(retry_count) if retry_count is not None else None, row["retry_count"] or 0),
         keep(_int_or_none(retry_delay_seconds) if retry_delay_seconds is not None else None, row["retry_delay_seconds"] or 60),
         keep(1 if autopilot else 0 if autopilot is not None else None, row["autopilot"] or 0),
         keep(1 if notify_on_failure else 0 if notify_on_failure is not None else None, row["notify_on_failure"] or 0),
         keep((notify_target or "").strip() or None if notify_target is not None else None, row["notify_target"]),
         keep(env_json if env_vars is not None else None, row["env_vars"]),
         keep((notes or "").strip() or None if notes is not None else None, row["notes"]),
         keep(pmode if permission_mode is not None else None, row["permission_mode"]),
         keep(tools_json if extra_allowed_tools is not None else None, row["extra_allowed_tools"]),
         sid),
    )
    if row["enabled"]:
        sched_mod.register(sid, new_cron)
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/steps")
def add_agent_step(
    sid: int,
    skill_name: str = Form(...),
    skill_kind: str = Form(...),
    prompt: str = Form(""),
    continue_on_error: Optional[str] = Form(None),
):
    _one_or_404("SELECT id FROM schedules WHERE id=?", (sid,), "Not found")
    if find_skill(skill_name, skill_kind) is None:
        raise HTTPException(400, f"Unknown skill: {skill_name}")
    row = db.q1(
        "SELECT COALESCE(MAX(position), 0) AS mx FROM agent_steps WHERE schedule_id=?",
        (sid,),
    )
    next_pos = (row["mx"] if row else 0) + 1
    db.exec_(
        """INSERT INTO agent_steps
           (schedule_id, position, skill_name, skill_kind, prompt, continue_on_error, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, next_pos, skill_name, skill_kind, prompt.strip(),
         1 if continue_on_error else 0, db.now()),
    )
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/steps/{step_id}/delete")
def delete_agent_step(sid: int, step_id: int):
    _one_or_404("SELECT id FROM agent_steps WHERE id=? AND schedule_id=?",
                (step_id, sid), "Step not found")
    db.exec_("DELETE FROM agent_steps WHERE id=?", (step_id,))
    _renumber_steps(sid)
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/steps/{step_id}/move")
def move_agent_step(sid: int, step_id: int, direction: str = Form(...)):
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be 'up' or 'down'")
    steps = db.q(
        "SELECT id, position FROM agent_steps WHERE schedule_id=? ORDER BY position",
        (sid,),
    )
    ids = [s["id"] for s in steps]
    if step_id not in ids:
        raise HTTPException(404, "Step not found")
    idx = ids.index(step_id)
    target = idx - 1 if direction == "up" else idx + 1
    if 0 <= target < len(ids):
        ids[idx], ids[target] = ids[target], ids[idx]
        for pos, sid_ in enumerate(ids, start=1):
            db.exec_("UPDATE agent_steps SET position=? WHERE id=?", (pos, sid_))
    return RedirectResponse(f"/agents/{sid}", status_code=303)


def _renumber_steps(sid: int) -> None:
    for pos, s in enumerate(
        db.q("SELECT id FROM agent_steps WHERE schedule_id=? ORDER BY position", (sid,)),
        start=1,
    ):
        db.exec_("UPDATE agent_steps SET position=? WHERE id=?", (pos, s["id"]))


@app.post("/agents/{sid}/mcps")
def update_agent_mcps(sid: int, mcps: Optional[List[str]] = Form(None)):
    _one_or_404("SELECT id FROM schedules WHERE id=?", (sid,), "Not found")
    mcps_clean = [m for m in (mcps or []) if m]
    mcps_json = _json.dumps(mcps_clean) if mcps_clean else None
    db.exec_("UPDATE schedules SET allowed_mcps=? WHERE id=?", (mcps_json, sid))
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/toggle")
def toggle_agent(sid: int):
    row = _one_or_404("SELECT * FROM schedules WHERE id=?", (sid,), "Not found")
    new_state = 0 if row["enabled"] else 1
    db.exec_("UPDATE schedules SET enabled=? WHERE id=?", (new_state, sid))
    if new_state:
        sched_mod.register(sid, row["cron_expr"])
    else:
        sched_mod.unregister(sid)
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/run-now")
def agent_run_now(sid: int):
    row = _one_or_404("SELECT * FROM schedules WHERE id=?", (sid,), "Not found")
    env = _parse_env_vars(row["env_vars"]) if row["env_vars"] else {}
    common = dict(
        working_directory=row["working_directory"],
        repo_id=row["repo_id"],
        schedule_id=sid,
        allowed_mcps=_parse_mcps(row["allowed_mcps"]) or None,
        model=row["model"] or None,
        max_turns=row["max_turns"] or None,
        max_cost_usd=row["max_cost_usd"] or None,
        timeout_seconds=row["timeout_seconds"] or None,
        env_vars=env or None,
        extra_allowed_tools=_parse_mcps(row["extra_allowed_tools"]) or None,
        permission_mode=row["permission_mode"] or None,
    )
    steps = sched_mod._steps_for(sid, row)
    if len(steps) == 1:
        run_id = runner.start_run(
            skill_name=steps[0]["skill_name"],
            skill_kind=steps[0]["skill_kind"],
            user_prompt=steps[0]["prompt"],
            **common,
        )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)
    runner.start_chain(steps=steps, **common)
    return RedirectResponse(f"/agents/{sid}", status_code=303)


@app.post("/agents/{sid}/delete")
def delete_agent(sid: int):
    sched_mod.unregister(sid)
    db.exec_("DELETE FROM schedules WHERE id=?", (sid,))
    return RedirectResponse("/agents", status_code=303)


# ============================================================
# Runs
# ============================================================

@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request, status: str = "", q: str = ""):
    runs = _fetch_runs(status, q)
    upcoming = sched_mod.get_next_runs()[:8]
    for u in upcoming:
        row = db.q1("SELECT last_fired_at FROM schedules WHERE id=?", (u["schedule_id"],))
        u["last_fired_at"] = row["last_fired_at"] if row else None
    return templates.TemplateResponse(
        "runs.html",
        _ctx(request, "runs", runs=runs, status=status, q=q, upcoming=upcoming),
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = _one_or_404("SELECT * FROM runs WHERE id=?", (run_id,), "Run not found")
    logs = db.q(
        "SELECT * FROM run_logs WHERE run_id=? ORDER BY id ASC LIMIT 2000",
        (run_id,),
    )
    return templates.TemplateResponse(
        "run_detail.html", _ctx(request, "runs", run=run, logs=logs)
    )


@app.post("/runs/{run_id}/kill")
def kill_run_endpoint(run_id: int):
    result = runner.kill_run(run_id)
    if not result.get("killed") and "not found" in (result.get("error") or ""):
        raise HTTPException(404, "Run not found")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/run")
def create_run(
    skill_name: str = Form(...),
    skill_kind: str = Form(...),
    repo_id: Optional[str] = Form(None),
    working_directory: Optional[str] = Form(None),
    prompt: str = Form(""),
    permission_mode: Optional[str] = Form(None),
    mcps: Optional[List[str]] = Form(None),
    extra_allowed_tools: Optional[str] = Form(None),
):
    _, wd = _resolve_wd(repo_id, working_directory)
    try:
        run_id = runner.start_run(
            skill_name=skill_name,
            skill_kind=skill_kind,
            working_directory=wd,
            user_prompt=prompt,
            repo_id=int(repo_id) if repo_id else None,
            permission_mode=permission_mode,
            allowed_mcps=[m for m in (mcps or []) if m] or None,
            extra_allowed_tools=_parse_tool_patterns(extra_allowed_tools) or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ============================================================
# Approvals
# ============================================================

@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, status: str = "pending"):
    if status not in ("pending", "approved", "rejected", "all"):
        status = "pending"
    if status == "all":
        rows = db.q("SELECT * FROM approvals ORDER BY id DESC LIMIT 200")
    else:
        rows = db.q("SELECT * FROM approvals WHERE status=? ORDER BY id DESC LIMIT 200", (status,))
    return templates.TemplateResponse(
        "approvals.html",
        _ctx(request, "approvals", approvals=rows, status=status),
    )


@app.get("/approvals/{aid}", response_class=HTMLResponse)
def approval_detail(request: Request, aid: int):
    row = _one_or_404("SELECT * FROM approvals WHERE id=?", (aid,), "Approval not found")
    return templates.TemplateResponse(
        "approval_detail.html", _ctx(request, "approvals", approval=row)
    )


@app.post("/approvals/{aid}/approve")
def approvals_approve(aid: int):
    try:
        approvals_mod.approve(aid)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse("/approvals", status_code=303)


@app.post("/approvals/{aid}/reject")
def approvals_reject(aid: int):
    approvals_mod.reject(aid)
    return RedirectResponse("/approvals", status_code=303)


# ============================================================
# Repos
# ============================================================

@app.get("/repos", response_class=HTMLResponse)
def repos_page(request: Request):
    repos = db.q(
        """SELECT r.*,
                  (SELECT COUNT(*) FROM runs WHERE repo_id = r.id) AS runs_total,
                  (SELECT COUNT(*) FROM schedules WHERE repo_id = r.id) AS agents_total
             FROM repos r ORDER BY name"""
    )
    return templates.TemplateResponse(
        "repos.html", _ctx(request, "repos", repos=repos)
    )


@app.post("/repos")
def add_repo(name: str = Form(...), path: str = Form(...)):
    resolved = str(Path(path).expanduser().resolve())
    if not Path(resolved).is_dir():
        raise HTTPException(400, f"Not a directory: {resolved}")
    try:
        db.exec_(
            "INSERT INTO repos (name, path, created_at) VALUES (?, ?, ?)",
            (name.strip(), resolved, db.now()),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not add repo: {e}")
    return RedirectResponse("/repos", status_code=303)


@app.post("/repos/{repo_id}/delete")
def delete_repo(repo_id: int):
    db.exec_("DELETE FROM repos WHERE id=?", (repo_id,))
    return RedirectResponse("/repos", status_code=303)


# ============================================================
# HTMX partials for live refresh
# ============================================================

@app.get("/partials/dashboard-kpis", response_class=HTMLResponse)
def partial_dashboard_kpis(request: Request):
    counts = _dashboard_counts()
    counts["skills"] = len(discover_skills())
    return templates.TemplateResponse(
        "_dashboard_kpis.html", {"request": request, "counts": counts}
    )


@app.get("/partials/dashboard-activity", response_class=HTMLResponse)
def partial_dashboard_activity(request: Request):
    activity = db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 12")
    return templates.TemplateResponse(
        "_activity.html", {"request": request, "activity": activity}
    )


@app.get("/partials/dashboard-upcoming", response_class=HTMLResponse)
def partial_dashboard_upcoming(request: Request):
    upcoming = sched_mod.get_next_runs()[:8]
    # Attach last_fired_at from schedules
    for u in upcoming:
        row = db.q1("SELECT last_fired_at FROM schedules WHERE id=?", (u["schedule_id"],))
        u["last_fired_at"] = row["last_fired_at"] if row else None
    return templates.TemplateResponse(
        "_upcoming.html", {"request": request, "upcoming": upcoming}
    )


@app.get("/partials/runs", response_class=HTMLResponse)
def partial_runs(request: Request, status: str = "", q: str = ""):
    runs = _fetch_runs(status, q)
    return templates.TemplateResponse(
        "_runs_table.html", {"request": request, "runs": runs}
    )


@app.get("/partials/run/{run_id}/logs", response_class=HTMLResponse)
def partial_run_logs(request: Request, run_id: int):
    run = db.q1("SELECT * FROM runs WHERE id=?", (run_id,))
    logs = db.q(
        "SELECT * FROM run_logs WHERE run_id=? ORDER BY id ASC LIMIT 2000",
        (run_id,),
    )
    return templates.TemplateResponse(
        "_run_log.html", {"request": request, "run": run, "logs": logs}
    )
