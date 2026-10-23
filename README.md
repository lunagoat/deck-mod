# deck for Claude Code

Delegate work from Claude Code to Codex, opencode, Antigravity (`agy`), Copilot and any ACP agent (Gemini CLI, Cursor, Qwen Code, Cline, Kiro, Goose, Kilo, Auggie, Vibe, Junie; untested), and watch them in a pane
beside the conversation. A rewrite of [deck](https://github.com/lunagoat/deck) as a Claude Code plugin: Claude Code
is the primary, deck's adapters run the others.

> **⚠️ Vibecoded.** This plugin was written almost entirely by an AI coding agent (Claude), steered by a human who
> tested it by hand. Nobody has audited the code. It runs other agents that can edit files and run commands on your
> machine, so read what it does before you trust it. Experimental, Linux only, and expect rough edges.
>
> Tested in real sessions with Codex, agy and Copilot; opencode follows the same path but is less exercised.

## Install

```
/plugin install deck --marketplace lunagoat/deck-mod
```

(or `claude plugin install deck --marketplace lunagoat/deck-mod` from a shell). Not published yet? Run it from a checkout:
`claude --plugin-dir /path/to/deck-mod`.

**You need:** Linux, Python 3.11+ (no pip packages), Claude Code 2.1.306 or newer, at least one of the agent CLIs
installed and logged in, and [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) for opencode, agy and
Copilot, which deck fences in (see Safety). The pane is a mod; Claude Code asks once before turning it on.

## Use it

Ask Claude: *"delegate a review of this diff to codex"*, or *"get agy and copilot to each write a paragraph"*.

- Claude gets the tools `agents`, `delegate`, `wait`, `poll`, `reply` and `stop`.
- `delegate` returns at once. When a delegate finishes and Claude hasn't read the result, deck wakes Claude with a
  message so it picks the answer up by itself (at most 10 wakes per session).
- **Pane:** opens by itself once the first delegate starts if the terminal is 144+ columns wide; otherwise type
  `/deck`. One tab per delegate with its live output, plus an overview tab with usage bars. The status line and a
  toast also report when delegates finish.
- Delegates work in the current folder, so don't give two of them overlapping files.

## Safety

deck never passes a skip-permissions flag. Each agent keeps its own permission rules, and when one asks, the question
reaches you as a native Claude Code dialog (Accept allows, Decline denies; some offer "always allow for this run").
A client that can't show the dialog gets a deny.

opencode, agy and Copilot also run inside a bubblewrap fence: the filesystem is read-only except the working folder and
the agent's own state directory. An agent that needs to write elsewhere will fail there. Set `rm_guard = true` in
`~/.config/deck/config.toml` to make `rm`, `rmdir`, `sudo` and similar ask first (off by default: agents are trusted to
ask for what they consider risky). Codex uses its own `on-request` policy and workspace sandbox.

## Config

`~/.config/deck/config.toml` (all optional):

```toml
only = []                                      # empty: every installed agent except claude; or list the ones to offer
max_delegations = 8                              # per task
max_depth = 2                                    # a delegate may delegate once more
rm_guard = false

[agents.myagent]                                 # any ACP agent
acp = ["myagent", "--acp"]
```

## Development

```
python3 tests/smoke_mcp.py        # MCP server against a fake agent
claude plugin test .              # the mod
claude plugin validate .
```

How it fits together: `daemon/deck-mcp` is an MCP server that runs deck's Hub in-process and writes a snapshot of
every delegate to `~/.local/state/deck/live/<pid>.json` about once a second. `hooks/register.tsx` reads that file to draw
the pane and to wake Claude. `daemon/deck/` is the Hub and adapters, copied from deck.

MIT licensed.
