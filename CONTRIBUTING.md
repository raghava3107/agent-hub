# Contributing

Thanks for helping improve Agent Hub.

1. Open an issue describing the problem or proposed change.
2. Keep changes focused and avoid committing local configuration, databases,
   virtual environments, logs, MCP exports, or credentials.
3. Run the checks below before opening a pull request:

```bash
python3 -m compileall -q app
bash -n run.sh bin/service.sh bin/sync-skills.sh
git diff --check
```

Changes that affect process execution, permissions, MCP configuration, or
approval handling should include a security rationale and documentation.
