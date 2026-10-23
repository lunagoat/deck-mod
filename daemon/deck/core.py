"""Tasks, runs, worktrees, routing, config. No UI code in here."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import detect, sandbox
from .adapters import ORDER, Adapter, Claude, _tool_summary, make_adapters, register_custom
from .paths import CONFIG, STATE, WORKTREES
from .safety import danger
from .usage import UsageBook, agy_windows, claude_windows, codex_windows, copilot_windows, opencode_note


DEFAULT_CONFIG = {
    "worktrees": False,      # delegates edit the working tree directly
    "claude_mode": "auto",   # claude's own permission mode: auto | manual (asks about nearly everything) | acceptEdits
    "auto_checkin": True,    # wake a primary agent up when the delegates it started have finished
    "max_checkins": 10,      # per run, so two agents can't wake each other forever
    "default_agents": ["claude"],  # every task starts here; claude delegates to the others when asked
    "rules": [],                   # optional: {match = "regex", agents = ["codex"]}
    "max_delegations": 8,          # per task, across all agents
    "only": [],              # empty: every installed agent but claude; or list the ones to offer
    "rm_guard": False,       # true: shims ask before rm & co for fenced agents; false: trust each agent's own asking
    "max_depth": 2,                # a delegate may delegate once more, no deeper
}
DELEGATE = Path(__file__).resolve().parent.parent / "bin" / "deck-delegate"
MODELS_FILE = STATE / "models.json"
EFFORTS_FILE = STATE / "efforts.json"
CHECKIN_DELAY = 3.0  # seconds to wait for more delegates to finish before waking their parent
PLAN_NOTE = ("PLAN ONLY: do not modify any files or run commands that change state. Investigate if you need "
             "to, then reply with a concrete step-by-step plan.\n\n")
_SHELL_META = re.compile(r"[;&|<>`$\n(){}]")  # anything that can chain or hide a second command


def always_key(tool: str, inp: dict) -> str | None:
    """What "always allow for this run" would cover. None = only allow-once is offered."""
    if tool == "Bash":
        cmd = str(inp.get("command", "")).strip()
        if not cmd or _SHELL_META.search(cmd) or danger(cmd):  # dangerous things are asked about every time
            return None
        return "Bash: " + " ".join(cmd.split()[:2])
    if tool in ("Permissions", "Edit outside folder"):  # widening what the agent can reach: ask every time
        return None
    return tool  # e.g. "Write": allowing it for this run means its edits stop asking


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG.exists():
        cfg.update(tomllib.loads(CONFIG.read_text()))
    cfg["problems"] = register_custom(cfg.get("agents", {}))  # [agents.*] become extra agents
    return cfg


def route(prompt: str, cfg: dict, avail: set[str]) -> list[str]:
    for r in cfg["rules"]:
        if re.search(r["match"], prompt, re.I):
            picked = [a for a in r["agents"] if a in avail]
            if picked:
                return picked
    picked = [a for a in cfg["default_agents"] if a in avail]
    return picked or [a for a in ORDER if a in avail][:1]


# ---- git helpers --------------------------------------------------------------

async def sh(*args: str, cwd: str | None = None) -> tuple[int, str]:
    p = await asyncio.create_subprocess_exec(
        *args, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return p.returncode, out.decode(errors="replace").strip()


async def git_root(cwd: str) -> str | None:
    rc, out = await sh("git", "rev-parse", "--show-toplevel", cwd=cwd)
    return out if rc == 0 else None


# ---- model ----------------------------------------------------------------------

_ids = iter(range(1, 10**9))
_rids = iter(range(1, 10**9))


@dataclass
class Run:
    task: "Task"
    agent: str
    label: str = ""         # unique within the task: "codex", "codex-2"
    parent: "Run | None" = None
    depth: int = 0
    model: str = ""
    effort: str = ""
    plan: bool = False      # /plan: propose, don't change files (delegates inherit it)
    waiting: str = ""       # set while the run is blocked on your confirm/deny
    allowed: set[str] = field(default_factory=set)  # "always allow" answers, for this run only
    said_yes: dict = field(default_factory=dict)  # (tool, input) -> when you last allowed exactly that
    queued: list[str] = field(default_factory=list)  # replies typed while it was busy; sent when it finishes
    reported: bool = False  # a delegate's result has been handed to its parent
    checkins: int = 0       # times deck woke this run up because its delegates finished
    checkin_armed: bool = False
    rid: int = field(default_factory=lambda: next(_rids))
    status: str = "queued"  # queued running done failed killed
    cwd: str = ""
    branch: str = ""
    session: str | None = None
    tokens: int | None = None
    cost: float | None = None
    started: float = 0.0
    ended: float = 0.0
    lines: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)
    summary: str = ""       # git diff --stat once finished
    merged: bool = False
    proc: asyncio.subprocess.Process | None = None
    partial: str = ""       # streamed text not yet terminated by a newline
    last_images: tuple = ()  # retained so a rejected/failed image turn can be retried
    on_event: Callable[["Run", str, str], None] | None = None

    @property
    def elapsed(self) -> float:
        return (self.ended or time.time()) - self.started if self.started else 0.0

    @property
    def live(self) -> bool:
        return self.status in ("queued", "running")

    def result(self, limit: int = 16000) -> str:
        """What a delegating agent gets back: the text after the last tool call."""
        tail: list[str] = []
        for kind, text in reversed(self.lines):
            if kind == "tool":
                break
            if kind == "text" and not text.startswith("──"):
                tail.append(text)
        if not tail:
            tail = [t for k, t in self.lines if k == "text" and not t.startswith("──")]
        else:
            tail.reverse()
        out = "\n".join(tail).strip()
        if len(out) > limit:  # keep both ends: the start is usually the summary, the end the conclusion
            half = limit // 2
            out = out[:half] + f"\n[… {len(out) - limit} characters left out …]\n" + out[-half:]
        return out

    def emit_delta(self, chunk: str, kind: str = "text"):
        """Streamed text: only whole lines are shown; the tail waits for its newline."""
        if getattr(self, "partial_kind", "text") != kind:
            self.flush()
        self.partial_kind = kind
        self.partial += chunk
        *done, self.partial = self.partial.split("\n")
        for line in done:
            self.emit(kind, line)

    def flush(self):
        if self.partial.strip():
            self.emit(getattr(self, "partial_kind", "text"), self.partial)
        self.partial = ""

    def emit(self, kind: str, text: str):
        self.lines.append((kind, text))
        if self.on_event:
            self.on_event(self, kind, text)


@dataclass
class Task:
    prompt: str
    repo_cwd: str
    id: int = field(default_factory=lambda: next(_ids))
    runs: dict[str, Run] = field(default_factory=dict)
    root: str | None = None  # git toplevel, if any
    created: float = field(default_factory=time.time)
    name: str = ""  # set by /rename or /new <name>

    @property
    def title(self) -> str:
        return self.name or " ".join(self.prompt.split())[:60]

    @property
    def live(self) -> bool:
        return any(r.live for r in self.runs.values())


class Hub:
    def __init__(self, cfg: dict, notify: Callable[[Run], None]):
        self.cfg = cfg
        self.set_claude_mode(cfg.get("claude_mode", "auto"))
        self.found: dict[str, detect.Found] = detect.scan(make_adapters())  # only agents you have
        self.usage = UsageBook()  # limit windows per agent, for the progress bars
        self.notify = notify  # called on any run state change
        self.tasks: list[Task] = []
        self.tag = time.strftime("%m%d%H%M%S")
        self.runs: dict[int, Run] = {}
        self.sock_path = f"{os.environ.get('XDG_RUNTIME_DIR', '/tmp')}/deck-{os.getpid()}.sock"
        self.server: asyncio.AbstractServer | None = None
        # set by the UI: async (run, tool, input, always_key) -> "allow" | "always" | "deny"
        self.asker: Callable[[Run, str, dict, str | None], Awaitable[str]] | None = None
        self.models: dict[str, str] = self._load(MODELS_FILE)
        self.efforts: dict[str, str] = self._load(EFFORTS_FILE)

    def set_claude_mode(self, mode: str) -> bool:
        if mode not in Claude.MODES:
            return False
        self.cfg["claude_mode"] = Claude.MODE = mode
        return True

    @staticmethod
    def _load(path: Path) -> dict[str, str]:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _set(self, table: dict, path: Path, agent: str, value: str | None):
        if value:
            table[agent] = value
        else:
            table.pop(agent, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(table, indent=1))

    def reload_choices(self):
        """The mod's settings pane writes models.json / efforts.json: pick up its changes before the next run."""
        self.models, self.efforts = self._load(MODELS_FILE), self._load(EFFORTS_FILE)

    def set_model(self, agent: str, model: str | None):
        self._set(self.models, MODELS_FILE, agent, model)

    def set_effort(self, agent: str, effort: str | None):
        self._set(self.efforts, EFFORTS_FILE, agent, effort)

    # -- delegation: agents call bin/deck-delegate, which talks to this socket ----------
    async def serve(self):
        self.server = await asyncio.start_unix_server(self._client, self.sock_path)

    def close(self):
        if self.server:
            self.server.close()
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass

    def children(self, run: Run) -> list[Run]:
        return [r for r in run.task.runs.values() if r.parent is run]

    async def report(self, run: Run) -> str:
        """A finished run's answer, plus what it changed, in the form its parent gets it."""
        rc, changed = await sh("git", "status", "--short", cwd=run.cwd)
        changed = changed if rc == 0 else ""  # not a git folder: nothing to report, not an error
        return run.result() + (f"\n\n[files changed in {run.cwd}]\n{changed[:800]}" if changed else "")

    def preamble(self, run: Run) -> str:
        others = [n for n in sorted(self.available()) if n != run.agent]
        if run.depth >= self.cfg["max_depth"] or not others:
            return ""
        d = DELEGATE
        return (
            "You are running inside `deck`, a dashboard where the user watches several coding agents at once. "
            "If the user asks you to involve other agents, or a subtask clearly suits one, delegate it. "
            f"Agents available: {', '.join(others)}. A delegate works in the same folder (visible to the user). "
            f'Quick question: `{d} <agent> "<self-contained prompt>"` waits and prints the answer '
            f"(if it prints STILL RUNNING, run `{d} --wait <id>`). "
            f'To keep working yourself meanwhile: `{d} --start <agent> "<prompt>"` returns at once with an id. '
            f"Carry on with your own work; `{d} --poll` shows how your delegates are doing without waiting, "
            "and if you finish your turn first, deck wakes you up with their results when they are done, "
            "so you never have to sit and wait. Don't delegate unless it helps or the user asks.")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            async for raw in reader:
                req = json.loads(raw)
                writer.write((json.dumps(await self._handle(req)) + "\n").encode())
                await writer.drain()
        except (ValueError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _handle(self, req: dict) -> dict:
        if req.get("op") == "delegate":
            parent = self.runs.get(req.get("from", 0))
            agent = req.get("agent", "")
            if not parent:
                return {"error": "unknown parent run"}
            if parent.status in ("killed", "failed"):
                return {"error": f"parent run is {parent.status}"}
            if agent not in self.available():
                return {"error": f"{agent!r} not available; choose from {sorted(self.available())}"}
            if agent == parent.agent:
                return {"error": "don't delegate to yourself"}
            if parent.depth + 1 > self.cfg["max_depth"]:
                return {"error": "delegation depth limit reached"}
            delegations = sum(1 for r in parent.task.runs.values() if r.parent is not None)
            if delegations >= self.cfg["max_delegations"]:
                return {"error": "delegation limit reached for this task"}
            child = self._add_run(parent.task, agent, parent=parent)
            child.plan = parent.plan
            prompt = req.get("prompt", "")
            parent.emit("text", f"── delegated → {child.label}: {prompt[:100]}")
            asyncio.create_task(self._run(child, prompt, False, None))
            return {"id": child.rid}
        if req.get("op") == "wait":
            run = self.runs.get(req.get("id", 0))
            if not run:
                return {"error": "unknown run"}
            deadline = time.time() + req.get("timeout", 100)
            while run.live and time.time() < deadline:
                await asyncio.sleep(0.5)
            if run.live:
                return {"status": "running", "agent": run.label}
            run.reported = True
            return {"status": run.status, "agent": run.label, "result": await self.report(run)}
        if req.get("op") == "poll":  # non-blocking: how are my delegates doing?
            me = self.runs.get(req.get("from", 0))
            if not me:
                return {"error": "unknown run"}
            rows = []
            for c in self.children(me):
                row = {"id": c.rid, "agent": c.label, "status": c.status, "seconds": int(c.elapsed)}
                if not c.live and not c.reported:
                    c.reported = True
                    row["result"] = await self.report(c)
                rows.append(row)
            return {"delegates": rows}
        if req.get("op") == "permission":  # from bin/deck-permission-mcp: a headless claude wants to run a tool
            return await self._permission(req)
        return {"error": "bad request"}

    async def _permission(self, req: dict) -> dict:
        """From bin/deck-permission-mcp (claude) and the shell shims (agy, opencode)."""
        run = self.runs.get(req.get("from", 0))
        tool, inp = req.get("tool", "?"), req.get("input", {})
        if not run:
            return {"behavior": "deny", "message": "deck doesn't know this run"}
        if await self.decide(run, tool, inp):
            return {"behavior": "allow", "updatedInput": inp}
        return {"behavior": "deny", "message": "The user denied this request in deck. Don't retry it; "
                                               "continue without it or ask the user how to proceed."}

    async def decide(self, run: Run, tool: str, inp: dict) -> bool:
        """Ask the person (or reuse an earlier 'always'). True = allowed. Fails closed."""
        if not self.asker:
            return False
        key = always_key(tool, inp)
        what = _tool_summary(tool, inp)
        if key and key in run.allowed:
            run.emit("tool", f"allowed (you said always): {what}")
            return True
        # An agent's own ask and deck's shell guard can both fire for one command: don't ask twice
        said = (tool, str(inp.get("command", "")).strip() if tool == "Bash" else json.dumps(inp, sort_keys=True))
        if time.time() - run.said_yes.get(said, 0) < 120:
            run.emit("tool", f"allowed (you just said yes to exactly this): {what}")
            return True
        run.waiting = what
        self.notify(run)
        try:
            answer = await self.asker(run, tool, inp, key)
        finally:
            run.waiting = ""
            self.notify(run)
        if answer == "always" and key:
            run.allowed.add(key)
        ok = answer in ("allow", "always")
        if ok:
            run.said_yes[said] = time.time()
        run.emit("tool", f"you {'allowed' if ok else 'denied'}: {what}")
        return ok

    def _add_run(self, t: Task, agent: str, parent: Run | None = None) -> Run:
        label, n = agent, 1
        while label in t.runs:
            n += 1
            label = f"{agent}-{n}"
        r = Run(t, agent, label=label, parent=parent, depth=parent.depth + 1 if parent else 0,
                on_event=self._ev)
        if parent:
            r.cwd = parent.cwd
        t.runs[label] = r
        self.runs[r.rid] = r
        return r

    def available(self) -> set[str]:
        only = self.cfg.get("only")  # the plugin's primary is Claude Code itself, so Claude isn't a delegate by default
        return {n for n in self.found if (n in only if only else n != "claude")}

    async def refresh_usage(self, claude: bool = False) -> str:
        """Re-read limits. codex and agy are free to ask; claude only reports them while running, so asking
        means one tiny haiku turn, which counts against your limit like any other, hence opt-in."""
        notes = []
        if "agy" in self.found:
            with tempfile.TemporaryDirectory() as scratch:  # a slash command, but don't let it touch a real project
                rc, out = await sh(self.exe("agy"), "--output-format", "stream-json", "-p=/usage", cwd=scratch)
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("event") == "command_result" and (w := agy_windows(d)):
                    self.usage.set("agy", w)
                    break
            else:
                notes.append("agy limits unavailable")
        if "codex" in self.found:
            from . import codexserver
            limits = await codexserver.read_rate_limits(self.exe("codex"))
            if limits:
                self.usage.set("codex", *codex_windows(limits))
            else:
                notes.append("codex limits unavailable")
        if "copilot" in self.found:
            rc, out = await sh("gh", "api", "copilot_internal/user") if shutil.which("gh") else (1, "")
            try:
                windows, plan = copilot_windows(json.loads(out))
            except ValueError:
                windows, plan = [], ""
            if windows:
                self.usage.set("copilot", windows, plan)
            else:
                notes.append("copilot quota unavailable (is gh logged in?)")
        if "opencode" in self.found:
            reads = [await sh(self.exe("opencode"), "stats", "--standalone", "--days", str(d), "--json") for d in (0, 7)]
            try:
                self.usage.set("opencode", [], opencode_note(*(json.loads(out) for rc, out in reads)))
            except ValueError:
                notes.append("opencode stats unavailable")
        if claude and "claude" in self.found:
            rc, out = await sh(self.exe("claude"), "-p", "ok", "--model", "haiku", "--output-format", "stream-json",
                               "--verbose", "--permission-mode", "plan")
            for line in out.splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "rate_limit_event":
                    self.usage.set("claude", claude_windows(d.get("rate_limit_info", {})))
                    break
            else:
                notes.append("claude didn't report limits")
        return "; ".join(notes)

    def rescan(self) -> tuple[list[str], list[str]]:
        """Look for installed agents again. Returns (newly found, gone)."""
        old = set(self.found)
        self.found = detect.scan(make_adapters())
        return sorted(set(self.found) - old), sorted(old - set(self.found))

    def exe(self, name: str) -> str:
        """The agent's real path, so it still launches when its folder isn't on PATH."""
        return self.found[name].path if name in self.found else name

    async def start_task(self, prompt: str, agents: list[str], cwd: str, isolate: bool | None = None,
                         plan: bool = False) -> Task:
        t = Task(prompt, cwd)
        t.root = await git_root(cwd)
        use_wt = bool(t.root) and (self.cfg["worktrees"] if isolate is None else isolate)
        self.tasks.append(t)
        for name in agents:
            self._add_run(t, name).plan = plan
        for r in list(t.runs.values()):
            asyncio.create_task(self._run(r, prompt, use_wt, None))
        return t

    def _ev(self, run: Run, kind: str, text: str):
        self.notify(run)

    async def follow_up(self, run: Run, prompt: str, shown: str | None = None):
        if run.live or not run.session:
            return
        if run.status == "killed":
            run.status = "done"
        run.reported = False
        run.emit("info", f"── {shown or 'follow-up: ' + prompt}")
        asyncio.create_task(self._run(run, prompt, False, run.session))

    def reply(self, run: Run, text: str) -> str:
        """Send a message to a run: now if it's idle, else queued for when it finishes.
        Returns what happened, for the status line."""
        if run.live:
            run.queued.append(text)
            return f"{run.label} is busy: queued, it will get this when it finishes"
        if not run.session:
            return f"{run.label} has no session to continue (not every agent can resume)"
        asyncio.create_task(self.follow_up(run, text))
        return f"sent to {run.label}"

    async def start_in_task(self, task: Task, agent: str, prompt: str) -> Run:
        """A new run of `agent` inside an existing task, sharing the folder of the task's first run."""
        first = next(iter(task.runs.values()))
        run = self._add_run(task, agent)
        run.cwd = first.cwd or task.repo_cwd
        run.plan = first.plan
        asyncio.create_task(self._run(run, prompt, False, None))
        return run

    # -- idle runs that have work waiting: queued replies, and delegates that finished ------------
    def _after_turn(self, run: Run):
        """Called whenever a run stops. Delivers queued replies, and wakes a parent whose delegates are done."""
        if run.status == "killed":
            run.queued.clear()
            if run.parent:
                self._maybe_checkin(run.parent)
            return
        if run.queued and run.session:
            from .images import Prompt
            n, text, run.queued = len(run.queued), Prompt.join_prompts(run.queued), []
            asyncio.create_task(self.follow_up(run, text, f"your queued repl{'ies' if n > 1 else 'y'}: {text[:120]}"))
            return
        for parent in (run, run.parent):  # runs aren't hashable, so no set here
            if parent:
                self._maybe_checkin(parent)

    def _maybe_checkin(self, parent: Run):
        if (not self.cfg.get("auto_checkin", True) or parent.live or not parent.session
                or parent.status == "killed" or parent.checkin_armed
                or parent.checkins >= self.cfg.get("max_checkins", 10)):
            return
        if any(not c.live and not c.reported for c in self.children(parent)):
            parent.checkin_armed = True  # batch delegates that finish within a moment of each other
            asyncio.get_running_loop().call_later(CHECKIN_DELAY, lambda: asyncio.create_task(self._checkin(parent)))

    async def _checkin(self, parent: Run):
        try:
            if parent.live or parent.status == "killed":
                return
            done = [c for c in self.children(parent) if not c.live and not c.reported]
            if not done:
                return
            parts = []
            for c in done:
                parts.append(f"--- {c.label} ({c.status}) ---\n{await self.report(c)}")
            # Re-check parent state after awaiting reports
            if parent.live or parent.status == "killed":
                return
            for c in done:
                c.reported = True
            still = [c.label for c in self.children(parent) if c.live]
            message = ("[deck] Your delegated tasks have finished:\n\n" + "\n\n".join(parts)
                       + (f"\n\nStill running: {', '.join(still)}. They will be reported when done." if still else "")
                       + "\n\nContinue your task using these results. If nothing more is needed, say so briefly.")
            parent.checkins += 1
            await self.follow_up(parent, message, "check-in: " + ", ".join(f"{c.label} {c.status}" for c in done))
        finally:
            parent.checkin_armed = False
            if not parent.live and any(not c.live and not c.reported for c in self.children(parent)):
                self._maybe_checkin(parent)

    async def _run(self, run: Run, prompt: str, use_wt: bool, session: str | None):
        images = getattr(prompt, "images", ())
        if images:
            run.last_images = tuple(images)
        t = run.task
        if run.status == "killed":
            run.queued.clear()
            run.ended = time.time()
            self.notify(run)
            return
        ad: Adapter = make_adapters()[run.agent]
        run.status, run.started, run.ended = "running", time.time(), 0.0
        run.summary = ""
        self.reload_choices()
        run.model = self.models.get(run.agent, "")
        run.effort = self.efforts.get(run.agent, "")
        if session is None:
            who = f"from {run.parent.label}: " if run.parent else "prompt: "
            run.emit("text", f"── {who}{prompt[:160]}")
        self.notify(run)
        try:
            if session is None:
                run.cwd = run.cwd or t.repo_cwd
                if use_wt:
                    name = Path(t.root).name
                    run.branch = f"deck/{self.tag}-{t.id}-{run.agent}"
                    wt = WORKTREES / name / f"{self.tag}-{t.id}-{run.agent}"
                    wt.parent.mkdir(parents=True, exist_ok=True)
                    rc, out = await sh("git", "worktree", "add", "-q", "-b", run.branch, str(wt), cwd=t.root)
                    if rc != 0:
                        raise RuntimeError(f"worktree failed: {out}")
                    rel = os.path.relpath(t.repo_cwd, t.root)
                    run.cwd = str(wt / rel) if rel != "." else str(wt)
            else:
                run.session = session
            if run.plan and session is None:
                prompt = PLAN_NOTE + prompt
            env = {**os.environ, "PWD": run.cwd,  # some CLIs trust $PWD
                   "DECK_SOCK": self.sock_path, "DECK_RUN": str(run.rid),
                   "MCP_TOOL_TIMEOUT": "3600000"}  # an approval can wait an hour for you
            finished_ok: bool | None = None

            def apply(ev):
                nonlocal finished_ok
                if ev.meta.get("session"):
                    run.session = ev.meta["session"]
                if ev.meta.get("tokens") is not None:
                    run.tokens = ev.meta["tokens"]
                if ev.meta.get("cost") is not None:
                    run.cost = ev.meta["cost"]
                if ev.meta.get("usage") is not None:
                    self.usage.set("claude", claude_windows(ev.meta["usage"]))
                if ev.meta.get("usage_codex") is not None:
                    self.usage.set("codex", *codex_windows(ev.meta["usage_codex"]))
                if ev.kind in ("text", "thought") and ev.meta.get("delta"):
                    run.emit_delta(ev.text, ev.kind)
                elif ev.kind in ("text", "thought", "tool", "tool_output", "error"):
                    run.flush()
                    run.emit(ev.kind, ev.text.replace(run.cwd, ".") if run.cwd else ev.text)
                if ev.kind == "done":
                    finished_ok = True
                elif ev.kind == "error":
                    finished_ok = False

            if ad.name == "codex":  # talks JSON-RPC to `codex app-server` so it can ask you things
                from . import codexserver
                rc = await codexserver.drive(
                    self, run, ad.with_preamble(prompt, self.preamble(run), session), session,
                    model=run.model, effort=run.effort, plan=run.plan, cwd=run.cwd, env=env, apply=apply,
                    exe=self.exe("codex"), images=images)
            elif ad.acp is not None:  # speaks ACP: its own permission asks come to you as dialogs
                from . import acp
                model_args = ad.acp_args(run.model)  # a model it only takes on the command line
                argv = [self.exe(run.agent), *ad.acp, *model_args]
                if ad.sandboxed:  # a backstop for agents whose own settings don't ask (opencode's default doesn't)
                    argv, env = sandbox.wrap(argv, env, run.cwd, t.root, ad.name, (ad.binary,),
                                              guard=self.cfg["rm_guard"])
                rc = await acp.drive(self, run, ad.with_preamble(prompt, self.preamble(run), session), session,
                                     argv=argv, model="" if model_args else run.model, effort=run.effort, plan=run.plan,
                                     cwd=run.cwd, env=env, apply=apply, images=images)
            else:
                cmd = ad.cmd(prompt, run.cwd, session, model=run.model, effort=run.effort, plan=run.plan,
                             preamble=self.preamble(run), delegate=str(DELEGATE))
                cmd[0] = self.exe(run.agent)
                payload = None
                if images:
                    if ad.name != "claude":
                        raise RuntimeError(f"{ad.name}'s current adapter does not support image input")
                    cmd.pop(cmd.index("-p") + 1)
                    cmd += ["--input-format", "stream-json"]
                    payload = (json.dumps({"type": "user", "message": {"role": "user", "content": [
                        {"type": "text", "text": str(prompt)}, *[im.claude() for im in images]]}}) + "\n").encode()
                if ad.sandboxed:  # can't ask for itself: fence it in and put a guard in front of rm & co
                    cmd, env = sandbox.wrap(cmd, env, run.cwd, t.root, ad.name, (ad.binary,),
                                              guard=self.cfg["rm_guard"])
                run.proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=run.cwd, env=env, stdin=asyncio.subprocess.PIPE if payload else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, limit=1 << 24)
                if run.status == "killed":
                    try:
                        run.proc.terminate()
                    except ProcessLookupError:
                        pass

                async def feed():
                    if payload:
                        run.proc.stdin.write(payload)
                        await run.proc.stdin.drain()
                        run.proc.stdin.close()
                feed_task = asyncio.create_task(feed())
                async for raw in run.proc.stdout:
                    for ev in ad.parse(raw.decode(errors="replace").rstrip("\n")):
                        apply(ev)
                rc = await run.proc.wait()
                try:
                    await feed_task
                except (BrokenPipeError, ConnectionResetError) as exc:
                    raise RuntimeError("agent closed its input before receiving all attachments; use /image retry") from exc
            run.flush()
            if run.status != "killed":
                run.status = "done" if (rc == 0 and finished_ok is not False) else "failed"
                if rc != 0:
                    run.emit("error", f"exit code {rc}")
        except Exception as e:  # noqa: BLE001 - surface anything in the pane
            run.status = "failed"
            run.emit("error", f"{type(e).__name__}: {e}")
        finally:
            run.ended = time.time()
            run.proc = None
            if run.session:
                from .sessions import record  # late import: sessions imports sh from here
                record(run.agent, run.session, run.cwd, t.root or t.repo_cwd, t.title)
            if run.branch and run.cwd:
                _, run.summary = await sh("git", "diff", "--stat", "HEAD", cwd=run.cwd)
                _, untracked = await sh("git", "ls-files", "--others", "--exclude-standard", cwd=run.cwd)
                if untracked:
                    run.summary += ("\n" if run.summary else "") + "new: " + ", ".join(untracked.split("\n")[:6])
            self.notify(run)
            self._after_turn(run)

    def attach(self, agent: str, session: str, cwd: str, title: str, lines: list,
               task: Task | None = None) -> Run:
        """Show an old session as an idle pane; a follow-up resumes it in place."""
        t = task or Task(title, cwd)
        for existing in t.runs.values():
            if existing.agent == agent and existing.session == session and existing.cwd == cwd:
                return existing
        r = self._add_run(t, agent)
        r.cwd, r.session, r.status, r.lines = cwd, session, "done", list(lines)
        r.model, r.effort = self.models.get(agent, ""), self.efforts.get(agent, "")
        if task is None:
            self.tasks.append(t)
        return r

    def kill(self, run: Run, seen: set[int] | None = None) -> int:
        seen = set() if seen is None else seen
        if run.rid in seen:
            return 0
        seen.add(run.rid)
        n = 0
        if run.live:
            run.status = "killed"
            n += 1
            if run.proc:
                try:
                    run.proc.terminate()
                except ProcessLookupError:
                    pass
        # Cascade: kill all children (subagents) of this run too
        for child in self.children(run):
            n += self.kill(child, seen)
        return n

    async def diff(self, run: Run) -> str:
        if not run.branch:  # ran in place (or delegated into its parent's tree): show the tree's changes
            _, out = await sh("git", "diff", "--stat", "-p", "HEAD", cwd=run.cwd)
            return out or "(no uncommitted changes here)"
        await sh("git", "add", "-A", cwd=run.cwd)
        _, out = await sh("git", "diff", "--cached", "--stat", "-p", "HEAD", cwd=run.cwd)
        await sh("git", "reset", "-q", cwd=run.cwd)
        return out or "(no changes)"

    async def merge(self, run: Run) -> str:
        if not run.branch:
            return "nothing to merge: run wasn't in a worktree"
        if run.live:
            return "still running"
        t = run.task
        await sh("git", "add", "-A", cwd=run.cwd)
        await sh("git", "commit", "-q", "-m", f"deck/{run.agent}: {t.title}", cwd=run.cwd)
        rc, out = await sh("git", "merge", "--no-edit", run.branch, cwd=t.root)
        if rc != 0:
            await sh("git", "merge", "--abort", cwd=t.root)
            return f"merge failed (aborted, nothing changed): {out.splitlines()[-1] if out else ''}"
        run.merged = True
        return f"merged {run.branch} into current branch"

    async def discard(self, run: Run) -> str:
        if not run.branch or run.live:
            return "nothing to discard (or still running)"
        t = run.task
        await sh("git", "worktree", "remove", "--force", str(Path(run.cwd)), cwd=t.root)
        await sh("git", "branch", "-D", run.branch, cwd=t.root)
        run.branch = ""
        return "worktree + branch removed"
