from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import approvals as approvals_mod
from . import db
from . import mcps as mcps_mod
from .settings import (
    CLAUDE_BIN,
    DATA_DIR,
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_MODEL,
    DEFAULT_PERMISSION_MODE,
)
from .skills import find_skill


TMP_MCP_DIR = DATA_DIR / "mcp-configs"
TMP_MCP_DIR.mkdir(parents=True, exist_ok=True)


ALLOWED_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}


HUB_HANDOFF = """\
--- Agent Hub context ---
You are running under Agent Hub. For any action that would send data outside \
this repo (post a PR comment, send a Slack message, create/comment on a Jira \
ticket), DO NOT call the tool directly. Instead, write a JSON file to \
.agent-hub-inbox/<slug>.json in the current working directory with fields:
  { "kind": "github-pr-comment" | "slack-message" | "jira-comment" | "generic",
    "target": "human-readable identifier (URL, channel, ticket key)",
    "body": "...",             // required for text-shaped actions
    "meta": { ... }            // any extra structured data the sender needs
  }
The hub queues these for human approval. Local edits and shell commands are \
fine — the guard is only for outbound network actions.
--- end context ---"""


def build_prompt(
    skill_name: str,
    skill_kind: str,
    user_prompt: str,
    include_handoff: bool = True,
) -> str:
    """Reference the skill/command by name and pass the user's goal.
    When include_handoff is False (autopilot agents), skip the draft-first
    appendix so the skill can call MCP tools / bash directly."""
    kind_is_command = skill_kind.endswith("command")
    invocation = f"/{skill_name}" if kind_is_command else f"Use the {skill_name} skill."
    body = user_prompt.strip() or "Proceed."
    if include_handoff:
        return f"{invocation}\n\n{body}\n\n{HUB_HANDOFF}"
    return f"{invocation}\n\n{body}"


def _write_scoped_mcp_config(run_id: int, cwd: str, names: List[str]) -> Optional[str]:
    """Write a per-run mcp config containing only the picked servers."""
    if not names:
        return None
    config = mcps_mod.build_scoped_config(names, cwd)
    path = TMP_MCP_DIR / f"run-{run_id}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600, secrets inside
    except OSError:
        pass
    return str(path)


def _log(run_id: int, stream: str, line: str) -> None:
    db.exec_(
        "INSERT INTO run_logs (run_id, ts, stream, line) VALUES (?, ?, ?, ?)",
        (run_id, db.now(), stream, line[:8000]),
    )


