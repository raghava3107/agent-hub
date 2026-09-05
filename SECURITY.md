# Security policy

## Supported versions

Only the latest `main` branch is currently supported.

## Scope

Agent Hub is a local, single-user application. It is not safe to expose the
development server or LaunchAgent directly to the public internet: the
application can execute `claude` in registered directories and has no built-in
authentication or tenant isolation.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use a GitHub
private vulnerability report for this repository when enabled. Include a
reproduction, affected version or commit, impact, and any suggested fix.

Until a fix is available, stop the service and keep it bound to loopback.
