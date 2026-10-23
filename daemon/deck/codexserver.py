"""Run codex through `codex app-server` rather than `codex exec`.

`exec` can't ask anything: commands that need approval just fail. The app server speaks JSON-RPC over
stdio and sends an approval request for each command, so deck can show its confirm/deny dialog.
Codex keeps its own rules: approvalPolicy "on-request" (codex's normal interactive setting; it asks
before things like `rm`, and for anything its sandbox blocks) inside the workspace-write sandbox, or
read-only for /plan. deck only supplies the answers.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Awaitable, Callable

from .adapters import Ev, _short

Apply = Callable[[Ev], None]


def inner_command(command: str) -> str:
    """codex reports `/usr/bin/bash -lc "<real command>"`; people want to see the real command."""
    try:
        words = shlex.split(command)
    except ValueError:
        return command
    if len(words) == 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh") and words[1] in ("-c", "-lc"):
        return words[2]
    return command


class Rpc:
    """Newline-delimited JSON-RPC to a subprocess. codex's app-server leaves out the "jsonrpc" field; ACP
    agents (acp.py) want it, hence the flag."""

    def __init__(self, proc: asyncio.subprocess.Process, jsonrpc: bool = False):
        self.proc = proc
        self.jsonrpc = jsonrpc
        self.n = 0
        self.waiting: dict[int, asyncio.Future] = {}

    def send(self, obj: dict):
        if self.jsonrpc:
            obj = {"jsonrpc": "2.0", **obj}
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())

    async def call(self, method: str, params: dict) -> dict:
        self.n += 1
        fut = asyncio.get_running_loop().create_future()
        self.waiting[self.n] = fut
        self.send({"id": self.n, "method": method, "params": params})
        reply = await fut
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result", {})

    def answer(self, req_id, result: dict):
        self.send({"id": req_id, "result": result})


async def read_rate_limits(exe: str = "codex", timeout: float = 20) -> dict | None:
    """Ask a short-lived app-server for the account's rate limits (5-hour and weekly windows)."""
    proc = await asyncio.create_subprocess_exec(
        exe, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)
    rpc = Rpc(proc)

    async def reader():
        async for raw in proc.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if "id" in m and "method" not in m and m["id"] in rpc.waiting:
                rpc.waiting.pop(m["id"]).set_result(m)

    task = asyncio.create_task(reader())
    try:
        async def ask():
            await rpc.call("initialize", {"clientInfo": {"name": "deck", "version": "1"}})
            rpc.send({"method": "initialized"})
            return (await rpc.call("account/rateLimits/read", None)).get("rateLimits")
        return await asyncio.wait_for(ask(), timeout)
    except (asyncio.TimeoutError, RuntimeError, OSError):
        return None
    finally:
        task.cancel()
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()