def _effective_allowed_tools(extra: Optional[List[str]]) -> List[str]:
    """Merge ambient defaults (git/gh access) with per-agent extras, dedup,
    preserve order. Empty patterns are dropped."""
    seen: set = set()
    out: List[str] = []
    for pat in list(DEFAULT_ALLOWED_TOOLS) + list(extra or []):
        s = str(pat).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def start_run(
    skill_name: str,
    skill_kind: str,
    working_directory: str,
    user_prompt: str,
    repo_id: Optional[int] = None,
    permission_mode: Optional[str] = None,
    schedule_id: Optional[int] = None,
    allowed_mcps: Optional[List[str]] = None,
    model: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    timeout_seconds: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    extra_allowed_tools: Optional[List[str]] = None,
    attempt_number: int = 1,
    parent_run_id: Optional[int] = None,
    step_position: int = 0,
    sync: bool = False,
) -> int:
    """Start a single run. If sync=True, block until it finishes (used by chains).
    Otherwise spawn a daemon thread and return immediately."""
    skill = find_skill(skill_name, skill_kind)
    if skill is None:
        raise ValueError(f"Unknown skill: {skill_name} ({skill_kind})")

    wd = Path(working_directory).expanduser().resolve()
    if not wd.exists() or not wd.is_dir():
        raise ValueError(f"Working directory does not exist: {wd}")

    pmode = permission_mode or DEFAULT_PERMISSION_MODE
    if pmode not in ALLOWED_PERMISSION_MODES:
        raise ValueError(f"Invalid permission mode: {pmode}")

    mcps_list = list(allowed_mcps or [])
    mcps_json = json.dumps(mcps_list) if mcps_list else None
    effective_model = (model or DEFAULT_MODEL) or None

    tools_effective = _effective_allowed_tools(extra_allowed_tools)
    tools_json = json.dumps(tools_effective) if tools_effective else None

    # Autopilot agents skip the draft-first HUB_HANDOFF — they're trusted to
    # call outbound tools (Slack MCP, gh) directly.
    include_handoff = True
    if schedule_id is not None:
        row = db.q1("SELECT autopilot FROM schedules WHERE id=?", (schedule_id,))
        if row and row["autopilot"]:
            include_handoff = False

    prompt = build_prompt(skill_name, skill_kind, user_prompt, include_handoff=include_handoff)
    run_id = db.exec_(
        """INSERT INTO runs
           (skill_name, skill_kind, repo_id, working_directory, prompt,
            status, started_at, schedule_id, allowed_mcps, model,
            attempt_number, parent_run_id, step_position, allowed_tools)
           VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (skill_name, skill_kind, repo_id, str(wd), prompt,
         db.now(), schedule_id, mcps_json, effective_model,
         int(attempt_number), parent_run_id, int(step_position), tools_json),
    )

    mcp_config_path = _write_scoped_mcp_config(run_id, str(wd), mcps_list)

    exec_kwargs = dict(
        model=effective_model,
        max_turns=max_turns,
        max_cost_usd=max_cost_usd,
        timeout_seconds=timeout_seconds,
        env_vars=env_vars or {},
        schedule_id=schedule_id,
        attempt_number=attempt_number,
        allowed_tools=tools_effective,
    )

    if sync:
        _execute(run_id, str(wd), prompt, pmode, mcp_config_path, **exec_kwargs)
    else:
        t = threading.Thread(
            target=_execute,
            args=(run_id, str(wd), prompt, pmode, mcp_config_path),
            kwargs=exec_kwargs,
            daemon=True,
        )
        t.start()
    return run_id


def start_chain(
    schedule_id: int,
    working_directory: str,
    steps: List[dict],
    repo_id: Optional[int] = None,
    permission_mode: Optional[str] = None,
    allowed_mcps: Optional[List[str]] = None,
    model: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    timeout_seconds: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    extra_allowed_tools: Optional[List[str]] = None,
    attempt_number: int = 1,
    **_ignored,
) -> None:
    """Run a sequence of steps synchronously in a background thread.
    Each step is a dict: {skill_name, skill_kind, prompt, continue_on_error}.
    Runs are linked via parent_run_id — the first step is the parent."""
    if not steps:
        return

    def worker():
        parent_id: Optional[int] = None
        for pos, step in enumerate(steps):
            try:
                run_id = start_run(
                    skill_name=step["skill_name"],
                    skill_kind=step["skill_kind"],
                    working_directory=working_directory,
                    user_prompt=step.get("prompt", ""),
                    repo_id=repo_id,
                    permission_mode=permission_mode,
                    schedule_id=schedule_id,
                    allowed_mcps=allowed_mcps,
                    model=model,
                    max_turns=max_turns,
                    max_cost_usd=max_cost_usd,
                    timeout_seconds=timeout_seconds,
                    env_vars=env_vars,
                    extra_allowed_tools=extra_allowed_tools,
                    attempt_number=attempt_number,
                    parent_run_id=parent_id,
                    step_position=pos,
                    sync=True,
                )
            except ValueError as e:
                # Log at the chain level and stop.
                _log_orphan(schedule_id, f"chain aborted: {e}")
                return

            if parent_id is None:
                parent_id = run_id

            row = db.q1("SELECT status FROM runs WHERE id=?", (run_id,))
            status = row["status"] if row else "failed"
            if status == "failed" and not step.get("continue_on_error", False):
                _log(parent_id or run_id, "system",
                     f"chain stopped after step {pos + 1}/{len(steps)} failed")
                return

    threading.Thread(target=worker, daemon=True).start()


def kill_run(run_id: int) -> dict:
    """Terminate a running run by killing its process group.
    Returns {killed, was_alive, error?}. Idempotent."""
    row = db.q1("SELECT id, status, pid FROM runs WHERE id=?", (run_id,))
    if row is None:
        return {"killed": False, "error": "run not found"}
    if row["status"] not in ("queued", "running"):
        return {"killed": False, "error": f"run already {row['status']}"}
    pid = row["pid"]
    if not pid:
        # queued but not yet spawned — just mark it stopped so it never runs.
        db.exec_(
            "UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?",
            ("killed by user before start", db.now(), run_id),
        )
        _log(run_id, "system", "⛔ killed by user (never spawned)")
        return {"killed": True, "was_alive": False}

    was_alive = False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
            was_alive = True
            _log(run_id, "system", f"⛔ SIGTERM sent to process group {pgid}")
        except (ProcessLookupError, PermissionError) as e:
            _log(run_id, "system", f"could not SIGTERM: {e}")

        # Give it 3 seconds, then hard kill any survivors.
        def hard_kill():
            try:
                os.killpg(pgid, signal.SIGKILL)
                _log(run_id, "system", f"⛔ SIGKILL sent to process group {pgid}")
            except (ProcessLookupError, PermissionError):
                pass

        threading.Timer(3.0, hard_kill).start()

    # Update status immediately so the UI reflects the kill.
    # (_execute() will later ALSO write the final row when its wait() unblocks —
    # its update wins the race with a proper exit_code, but we set a clear
    # error message here in case it doesn't return.)
    db.exec_(
        "UPDATE runs SET status='failed', error=? WHERE id=? AND status IN ('queued','running')",
        ("killed by user", run_id),
    )
    return {"killed": True, "was_alive": was_alive}


def _log_orphan(schedule_id: int, msg: str) -> None:
    """Chain-level log line when we couldn't even create a run row."""
    # Attach it to the most recent run for the schedule if possible,
    # otherwise fall back to a stray line under run_id=0 (harmless).
    row = db.q1(
        "SELECT id FROM runs WHERE schedule_id=? ORDER BY id DESC LIMIT 1",
        (schedule_id,),
    )
    _log(row["id"] if row else 0, "system", msg)


def _execute(
    run_id: int,
    cwd: str,
    prompt: str,
    permission_mode: str,
    mcp_config_path: Optional[str] = None,
    *,
    model: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_cost_usd: Optional[float] = None,
    timeout_seconds: Optional[int] = None,
    env_vars: Optional[Dict[str, str]] = None,
    schedule_id: Optional[int] = None,
    attempt_number: int = 1,
    allowed_tools: Optional[List[str]] = None,
) -> None:
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", permission_mode,
    ]
    if model:
        cmd += ["--model", model]
    if max_turns and max_turns > 0:
        cmd += ["--max-turns", str(int(max_turns))]
    if allowed_tools:
        # Claude Code accepts a single comma-separated value for --allowed-tools.
        cmd += ["--allowed-tools", ",".join(allowed_tools)]
    if mcp_config_path:
        cmd += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]

    env = os.environ.copy()
    env.setdefault("CLAUDE_CODE_QUIET", "1")
    if env_vars:
        for k, v in env_vars.items():
            if k:
                env[str(k)] = str(v)

    # Fresh process group so we can kill the whole tree on timeout.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
    except FileNotFoundError as e:
        db.exec_(
            "UPDATE runs SET status='failed', error=?, finished_at=? WHERE id=?",
            (f"claude binary not found: {e}", db.now(), run_id),
        )
        return

    db.exec_("UPDATE runs SET status='running', pid=? WHERE id=?", (proc.pid, run_id))
    _log(run_id, "system", f"$ {' '.join(cmd)}")
    _log(run_id, "system", f"cwd={cwd}")
    if attempt_number > 1:
        _log(run_id, "system", f"attempt {attempt_number}")
    if env_vars:
        _log(run_id, "system", f"extra env keys: {sorted(env_vars.keys())}")

    stderr_lines: list[str] = []
    captured_cost: dict = {"cost": None}
    state = {"timed_out": False}

    def kill_tree(sig: int = signal.SIGTERM) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            pass

    timer: Optional[threading.Timer] = None
    if timeout_seconds and timeout_seconds > 0:
        def on_timeout():
            if proc.poll() is None:
                state["timed_out"] = True
                _log(run_id, "system", f"⏱ timeout after {timeout_seconds}s — killing process")
                kill_tree(signal.SIGTERM)
                # Give it a moment, then hard-kill.
                threading.Timer(3.0, lambda: kill_tree(signal.SIGKILL)).start()
        timer = threading.Timer(float(timeout_seconds), on_timeout)
        timer.daemon = True
        timer.start()

    def pump_stderr():
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line.rstrip())
            _log(run_id, "stderr", line.rstrip())

    err_thread = threading.Thread(target=pump_stderr, daemon=True)
    err_thread.start()

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            raw = raw.rstrip()
            if not raw:
                continue
            summary = _summarize_stream_json(raw, captured_cost)
            _log(run_id, "stdout", summary or raw)
    except Exception as e:  # noqa: BLE001
        _log(run_id, "system", f"stream error: {e}")

    exit_code = proc.wait()
    err_thread.join(timeout=2)
    if timer is not None:
        timer.cancel()

    timed_out = state["timed_out"]
    if timed_out:
        status = "failed"
        error = f"timed out after {timeout_seconds}s"
    elif exit_code == 0:
        status = "completed"
        error = None
    else:
        status = "failed"
        error = "\n".join(stderr_lines[-20:]) or f"exit={exit_code}"

    cost = captured_cost["cost"]
    if cost is not None and max_cost_usd and cost > float(max_cost_usd):
        _log(run_id, "system",
             f"⚠ cost ${cost:.4f} exceeded cap ${float(max_cost_usd):.4f}")

    db.exec_(
        """UPDATE runs
           SET status=?, exit_code=?, error=?, finished_at=?,
               total_cost_usd=?, timed_out=?
           WHERE id=?""",
        (status, exit_code, error, db.now(), cost, 1 if timed_out else 0, run_id),
    )

    # Approvals scan (with autopilot honouring inside scan_inbox).
    try:
        found = approvals_mod.scan_inbox(run_id, cwd, schedule_id=schedule_id)
        if found:
            _log(run_id, "system", f"queued {found} approval(s) from .agent-hub-inbox/")
    except Exception as e:  # noqa: BLE001
        _log(run_id, "system", f"approvals scan error: {e}")

    # Failure notification hook (creates a slack-message approval).
    if status == "failed" and schedule_id is not None:
        try:
            approvals_mod.maybe_notify_failure(schedule_id, run_id, error or "unknown error")
        except Exception as e:  # noqa: BLE001
            _log(run_id, "system", f"failure notify error: {e}")

    # Retry policy — driven off the schedule row so ad-hoc runs don't retry.
    if status == "failed" and schedule_id is not None:
        try:
            _maybe_retry(run_id, schedule_id, attempt_number)
        except Exception as e:  # noqa: BLE001
            _log(run_id, "system", f"retry scheduling error: {e}")

    if mcp_config_path:
        try:
            os.remove(mcp_config_path)
        except OSError:
            pass


