from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


HOME = Path.home()
USER_CLAUDE_JSON = HOME / ".claude.json"
USER_SETTINGS = HOME / ".claude" / "settings.json"
USER_CLAUDE_DIR = HOME / ".claude"

# Extra settings variants users maintain alongside settings.json (e.g. the
# rr-context-mode split of settings.full.json / settings.lean.json — the app
# still knows how to work with any MCP declared there, because at run-time we
# generate an explicit --mcp-config from the exact server dict we found).
EXTRA_SETTINGS_PATTERNS = ("settings.full.json", "settings.local.json")


@dataclass
class Mcp:
    name: str
    source: str            # human-readable origin ("user runtime", "user settings", "project .mcp.json")
    kind: str              # "stdio" | "http" | "sse" | "unknown"
    endpoint: str          # command line (stdio) or url (http/sse)
    raw: dict              # original server dict — we pass this straight through when generating configs

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "endpoint": self.endpoint,
        }


def _classify(server: dict) -> tuple[str, str]:
    """Return (kind, endpoint) from a server config dict."""
    url = server.get("url")
    if url:
        transport = str(server.get("type") or server.get("transport") or "").lower()
        if "sse" in transport:
            return "sse", url
        return "http", url
    cmd = server.get("command")
    if cmd:
        args = server.get("args") or []
        return "stdio", " ".join([str(cmd), *[str(a) for a in args]])
    return "unknown", ""


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _harvest(source_label: str, data: Optional[dict], out: Dict[str, Mcp]) -> None:
    if not data:
        return
    servers = data.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        if name in out:
            # earlier source wins; still remember the extra source
            out[name].source = f"{out[name].source}, {source_label}"
            continue
        kind, endpoint = _classify(server)
        out[name] = Mcp(
            name=name,
            source=source_label,
            kind=kind,
            endpoint=endpoint,
            raw=server,
        )


def discover_mcps(cwd: Optional[str] = None) -> List[Mcp]:
    """Return every MCP visible in the config chain, deduped by name.
    User runtime (~/.claude.json) has priority since that's what claude
    actually consults at start-up.

    Also scans extra settings variants (e.g. settings.full.json — the "master
    list" people keep for switch-mode workflows) so MCPs that aren't currently
    loaded still appear in the picker. When we run an agent scoped to one of
    those, --mcp-config --strict-mcp-config lets claude start it fresh from
    the exact server dict we found — no need for it to be pre-loaded."""
    out: Dict[str, Mcp] = {}
    _harvest("user runtime", _read_json(USER_CLAUDE_JSON), out)
    _harvest("user settings", _read_json(USER_SETTINGS), out)

    # Optional extra settings variants under ~/.claude/
    for fname in EXTRA_SETTINGS_PATTERNS:
        p = USER_CLAUDE_DIR / fname
        if p.exists():
            _harvest(f"user {fname.replace('.json','')}", _read_json(p), out)

    if cwd:
        cwd_path = Path(cwd)
        _harvest("project .mcp.json", _read_json(cwd_path / ".mcp.json"), out)
        _harvest("project settings", _read_json(cwd_path / ".claude" / "settings.json"), out)
    return sorted(out.values(), key=lambda m: m.name.lower())


def build_scoped_config(names: List[str], cwd: Optional[str] = None) -> dict:
    """Return a dict shaped as {\"mcpServers\": {…}} containing only the
    requested MCP names, sourced from the same discovery chain. Suitable
    for writing to a temp file and passing via --mcp-config."""
    all_mcps = {m.name: m for m in discover_mcps(cwd)}
    picked = {}
    for n in names:
        if n in all_mcps:
            picked[n] = all_mcps[n].raw
    return {"mcpServers": picked}