async def drive(hub, run, prompt: str, session: str | None, *, model: str, effort: str, plan: bool,
                cwd: str, env: dict, apply: Apply, exe: str = "codex", images=()) -> int:
    """Run one turn. Returns a process-style exit code: 0 completed, 1 failed."""
    proc = await asyncio.create_subprocess_exec(
        exe, "app-server", cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 24)
    run.proc = proc
    if run.status == "killed":
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    rpc = Rpc(proc)
    items: dict[str, dict] = {}          # item id -> item, so file-change approvals know the paths
    streamed_thoughts: set[str] = set()
    turn_done = asyncio.Event()
    outcome = {"rc": 1}
    root = os.path.realpath(cwd)

    def inside(path: str) -> bool:
        p = os.path.realpath(path)
        return p == root or p.startswith(root + os.sep)

    async def on_request(m: dict):
        method, p, rid = m["method"], m.get("params", {}), m["id"]
        try:
            if method == "item/commandExecution/requestApproval":
                cmd = inner_command(p.get("command") or "")
                ok = await hub.decide(run, "Bash", {"command": cmd, "description": p.get("reason") or ""})
                rpc.answer(rid, {"decision": "accept" if ok else "decline"})
            elif method == "item/fileChange/requestApproval":
                files = [c.get("path", "") for c in items.get(p.get("itemId"), {}).get("changes", [])]
                if files and not p.get("grantRoot") and all(inside(f) for f in files):
                    ok = True  # edits inside the run's own folder are fine, like claude's acceptEdits
                else:
                    ok = await hub.decide(run, "Edit outside folder", {"files": files, "reason": p.get("reason"),
                                                        "also wants write access to": p.get("grantRoot")})
                rpc.answer(rid, {"decision": "accept" if ok else "decline"})
            elif method == "item/permissions/requestApproval":
                ok = await hub.decide(run, "Permissions", {"wants": p.get("permissions"), "reason": p.get("reason")})
                rpc.answer(rid, {"permissions": p.get("permissions") if ok else {}, "scope": "turn"})
            elif method == "mcpServer/elicitation/request":
                rpc.answer(rid, {"action": "decline"})
            elif method == "item/tool/requestUserInput":
                rpc.answer(rid, {"answers": {}})
            else:  # anything we don't understand is refused rather than guessed at
                rpc.send({"id": rid, "error": {"code": -32601, "message": f"deck doesn't handle {method}"}})
        except Exception as e:  # noqa: BLE001
            apply(Ev("error", f"{method}: {e}"))
            rpc.send({"id": rid, "error": {"code": -32000, "message": str(e)}})

    def on_note(m: dict):
        method, p = m["method"], m.get("params", {})
        item = p.get("item", {})
        if method == "item/started":
            items[item.get("id")] = item
            kind = item.get("type")
            if kind == "commandExecution":
                apply(Ev("tool", "bash: " + inner_command(item.get("command", ""))))
            elif kind in ("mcpToolCall", "webSearch", "dynamicToolCall"):
                apply(Ev("tool", _short(item.get("tool") or item.get("query") or kind)))
        elif method == "item/completed":
            kind = item.get("type")
            if kind == "reasoning":
                summary = item.get("summary") or []
                if summary and item.get("id") not in streamed_thoughts:
                    apply(Ev("thought", "\n".join(summary)))
            elif kind == "commandExecution" and item.get("aggregatedOutput"):
                apply(Ev("tool_output", item["aggregatedOutput"]))
            if kind == "agentMessage" and (item.get("text") or "").strip():
                apply(Ev("text", item["text"]))
            elif kind == "fileChange" and item.get("status") == "completed":
                files = ", ".join(os.path.relpath(c.get("path", "?"), cwd) for c in item.get("changes", []))
                apply(Ev("tool", "edit: " + _short(files)))
            elif kind == "commandExecution" and item.get("status") in ("declined", "failed"):
                apply(Ev("tool", f"({item.get('status')}) " + _short(inner_command(item.get("command", "")))))
        elif method == "item/reasoning/summaryTextDelta":
            streamed_thoughts.add(p.get("itemId"))
            apply(Ev("thought", p.get("delta", ""), {"delta": True}))
        elif method == "item/reasoning/summaryPartAdded":
            apply(Ev("thought", "\n", {"delta": True}))
        elif method == "account/rateLimits/updated":
            apply(Ev("info", "", {"usage_codex": p.get("rateLimits") or p}))
        elif method == "thread/tokenUsage/updated":
            total = p.get("tokenUsage", {}).get("total", {}).get("totalTokens")
            if total is not None:
                apply(Ev("info", "", {"tokens": total}))
        elif method == "turn/completed":
            turn = p.get("turn", {})
            if turn.get("status") == "completed":
                outcome["rc"] = 0
            elif turn.get("error"):
                apply(Ev("error", str(turn["error"].get("message", turn["error"]))))
            else:
                apply(Ev("error", f"turn {turn.get('status', 'failed')}"))
            turn_done.set()
        elif method == "error":
            apply(Ev("error", str(p.get("error", {}).get("message", p))))

    async def reader():
        async for raw in proc.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if "method" in m and "id" in m:
                asyncio.create_task(on_request(m))
            elif "method" in m:
                on_note(m)
            elif "id" in m and m["id"] in rpc.waiting:
                rpc.waiting.pop(m["id"]).set_result(m)
        turn_done.set()  # the server went away

    reader_task = asyncio.create_task(reader())
    try:
        await rpc.call("initialize", {"clientInfo": {"name": "deck", "version": "1"}})
        rpc.send({"method": "initialized"})
        base = {"cwd": cwd, "approvalPolicy": "on-request",
                "sandbox": "read-only" if plan else "workspace-write"}
        if model:
            base["model"] = model
        if session:
            started = await rpc.call("thread/resume", {"threadId": session, **base})
        else:
            started = await rpc.call("thread/start", base)
        thread = started.get("thread", {}).get("id") or session
        apply(Ev("info", "", {"session": thread}))
        turn = {"threadId": thread, "input": [{"type": "text", "text": prompt}, *[im.codex() for im in images]]}
        if effort:
            turn["effort"] = effort
        await rpc.call("turn/start", turn)
        await turn_done.wait()
    except Exception as e:  # noqa: BLE001
        apply(Ev("error", str(e)))
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        reader_task.cancel()
        await proc.wait()
    return outcome["rc"]