def _maybe_retry(run_id: int, schedule_id: int, attempt_number: int) -> None:
    row = db.q1("SELECT * FROM schedules WHERE id=?", (schedule_id,))
    if row is None:
        return
    retry_count = int(row["retry_count"] or 0)
    if retry_count <= 0 or attempt_number > retry_count:
        return
    delay = int(row["retry_delay_seconds"] or 60)

    # We defer the actual retry to APScheduler to avoid tying up this thread.
    from . import scheduler as sched_mod  # local import avoids circular ref
    sched_mod.schedule_one_shot_retry(schedule_id, attempt_number + 1, delay)
    _log(run_id, "system",
         f"↻ scheduled retry {attempt_number + 1}/{retry_count + 1} in {delay}s")


def _summarize_stream_json(raw: str, captured_cost: Optional[dict] = None) -> Optional[str]:
    """Turn a stream-json event into a compact one-line log entry."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    ev = obj.get("type", "")
    if ev == "result" and captured_cost is not None:
        try:
            captured_cost["cost"] = float(obj.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    if ev == "assistant":
        msg = obj.get("message", {}) or {}
        parts = []
        for block in msg.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool = block.get("name", "?")
                parts.append(f"[tool:{tool}]")
        text = " ".join(p for p in parts if p).strip()
        if text:
            return f"assistant: {text[:1500]}"
        return "assistant: (empty)"
    if ev == "user":
        msg = obj.get("message", {}) or {}
        parts = []
        for block in msg.get("content", []) or []:
            if block.get("type") == "tool_result":
                out = block.get("content", "")
                if isinstance(out, list):
                    out = " ".join(
                        b.get("text", "") if isinstance(b, dict) else str(b)
                        for b in out
                    )
                parts.append(f"tool_result: {str(out)[:800]}")
        text = " | ".join(p for p in parts if p).strip()
        if text:
            return text
        return None
    if ev == "result":
        return f"result: {obj.get('subtype', '')} cost=${obj.get('total_cost_usd', 0):.4f}"
    if ev == "system":
        return f"system: {obj.get('subtype', '')}"
    return None
