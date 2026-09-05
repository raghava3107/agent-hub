# Spidey — Deployment & Lifecycle

Everything about running the app long-term: sleep, reboot, background service,
battery cost, when to consider a server. Read once, decide later.

---

## TL;DR

- Right now (`./run.sh` in a terminal) works fine — but the app dies when you
  close the terminal or reboot.
- **For "always available" on this laptop:** install as a macOS LaunchAgent
  via `bin/service.sh install`. Native macOS mechanism. Set-and-forget.
- **For "keeps running even when idle":** wrap it in `caffeinate -i` so the
  laptop doesn't idle-sleep while agents are alive.
- **For "runs during sleep or when the laptop is closed":** you need a
  different machine. Physics — macOS suspends every user process during
  sleep. No app-level fix exists.

---

## The whole trade-off spectrum

| Approach | Sleep behavior | Reboot | Remote access | Local files | Cost | Complexity |
|---|---|---|---|---|---|---|
| `./run.sh` in a terminal (today's state) | Paused during sleep, resumes on wake | Manual restart | 127.0.0.1 only | ✅ | free | trivial |
| **launchd LaunchAgent** (canonical macOS way) | Paused during sleep, resumes on wake, restarts on crash | ✅ auto on login | 127.0.0.1 only | ✅ | free | tiny |
| launchd + `caffeinate -i` wrapper | Lid-open = keeps firing; still sleeps if you close lid or manually sleep | ✅ auto | 127.0.0.1 only | ✅ | small battery cost when unplugged | tiny |
| launchd + Tailscale | Same as above | ✅ auto | phone/other Macs via Tailscale | ✅ | free (personal) | small (install Tailscale) |
| Home server (spare Mac Mini / Raspberry Pi) | Never sleeps | ✅ | LAN + Tailscale | ⚠️ repos need to sync (git, Syncthing, or NFS mount) | one-time hardware | medium |
| Cloud VPS (Fly.io / Hetzner) | Never sleeps | ✅ | HTTPS from anywhere | ❌ repos aren't there | ~$5/mo | high — real rewrite |

---

## Sleep behavior — the honest picture

### What happens when your Mac sleeps

macOS suspends **every user process** during sleep — Spidey included.

Concretely:
1. Uvicorn's HTTP loop is frozen (no new requests handled)
2. APScheduler's timer thread is frozen (no fires happen)
3. Any running `claude` subprocess is frozen mid-thought
4. Any inflight MCP tool call is frozen

Nothing in the app can change this. It's kernel-level.

### What happens on wake

1. All the frozen processes resume from exactly where they left off
2. APScheduler notices it missed fire times
3. Because we set `coalesce=True` on all cron jobs, **one catch-up run** happens per missed schedule (not N runs for N missed intervals)
4. Normal cadence resumes from that point

**Example**: A 5-min agent sleeps for 2 hours (24 missed intervals). On wake,
APScheduler fires **one** run to make up for the gap, then the next fire is
5 min later, and normal.

### What NOT to do

- **Don't reboot** just because you woke the laptop. Nothing to reboot for. The
  process resumes automatically.
- **Don't manually restart the app.** Same reason.

### What to do if the app really isn't answering after wake

Rare, but if `http://127.0.0.1:8765` doesn't respond:

```bash
# Option 1: if you have the service installed
bin/service.sh restart

# Option 2: if you're running ./run.sh manually
ctrl-c in the terminal, then ./run.sh again
```

---

## Reboot behavior

### Without the LaunchAgent (today)

The app doesn't come back on reboot. You must:

```bash
cd agent-hub && ./run.sh
```

The terminal window must stay open. Close it → app dies.

### With the LaunchAgent installed (`bin/service.sh install`)

macOS auto-starts the app when you log in. No terminal required. Survives:

- Reboots
- Log-out / log-in
- App crashes (launchd auto-restarts within ~10s)
- You closing terminal windows
- `pkill uvicorn` — launchd notices and respawns

The only ways to stop it:

- `bin/service.sh stop` — temporarily stop, plist stays in place
- `bin/service.sh uninstall` — remove entirely

State is safe regardless (SQLite lives in `data/hub.db`). Agents auto-re-register
via `sched_mod.load_all_from_db()` on startup.

---

## What is a "LaunchAgent" (and why it's the canonical answer)

`launchd` is macOS's init system — the process that boots the OS and starts
every service. A **LaunchAgent** is a small plist file placed in
`~/Library/LaunchAgents/` telling launchd:

- What command to run
- When to run it (at login, on schedule, on file change, etc.)
- What to do if it exits (restart, ignore)
- Where to send logs

This is **not** a workaround. It's what runs:

- Homebrew services (`brew services start postgresql`)
- Docker Desktop
- VSCode's remote server
- Ollama, LM Studio
- Every background app that survives a reboot

Our plist looks like this (`Label` is `<agent_hub.plist_label_prefix from config/local.json>.hub`
— `com.rkota.rragents.hub` and the `/Users/rkota/...` paths below are this
example machine's actual values; `bin/service.sh install` generates yours
from `config/local.json` and your real repo path):

```xml
<key>Label</key><string>com.rkota.rragents.hub</string>
<key>ProgramArguments</key>
<array>
  <string>/bin/bash</string>
  <string>/Users/yourname/WorkSpace/agent-hub/run.sh</string>
</array>
<key>WorkingDirectory</key><string>/Users/yourname/WorkSpace/agent-hub</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>/tmp/rr-agents-hub.log</string>
<key>StandardErrorPath</key><string>/tmp/rr-agents-hub.log</string>
```

`bin/service.sh install` writes this file, loads it via `launchctl load`,
and waits for the app to answer on `:8765`.

---

## What is `caffeinate -i`

`caffeinate` is a built-in macOS command (`/usr/bin/caffeinate` — been in
macOS since Lion, 2011). Nothing to install. It creates a **power assertion**
that tells the OS "don't sleep for these reasons."

Flags:

| Flag | Meaning |
|---|---|
| `-d` | Prevent display sleep (screen stays lit) |
| `-i` | Prevent **idle** system sleep |
| `-m` | Prevent disk sleep |
| `-s` | Prevent sleep even on battery |
| `-u` | Assert user activity (screen-saver password reset, etc.) |
| `-t <sec>` | Assert for N seconds only |

### The one flag that matters for us: `-i`

`caffeinate -i <command>` runs `<command>` and prevents the "idle timeout"
sleep for the duration.

**What it stops:** the sleep triggered by "10 minutes with no keyboard/mouse
activity" (or whatever your Battery/Energy Saver setting is).

**What it does NOT stop:**

- Closing the lid (still sleeps unless external display + power in clamshell mode)
- Apple menu → Sleep (still sleeps)
- Pressing the power button
- `sudo pmset sleepnow`

### How this helps us

Without `caffeinate -i`:

```
You: use laptop → agents fire correctly
You: walk away 30 min → macOS idle-timer fires → sleep → agents PAUSE
You: back at desk → wake → 1 coalesced catch-up fire
```

With `caffeinate -i`:

```
You: use laptop → agents fire correctly
You: walk away 30 min → screen dims → laptop STAYS AWAKE → agents keep firing
You: back at desk → wiggle mouse → nothing was missed
```

### The trade-off

**Plugged in:** roughly zero cost. Idle awake M-series MacBooks draw ~3–5W
vs ~1W asleep. Difference is a few extra kWh per year at most. You won't
notice on your bill.

**On battery:** real cost. Battery drain when caffeinated is ~3× the drain
when asleep. A 60 Wh battery gets ~15 h caffeinated-idle vs ~60 h asleep.
If you close the lid it still sleeps, so this only matters when you leave
the laptop open on battery for long stretches.

### The smart wrapper

We can wrap the app so it only caffeinates when plugged in:

```bash
if pmset -g ps | grep -q "AC Power"; then
  exec caffeinate -i ./run.sh
else
  exec ./run.sh   # on battery? let macOS sleep normally
fi
```

Best of both worlds. Zero battery cost. Full agent uptime when plugged in.

(Not wired yet — will add before final install if you want.)

---

## Cost analysis (what installing this actually costs your laptop)

### RAM

| State | Memory usage |
|---|---|
| App idle (no agents firing) | ~40 MB |
| App + one claude subprocess running | ~250–450 MB |
| App + three concurrent runs | ~750 MB – 1.3 GB |

None of these are meaningful on a modern Mac (8–96 GB RAM).

### CPU

| State | CPU load |
|---|---|
| App idle | ~0% (event-loop waiting on epoll/kqueue) |
| Agent firing, LLM streaming | one core at 20–60% for 20–90 s per run |
| Idle between runs | back to ~0% |

### Battery drain per day (on battery, if the laptop stays open)

| Configuration | Extra drain |
|---|---|
| App idle, no scheduled agents | <0.5% / day |
| One hourly agent on Haiku | ~1% / day |
| Every-5-min agent on Haiku | ~3–5% / day |
| Every-2-min agent on Opus | ~15–25% / day |

Add ~2–3% baseline if `caffeinate -i` is on and laptop is idle-open on battery.

### Disk

- `data/hub.db` grows ~1–2 MB/day for active use
- `/tmp/rr-agents-hub.log` grows unbounded (should add log rotation)
- MCP config temp files: created per run, auto-deleted; sub-KB

Long-term, the log file is the only real concern. Fix: add a `newsyslog` rule
that rotates the file at 10 MB and keeps 3 backups.

### Anthropic API cost

**This is the real ongoing cost.** Not laptop resources, but token spend.

| Model | Per run (typical) | Every 5 min for a day |
|---|---|---|
| Haiku | ~$0.01–0.05 | $2.88–$14.40 |
| Sonnet | ~$0.05–0.20 | $14.40–$57.60 |
| Opus | ~$0.10–1.00 | $28.80–$288 |

**Match the model to the frequency.** Every-few-minutes agents should always
be on Haiku unless there's a real reason for more. Hourly-or-less agents can
use Sonnet. Opus is for one-offs or rare-tick agents.

---

## My concrete recommendation

For **the best local-first setup** on this Mac:

1. **Install as a LaunchAgent** — `bin/service.sh install`
   Rationale: canonical macOS pattern. Auto-start, auto-restart, log-file.
   No user cost beyond what the app already uses.

2. **Wrap with a smart `caffeinate -i`** — only active when plugged in
   Rationale: eliminates the "walked away from my desk" gap without any
   battery penalty when unplugged.

3. **Match models to frequency** — every-N-min = Haiku, hourly = Sonnet,
   long/complex = Opus. This is a config choice per-agent, not global.

4. **Add log rotation** — `newsyslog.d/rr-agents.conf` to cap
   `/tmp/rr-agents-hub.log` at 10 MB.

5. **Optionally add Tailscale** — if you ever want to trigger agents from
   your phone or another machine. Free, works out of the box.

**Skip until you have a real need:**

- Home server / VPS. Only necessary if you truly need runs during sleep,
  or a shared/team setup. Different architecture, different conversation.

---

## Setup checklist (when ready)

```bash
cd agent-hub

# 1. Verify the app runs manually first
./run.sh                          # ctrl-c after confirming http://127.0.0.1:8765

# 2. Install as a service
bin/service.sh install

# 3. Verify it's up
bin/service.sh status
open http://127.0.0.1:8765

# 4. (Optional but recommended) Add smart caffeinate wrapper
#    -- I'll wire this into service.sh in a follow-up

# 5. Watch a live run to confirm
open http://127.0.0.1:8765/agents/10  # the joke agent

# 6. From now on:
bin/service.sh status              # is it up?
bin/service.sh logs                # tail the log
bin/service.sh restart             # after code changes
bin/service.sh stop / uninstall    # if you want it off
```

---

## FAQ

**Q: My laptop was asleep for 2 hours. Do I need to reboot?**
No. Just wake it. The app resumes automatically. If you're using the LaunchAgent,
launchd also handles crashes and restarts for you. Reboot is never required
for Spidey specifically.

**Q: What if I close my laptop lid?**
System sleeps (unless external display + power + input device is attached for
clamshell mode). App pauses. Wake resumes it with one coalesced fire.

**Q: What if I want to reboot my Mac?**
Just reboot. With the LaunchAgent installed, the app comes back on login.
Without it, run `./run.sh` after login.

**Q: What if I close the terminal I ran `./run.sh` in?**
Without the LaunchAgent, the app dies (child of your shell). With it, doesn't
matter — launchd owns the process, not your shell.

**Q: Can two people share this on the same network?**
Not out of the box — the app binds to 127.0.0.1. To expose on LAN, change
`run.sh` to `--host 0.0.0.0`. But no auth exists; add one before exposing.

**Q: What if the LaunchAgent doesn't stop when I run uninstall?**
`launchctl unload` handles it, but if a process is stuck, run
`pkill -f "uvicorn app.main:app"`.

**Q: Where does log data live?**
- App stdout/stderr: `/tmp/rr-agents-hub.log`
- SQLite DB: `data/hub.db`
- Per-run temp MCP configs: `data/mcp-configs/run-<id>.json` (auto-cleaned)
- Skill/command sync log: `/tmp/rr-agents-sync.log`

**Q: Can I move `agent-hub/` to a different path after installing?**
No — the plist has the absolute path hard-coded. If you move the folder:
`bin/service.sh uninstall`, move, then `bin/service.sh install` again.

**Q: What breaks if I `rm -rf data/`?**
You lose all agents, runs, and approvals. The app auto-recreates a fresh
DB on next start. Skills, MCP configs, and code are safe (elsewhere).
