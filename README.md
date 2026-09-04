# AAE Claude Code Adapter

Native Claude Code integration for [Adaptive Agentic Engineering](https://github.com/Wolfeitz/adaptive-agentic-engineering).

The adapter projects AAE onto Claude Code's native `CLAUDE.md`, project skills,
hooks, subagents, permissions, MCP, and plugin surfaces. It does not recreate
those mechanisms in AAE core.

```bash
python -m pip install -e ../adaptive-agentic-engineering -e .
aae-claude-code init /path/to/project
aae-claude-code doctor /path/to/project
```

Existing Claude configuration and hand-authored skills are preserved. The
capability manifest and scheduled latest-version check make upstream drift
visible before the adapter claims support.
