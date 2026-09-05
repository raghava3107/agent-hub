from pathlib import Path
import json
import os
import shutil

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "hub.db"

HOME = Path.home()
USER_SKILLS_DIR = HOME / ".claude" / "skills"
USER_COMMANDS_DIR = HOME / ".claude" / "commands"

# Local machine configuration belongs to Agent Hub now. The managed skills
# repository is configured separately, so Agent Hub can live in its own repo.
CONFIG_PATH = Path(os.environ.get("HUB_CONFIG_PATH", APP_ROOT / "config" / "local.json")).expanduser()
try:
    _config = json.loads(CONFIG_PATH.read_text())
except (FileNotFoundError, json.JSONDecodeError):
    _config = {}
_agent_hub_config = _config.get("agent_hub", {})
_skills_config = _config.get("skills", {})

MANAGED_REPO = Path(
    os.environ.get("HUB_MANAGED_REPO")
    or _skills_config.get("managed_repo")
    or ""
).expanduser()
if not MANAGED_REPO:
    MANAGED_REPO = APP_ROOT
MANAGED_REPO = MANAGED_REPO.resolve()

REPO_SKILLS_DIR = Path(
    os.environ.get("HUB_REPO_SKILLS_DIR")
    or _skills_config.get("skills_dir")
    or MANAGED_REPO / "claude" / "skills"
).expanduser()
REPO_COMMANDS_DIR = Path(
    os.environ.get("HUB_REPO_COMMANDS_DIR")
    or _skills_config.get("commands_dir")
    or MANAGED_REPO / "claude" / "commands"
).expanduser()

PLIST_LABEL_PREFIX = _agent_hub_config.get("plist_label_prefix", "com.myagents")
AGENT_HUB_HOSTNAME = _agent_hub_config.get("hostname", "localhost")
AGENT_HUB_PORT = _agent_hub_config.get("port", 8765)

SKILL_SOURCES = [
    ("user-skill", USER_SKILLS_DIR),
    ("user-command", USER_COMMANDS_DIR),
    ("repo-skill", REPO_SKILLS_DIR),
    ("repo-command", REPO_COMMANDS_DIR),
]

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"

DEFAULT_MODEL = os.environ.get("HUB_MODEL", "")
DEFAULT_PERMISSION_MODE = os.environ.get("HUB_PERMISSION_MODE", "acceptEdits")

# Ambient tool patterns granted to EVERY run (in addition to what the agent's
# extra_allowed_tools adds). Passed to claude as --allowed-tools flags. Lets
# skills run git/gh unattended without bypassPermissions.
# Override via env: HUB_DEFAULT_ALLOWED_TOOLS="Bash(git *),Bash(gh *),Bash(ls)"
_default_tools_env = os.environ.get("HUB_DEFAULT_ALLOWED_TOOLS", "").strip()
if _default_tools_env:
    DEFAULT_ALLOWED_TOOLS = [t.strip() for t in _default_tools_env.split(",") if t.strip()]
else:
    DEFAULT_ALLOWED_TOOLS = [
        "Bash(git *)",   # git status/log/diff/branch/add/commit/push/… all fine
        "Bash(gh *)",    # gh pr/issue/api/repo/… all fine
    ]

DATA_DIR.mkdir(parents=True, exist_ok=True)
