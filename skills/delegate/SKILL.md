---
name: delegate
description: Hand a self-contained subtask (review, second opinion, parallel edit) to another coding agent via the deck MCP tools. Use when the user names codex, opencode, agy or copilot, or asks for a second opinion.
---

1. Call `mcp__plugin_deck_deck__agents` to see who is installed.
2. Call `delegate` with a self-contained prompt (the agent has none of this conversation). Use `wait: true` for quick questions; otherwise keep working: deck sends you a message when a delegate finishes, then call `poll` to read it.
3. Delegates edit the same working tree. Don't hand two agents overlapping files.
4. Report each delegate's answer to the user, attributed by agent.
