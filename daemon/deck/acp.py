"""Drive any coding agent that speaks ACP (Agent Client Protocol, agentclientprotocol.com).

ACP is JSON-RPC over the agent's stdin/stdout. The agent sends `session/request_permission` before a
tool call it isn't sure about, and deck answers with the same confirm/deny dialog claude and codex use.
So an ACP agent keeps its own safeguards and deck only supplies the answers. One process per turn;
later turns resume the session with `session/load` (or `session/resume`) when the agent supports it.

deck advertises no client capabilities (no fs/terminal methods), so the agent uses its own tools.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from typing import Callable

from .adapters import Ev, _short
from .codexserver import Rpc

PROTOCOL = 1
Apply = Callable[[Ev], None]


def _config_option(options: list[dict], *, id_: str = "", category: str = "") -> dict | None:
    for o in options or []:
        if (id_ and o.get("id") == id_) or (category and o.get("category") == category):
            return o
    return None


def _offers(option: dict, value: str) -> bool:
    return any(o.get("value") == value for o in option.get("options", []) if isinstance(o, dict))


def pick_option(options: list[dict], allow: bool) -> dict:
    """The outcome to answer a permission request with. Always an option ID the agent offered."""
    order = ("allow_once", "allow_always") if allow else ("reject_once", "reject_always")
    for kind in order:
        for o in options:
            if o.get("kind") == kind:
                return {"outcome": "selected", "optionId": o["optionId"]}
    return {"outcome": "cancelled"}  # nothing suitable offered: refuse rather than guess


def permission_input(tool_call: dict) -> tuple[str, dict]:
    """(tool name, input) in the shape deck's dialog and safety rules already understand."""
    kind, raw = tool_call.get("kind") or "", tool_call.get("rawInput") or {}
    if kind == "execute":
        return "Bash", {"command": str(raw.get("command") or tool_call.get("title") or ""), "description": ""}
    paths = [loc.get("path") for loc in tool_call.get("locations") or [] if isinstance(loc, dict)]
    inp = {"title": tool_call.get("title")}
    if raw:
        inp["input"] = raw
    if paths:
        inp["paths"] = paths
    return (tool_call.get("name") or kind or "Tool").capitalize(), inp


