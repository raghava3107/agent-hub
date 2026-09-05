# Operating model

Agent Hub is easiest to understand as a small control plane for local agent
runs. The current release is intentionally single-node; it is cluster-friendly
in its boundaries and terminology, but it is not a multi-node service yet.

```text
Browser
  │ HTTP (loopback)
  ▼
Agent Hub control plane
  ├── FastAPI UI + API
  ├── APScheduler (one scheduler leader)
  ├── SQLite state (agents, runs, approvals, logs)
  └── Run supervisor
        └── claude subprocess
              ├── managed repository / workspace
              ├── selected MCP servers
              └── .agent-hub-inbox/ approval artifacts
```

## What is supported today

- One trusted user and one Hub process per machine.
- Multiple agents on that machine.
- Multiple registered repositories/workspaces.
- Concurrent local runs, subject to the machine's CPU, memory, and Claude/MCP limits.
- A persistent SQLite database at `data/hub.db`.

## What “cluster-friendly” means here

Use stable names, explicit workspace ownership, and one scheduler leader:

| Concept | Naming pattern | Example |
|---|---|---|
| Agent | `<domain>-<action>-<cadence>` | `release-watch-hourly` |
| Run | `run-<numeric-id>` | `run-2048` |
| Workspace | `<team>-<product>-<purpose>` | `acme-web-review` |
| MCP scope | short capability names | `github`, `slack`, `jira` |

Do not point two Hub processes at the same SQLite database. The in-process
scheduler is not a distributed leader-election system, and two instances could
fire the same scheduled agent twice.

## Path to a real multi-node deployment

Before running shared workers or public tenants, replace the local assumptions
with:

1. Postgres or another server-grade state store.
2. A distributed scheduler with leader election.
3. A queue-backed worker pool with per-run isolation.
4. Authentication, authorization, tenant/workspace boundaries, and quotas.
5. Sandboxed execution and a managed secret store.
6. Centralized logs, metrics, tracing, and audit events.

Until those pieces exist, deploy one isolated Hub per trusted machine or
workspace and keep the HTTP bind on `127.0.0.1`.
