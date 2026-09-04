# AAE Claude Code Adapter Instructions

This repository owns Claude Code-specific projection and verification only.
Portable AAE behavior belongs in `adaptive-agentic-engineering`.

- Check current official Anthropic documentation before changing a native surface.
- Update `capabilities.json` and tests together.
- Preserve Claude Code's native instructions, skills, hooks, subagents, MCP,
  plugins, permissions, and tool controls.
- Never persist raw hook prompts, tool responses, transcripts, or file contents.
- Do not claim a version verified until its probes and tests pass.