async def drive(hub, run, prompt: str, session: str | None, *, argv: list[str], model: str, effort: str,
                plan: bool, cwd: str, env: dict, apply: Apply, images=()) -> int:
    """Run one turn against an ACP agent started with `argv`. Returns 0 on a clean end_turn."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=1 << 24)
    run.proc = proc
    if run.status == "killed":
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    rpc = Rpc(proc, jsonrpc=True)
    stderr_tail: deque[str] = deque(maxlen=6)
    tools: dict[str, dict] = {}       # toolCallId -> what we know so far
    shown: set[str] = set()
    tool_outputs: dict[str, str] = {}
    state = {"stop": None, "replaying": False}

    async def drain_stderr():
        async for raw in proc.stderr:
            stderr_tail.append(raw.decode(errors="replace").rstrip())

    def tool_line(tc: dict) -> str:
        raw = tc.get("rawInput") or {}
        if tc.get("kind") == "execute" and raw.get("command"):
            return "bash: " + str(raw["command"])
        return _short(tc.get("title") or tc.get("name") or tc.get("kind") or "tool")

    def on_update(u: dict):
        kind = u.get("sessionUpdate")
        if state["replaying"]:   # session/load re-sends the old conversation; deck already has it
            return
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            c = u.get("content") or {}
            if c.get("type") == "text" and c.get("text"):
                apply(Ev("thought" if kind == "agent_thought_chunk" else "text", c["text"], {"delta": True}))
        elif kind in ("tool_call", "tool_call_update"):
            tc = tools.setdefault(u.get("toolCallId", "?"), {})
            tc.update({k: v for k, v in u.items() if v is not None})
            status = tc.get("status")
            first = tc.get("toolCallId") not in shown
            if status in ("in_progress", "completed", "failed") and first:
                shown.add(tc["toolCallId"])
                apply(Ev("tool", tool_line(tc)))
            if status == "failed" and u.get("sessionUpdate") == "tool_call_update":
                apply(Ev("tool", "(failed) " + tool_line(tc)))
            output = "\n".join(c["content"].get("text", "") for c in tc.get("content", [])
                               if c.get("type") == "content" and c.get("content", {}).get("type") == "text")
            key = u.get("toolCallId", "?")
            previous = tool_outputs.get(key, "")
            if output and output != previous:
                apply(Ev("tool_output", output[len(previous):] if output.startswith(previous) else output))
                tool_outputs[key] = output
        elif kind == "usage_update":
            meta = {"tokens": u.get("used")} if u.get("used") is not None else {}
            cost = (u.get("cost") or {}).get("amount")
            if cost:
                meta["cost"] = cost
            if meta:
                apply(Ev("info", "", meta))

    async def on_request(m: dict):
        method, p, rid = m["method"], m.get("params") or {}, m["id"]
        try:
            if method == "session/request_permission":
                tool, inp = permission_input(p.get("toolCall") or {})
                ok = await hub.decide(run, tool, inp)
                rpc.answer(rid, {"outcome": pick_option(p.get("options") or [], ok)})
            else:  # fs/* and terminal/*: we advertised none, so refuse anything else
                rpc.send({"id": rid, "error": {"code": -32601, "message": f"deck doesn't handle {method}"}})
        except Exception as e:  # noqa: BLE001
            apply(Ev("error", f"{method}: {e}"))
            rpc.send({"id": rid, "error": {"code": -32000, "message": str(e)}})

    async def reader():
        async for raw in proc.stdout:
            try:
                m = json.loads(raw)
            except ValueError:
                continue  # an agent that logs to stdout: ignore the noise
            if "method" in m and "id" in m:
                asyncio.create_task(on_request(m))
            elif m.get("method") == "session/update":
                on_update((m.get("params") or {}).get("update") or {})
            elif "id" in m and m["id"] in rpc.waiting:
                rpc.waiting.pop(m["id"]).set_result(m)
        for fut in rpc.waiting.values():  # the agent went away mid-request
            if not fut.done():
                fut.set_result({"error": {"message": "the agent exited"
                                          + (": " + " | ".join(stderr_tail) if stderr_tail else "")}})

    tasks = [asyncio.create_task(reader()), asyncio.create_task(drain_stderr())]
    rc = 1
    try:
        init = await rpc.call("initialize", {"protocolVersion": PROTOCOL, "clientCapabilities": {},
                                             "clientInfo": {"name": "deck", "version": "1"}})
        if init.get("protocolVersion") != PROTOCOL:
            raise RuntimeError(f"agent speaks ACP v{init.get('protocolVersion')}, deck speaks v{PROTOCOL}")
        caps = init.get("agentCapabilities") or {}
        if images and not (caps.get("promptCapabilities") or {}).get("image"):
            raise RuntimeError("this ACP agent does not advertise image input support; recover attachments with /image retry")
        base = {"cwd": cwd, "mcpServers": []}
        auth_methods = init.get("authMethods") or []
        authed = False

        async def authed_call(method: str, params: dict) -> dict:
            """Agents that haven't been logged in answer with an auth error: pick the first method they offer
            (usually 'use the credentials the CLI already has') and try once more."""
            nonlocal authed
            try:
                return await rpc.call(method, params)
            except RuntimeError as e:
                if authed or not auth_methods or "auth" not in str(e).lower():
                    raise
                authed = True
                chosen = auth_methods[0]
                apply(Ev("tool", f"(authenticating with {chosen.get('name') or chosen.get('id')})"))
                await rpc.call("authenticate", {"methodId": chosen.get("id")})
                return await rpc.call(method, params)

        info: dict = {}
        if session and caps.get("loadSession"):
            state["replaying"] = True
            try:
                info = await authed_call("session/load", {"sessionId": session, **base})
            finally:
                state["replaying"] = False
            sid = session
        elif session and (caps.get("sessionCapabilities") or {}).get("resume") is not None:
            info = await authed_call("session/resume", {"sessionId": session, **base})
            sid = session
        else:
            if session:
                apply(Ev("error", "this agent can't resume sessions: starting a fresh one, it won't remember earlier turns"))
            info = await authed_call("session/new", base)
            sid = info.get("sessionId")
        if not sid:
            raise RuntimeError("the agent didn't return a session id")
        apply(Ev("info", "", {"session": sid}))

        # model / effort / plan: best effort through whatever the agent advertises
        options = info.get("configOptions") or []
        for want, found in ((model, _config_option(options, id_="model", category="model")),
                            (effort, _config_option(options, category="thought_level")
                             or _config_option(options, id_="reasoning_effort"))):
            if want and found and _offers(found, want):
                await rpc.call("session/set_config_option", {"sessionId": sid, "configId": found["id"], "value": want})
            elif want and found is None and want == model and any(
                    m.get("modelId") == want for m in (info.get("models") or {}).get("availableModels", [])):
                await rpc.call("session/set_model", {"sessionId": sid, "modelId": want})  # the older, pre-config-option way
            elif want:
                apply(Ev("tool", f"({found['name'] if found else 'setting'} {want!r} isn't offered by this agent)"))
        if plan:
            mode = _config_option(options, category="mode")
            plan_value = next((o["value"] for o in (mode or {}).get("options", []) if "plan" in str(o.get("value", "")).lower()), None)
            modes = info.get("modes") or {}
            legacy = next((m["id"] for m in modes.get("availableModes", []) if "plan" in m.get("id", "").lower()), None)
            if mode and plan_value:
                await rpc.call("session/set_config_option", {"sessionId": sid, "configId": mode["id"], "value": plan_value})
            elif legacy:
                await rpc.call("session/set_mode", {"sessionId": sid, "modeId": legacy})

        result = await rpc.call("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": prompt}, *[im.acp() for im in images]]})
        usage = result.get("usage") or {}
        if usage.get("totalTokens"):
            apply(Ev("info", "", {"tokens": usage["totalTokens"]}))
        state["stop"] = result.get("stopReason")
        if state["stop"] == "end_turn":
            rc = 0
        elif state["stop"] != "cancelled":
            apply(Ev("error", f"stopped early: {state['stop']}"))
    except Exception as e:  # noqa: BLE001 - shown in the pane
        apply(Ev("error", str(e)))
    finally:
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        for t in tasks:
            t.cancel()
        await proc.wait()
    return rc
