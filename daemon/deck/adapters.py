"""One adapter per agent CLI: how to launch it headless and how to read its output."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import detect

BIN = Path(__file__).resolve().parent.parent / "bin"
PERMISSION_MCP = BIN / "deck-permission-mcp"


@dataclass
class Ev:
    """A parsed output event. kind: text | thought | tool | tool_output | info | error | done."""
    kind: str
    text: str = ""
    meta: dict = field(default_factory=dict)  # session, tokens, cost


def _short(s: str, n: int = 110) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_summary(name: str, inp) -> str:
    if isinstance(inp, dict):
        for k in ("command", "CommandLine", "file_path", "TargetFile", "AbsolutePath", "path",
                  "pattern", "Query", "url", "query", "description"):
            if k in inp:
                return f"{name}: {inp[k] if k in ('command', 'CommandLine') else _short(inp[k])}"
        if inp:
            return f"{name}: {_short(next(iter(inp.values())))}"
    return f"{name}: {_short(inp)}" if inp else name


# ---- parsers: line -> [Ev] -------------------------------------------------

def parse_claude(d: dict) -> list[Ev]:
    t = d.get("type")
    if t == "user":
        out = []
        for b in d.get("message", {}).get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                content = b.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
                if isinstance(content, str) and content:
                    out.append(Ev("tool_output", content))
        return out
    if t == "assistant":
        out = []
        for b in d.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(Ev("text", b["text"]))
            elif b.get("type") == "thinking" and b.get("thinking"):
                out.append(Ev("thought", b["thinking"]))
            elif b.get("type") == "tool_use" and b.get("name") == "ExitPlanMode":
                out.append(Ev("text", str(b.get("input", {}).get("plan", "(plan ready)"))))  # /plan's answer
            elif b.get("type") == "tool_use":
                out.append(Ev("tool", _tool_summary(b.get("name", "tool"), b.get("input"))))
        return out
    if t == "system" and d.get("subtype") == "init":
        return [Ev("info", "", {"session": d.get("session_id")})]
    if t == "system" and d.get("subtype") == "compact_boundary":  # /compact
        m = d.get("compact_metadata", {})
        return [Ev("text", f"── compacted: {m.get('pre_tokens', '?')} → {m.get('post_tokens', '?')} tokens")]
    if t == "rate_limit_event":  # 5-hour and 7-day utilisation, for the usage bars
        return [Ev("info", "", {"usage": d.get("rate_limit_info", {})})]
    if t == "result":
        u = d.get("usage", {})
        meta = {"session": d.get("session_id"), "cost": d.get("total_cost_usd"),
                "tokens": (u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                           + u.get("cache_read_input_tokens", 0) + u.get("output_tokens", 0))}
        return [Ev("error" if d.get("is_error") else "done", "", meta)]
    return []


def parse_opencode(d: dict, _seen: set | None = None) -> list[Ev]:
    t = d.get("type")
    p = d.get("part", {})
    sid = d.get("sessionID")
    meta = {"session": sid} if sid else {}
    if t == "text":
        return [Ev("text", p.get("text", ""), meta)]
    if t == "reasoning":
        return [Ev("thought", p.get("text", ""), meta)]
    if t == "tool_use" or p.get("type") == "tool":
        st = p.get("state", {})
        if st.get("status") in ("running", "completed", "pending") or t == "tool_use":
            return [Ev("tool", _tool_summary(p.get("tool", "tool"), st.get("input")), meta)]
    if t == "step_finish":
        tk = p.get("tokens", {})
        return [Ev("info", "", {**meta, "tokens": tk.get("total"), "cost": p.get("cost")})]
    if t == "error":
        return [Ev("error", _short(json.dumps(d.get("error", d)), 300))]
    return [Ev("info", "", meta)] if meta else []


def parse_agy(d: dict) -> list[Ev]:
    e = d.get("event")
    if e == "init":
        return [Ev("info", "", {"session": d.get("conversation_id")})]
    if e == "step_update":
        s = d["step_update"]
        ty = s.get("step_type", "")
        if ty == "agent_response":
            return [Ev("text", s["text_delta"], {"delta": True})] if s.get("text_delta") else []
        if ty == "tool" and s.get("state") == "ACTIVE":
            info = s.get("tool_info", {})
            name = s.get("tool_name") or info.get("name", "tool")
            return [Ev("tool", _tool_summary(name, info.get("parameters")), {"dedupe": s.get("step_index")})]
    if e == "result":
        r = d["result"]
        u = r.get("usage", {})
        meta = {"session": r.get("conversation_id"), "tokens": u.get("total_tokens")}
        return [Ev("done" if r.get("status") == "SUCCESS" else "error", r.get("status", ""), meta)]
    return []


def parse_plain(line: str) -> list[Ev]:
    return [Ev("text", line)]


# ---- adapters ---------------------------------------------------------------

@dataclass
class Adapter:
    name: str
    binary: str
    color: str
    blurb: str
    trust: str = ""  # shown in the pane header: how this agent's permissions are handled
    sandboxed: bool = False  # can't ask for itself, so deck fences it in (sandbox.py)
    acp: list[str] | None = None  # args that make this CLI speak ACP on stdio; deck then drives it via acp.py

    def available(self) -> bool:
        return detect.find(self.binary) is not None

    def cmd(self, prompt: str, cwd: str, session: str | None, *,
            model: str = "", effort: str = "", plan: bool = False, preamble: str = "", delegate: str = "") -> list[str]:
        raise NotImplementedError

    def acp_args(self, model: str) -> list[str]:
        """Flags for a model this agent only takes on its command line; empty when its ACP session offers one."""
        return []

    @staticmethod
    def with_preamble(prompt: str, preamble: str, session: str | None) -> str:
        """Agents without a system-prompt flag get the delegation note prepended once."""
        return f"{preamble}\n\n---\n{prompt}" if preamble and not session else prompt

    json_lines = True

    def parse(self, line: str) -> list[Ev]:
        if line.startswith("Reading additional input from stdin"):
            return []
        if not self.json_lines:
            return parse_plain(line)
        try:
            d = json.loads(line)
        except ValueError:
            return [Ev("text", line)] if line.strip() else []
        return self._parse(d)

    def _parse(self, d: dict) -> list[Ev]:
        return []


class Claude(Adapter):
    MODES = ("auto", "manual", "acceptEdits")  # claude's own permission modes; never bypassPermissions
    MODE = "auto"  # set from the claude_mode setting (config.toml or /config)

    def cmd(self, prompt, cwd, session, *, model="", effort="", plan=False, preamble="", delegate=""):
        # Claude's own permission system decides what needs asking (auto mode by default, the one you
        # use in the terminal). Whatever it would ask about goes to the `approve` tool of
        # bin/deck-permission-mcp, which puts a confirm/deny dialog in deck. deck adds no rules here.
        mcp = {"mcpServers": {"deck": {"command": "python3", "args": [str(PERMISSION_MCP)]}}}
        c = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
             "--permission-mode", "plan" if plan else self.MODE,
             "--mcp-config", json.dumps(mcp), "--permission-prompt-tool", "mcp__deck__approve"]
        if preamble:
            c += ["--append-system-prompt", preamble, "--allowedTools", f"Bash({delegate}:*)"]
        if model:
            c += ["--model", model]
        if effort:
            c += ["--effort", effort]
        return c + (["--resume", session] if session else [])

    def _parse(self, d):
        return parse_claude(d)


class Codex(Adapter):
    """Not launched through cmd(): codexserver.py drives `codex app-server`, which can ask deck for
    approval (`codex exec` can't ask, it just fails)."""


class OpenCode(Adapter):
    def cmd(self, prompt, cwd, session, *, model="", effort="", plan=False, preamble="", delegate=""):
        c = ["opencode", "run", "--standalone", "--format", "json"]  # standalone: honours cwd
        if plan:
            c += ["--agent", "plan"]
        if model:  # opencode has no effort flag; it's a variant suffix on the model
            c += ["-m", f"{model}#{effort}" if effort else model]
        if session:
            c += ["-s", session]
        return c + [self.with_preamble(prompt, preamble, session)]

    def _parse(self, d):
        return parse_opencode(d)


class Agy(Adapter):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._seen: set = set()

    def cmd(self, prompt, cwd, session, *, model="", effort="", plan=False, preamble="", delegate=""):
        c = ["agy", "-p", self.with_preamble(prompt, preamble, session), "--output-format", "stream-json"]
        if plan:  # note: agy's print mode reports permission_mode "always-proceed" whatever --mode says
            c += ["--mode", "plan"]
        if model:
            c += ["--model", model]
        if effort:
            c += ["--effort", effort]
        return c + (["--conversation", session] if session else [])

    def _parse(self, d):
        evs = parse_agy(d)
        out = []
        for e in evs:  # a step goes ACTIVE many times; show each step once
            key = e.meta.get("dedupe")
            if key is not None:
                if key in self._seen:
                    continue
                self._seen.add(key)
            out.append(e)
        return out


class AcpAgent(Adapter):
    """An agent deck drives over ACP. Built from ACP_KNOWN or a config.toml `[agents.x] acp = [...]`."""

    def __init__(self, name: str, binary: str, args: list[str], color: str, trust: str, sandboxed: bool = True,
                 blurb: str = "ACP agent"):
        super().__init__(name, binary, color, blurb, trust, sandboxed=sandboxed, acp=list(args))

    def cmd(self, prompt, cwd, session, **kw):
        raise NotImplementedError("ACP agents are launched by acp.py")


class Copilot(AcpAgent):
    """Driven over ACP (`copilot --acp`), because `copilot -p` has no way to ask anyone: headless it
    denies every tool call ("permission denied because no interactive user response was available"), so
    it can't edit, run or even read anything outside the folder. The one flag that makes `-p` useful,
    `--allow-all-tools`, skips its asking entirely (deck doesn't pass skip-permission flags), while ACP
    keeps copilot's own approvals and routes them to you, like opencode. Its session store is the same
    either way, so sessions started with `-p` still resume."""

    def __init__(self):
        super().__init__("copilot", "copilot", ["--acp"], "#c084fc",
                         "its own asks reach you; also fenced; rm etc. ask",
                         blurb="GitHub-flavoured tasks, quick edits")

    def acp_args(self, model: str) -> list[str]:
        return ["--model", model] if model else []  # its ACP session doesn't offer a model option


# Coding agents that can run as ACP servers (from their docs, via deck's own research; all UNTESTED here
# except opencode and copilot, which are built in). Offered only when installed, fenced in by default.
# binary may list alternatives with "|".
ACP_KNOWN = {
    "gemini": ("gemini", ["--acp"], "#4285f4"),
    "cursor": ("cursor-agent|agent", ["acp"], "#a1a1aa"),
    "qwen": ("qwen", ["--acp"], "#7c3aed"),
    "cline": ("cline", ["--acp"], "#22c55e"),
    "kiro": ("kiro-cli", ["acp"], "#9333ea"),
    "goose": ("goose", ["acp"], "#f59e0b"),
    "kilo": ("kilo", ["acp"], "#eab308"),
    "auggie": ("auggie", ["--acp"], "#06b6d4"),
    "vibe": ("vibe-acp", [], "#f97316"),
    "junie": ("junie", ["--acp=true"], "#ec4899"),
}


class Custom(Adapter):
    """Any other CLI, described in config.toml:

        [agents.gemini]
        command = ["gemini", "-p", "{prompt}"]     # {prompt} and {model} are filled in
        model_flag = ["--model", "{model}"]         # optional, added when you pick a model
        color = "#4285f4"                           # optional

    Output is shown as plain text and every run is fenced in (sandbox.py), because deck can't know how
    an arbitrary CLI asks for permission. There is no session to resume."""
    json_lines = False

    def __init__(self, name: str, spec: dict):
        command = [str(x) for x in spec["command"]]
        super().__init__(name, command[0], spec.get("color", "#9aa0a6"), "custom agent",
                         "fenced in; custom", sandboxed=True)
        self.template = command
        self.model_flag = [str(x) for x in spec.get("model_flag", [])]

    def cmd(self, prompt, cwd, session, *, model="", effort="", plan=False, preamble="", delegate=""):
        full = self.with_preamble(("PLAN ONLY, change nothing: " if plan else "") + prompt, preamble, session)
        fill = lambda s: s.replace("{prompt}", full).replace("{model}", model)  # noqa: E731
        c = [fill(x) for x in self.template]
        return c + ([fill(x) for x in self.model_flag] if model and self.model_flag else [])


CUSTOM_SPECS: dict[str, dict] = {}  # filled from config.toml [agents.*] by core.load_config
BUILTIN = ["claude", "codex", "opencode", "agy", "copilot"]


def register_custom(specs: dict[str, dict]) -> list[str]:
    """Add the config's [agents.*] to the registry. Returns problems, one line each."""
    problems = []
    CUSTOM_SPECS.clear()
    for name, spec in specs.items():
        if name in BUILTIN:
            problems.append(f"[agents.{name}] ignored: {name} is built in")
        elif not isinstance(spec, dict) or not (spec.get("command") or spec.get("acp")):
            problems.append(f"[agents.{name}] ignored: it needs command = [...] or acp = [...]")
        else:
            CUSTOM_SPECS[name] = spec
    ORDER[:] = BUILTIN + [n for n in ACP_KNOWN if n not in CUSTOM_SPECS] + list(CUSTOM_SPECS)
    return problems


def make_adapters() -> dict[str, Adapter]:
    """Fresh adapters per run (Agy keeps per-run dedupe state)."""
    known = {n: AcpAgent(n, b, a, c, "ACP: asks you; fenced; untested") for n, (b, a, c) in ACP_KNOWN.items()
             if n not in CUSTOM_SPECS}
    custom = {}
    for n, spec in CUSTOM_SPECS.items():
        if spec.get("acp"):  # [agents.x] acp = ["binary", "--acp"]
            argv = [str(x) for x in spec["acp"]]
            fenced = bool(spec.get("fenced", True))
            custom[n] = AcpAgent(n, argv[0], argv[1:], spec.get("color", "#9aa0a6"),
                                 "ACP: asks you" + ("; fenced" if fenced else "; NOT fenced") + "; custom", fenced)
        else:
            custom[n] = Custom(n, spec)
    return {**_builtin(), **known, **custom}


def _builtin() -> dict[str, Adapter]:
    return {
        "claude": Claude("claude", "claude", "#d97757", "reasoning, review, design, long refactors",
                         "its own rules, you answer"),
        "codex": Codex("codex", "codex", "#10a37f", "focused edits, bug fixes, tests",
                       "its own rules, you answer"),
        "opencode": OpenCode("opencode", "opencode", "#6d8bff", "model-flexible second opinion",
                             "ACP: asks you; fenced; rm etc. ask", sandboxed=True, acp=["acp"]),
        "agy": Agy("agy", "agy", "#4285f4", "frontend, browser tasks, UI",
                   "fenced in; rm etc. ask", sandboxed=True),
        "copilot": Copilot(),
    }


# effort levels each CLI accepts (copilot has no effort flag)
EFFORTS = {
    "claude": ["low", "medium", "high", "xhigh", "max"],
    "codex": ["low", "medium", "high", "xhigh", "max", "ultra"],
    "agy": ["low", "medium", "high", "xhigh", "max"],
    "opencode": ["low", "medium", "high", "max", "minimal"],  # variants vary by model; needs a model set
}
ORDER = list(BUILTIN)  # grows with the config's [agents.*]; see register_custom
