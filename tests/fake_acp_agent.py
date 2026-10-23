#!/usr/bin/env python3
"""A tiny ACP agent for tests: speaks the protocol deck uses, and logs what it received to $FAKE_ACP_LOG.

Prompt keywords: "REQUEST-PERMISSION" -> request permission to run `rm -rf build` and report the answer;
"CRASH-NOW" -> exit with an error; "HANG-NOW" -> never answer.
"""
import json
import os
import sys
import time

LOG = os.environ.get("FAKE_ACP_LOG")


def log(kind, payload):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")


def send(obj):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **obj}) + "\n")
    sys.stdout.flush()


def update(sid, **u):
    send({"method": "session/update", "params": {"sessionId": sid, "update": u}})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


AUTH = "--auth" in sys.argv          # session/new fails until `authenticate` is called
LEGACY = "--legacy-models" in sys.argv  # models via `models`/session/set_model instead of configOptions


def main():
    authed = not AUTH
    while (m := read()) is not None:
        method, params, mid = m.get("method"), m.get("params") or {}, m.get("id")
        if method:
            log(method, params)
        if method == "initialize":
            send({"id": mid, "result": {"protocolVersion": 1, "authMethods": [{"id": "login", "name": "Log in"}] if AUTH else [], "agentInfo": {"name": "fake"},
                                        "agentCapabilities": {"loadSession": True, "sessionCapabilities": {}}}})
        elif method == "authenticate":
            authed = params.get("methodId") == "login"
            send({"id": mid, "result": {}} if authed else {"id": mid, "error": {"code": -32000, "message": "bad method"}})
        elif method == "session/new" and not authed:
            send({"id": mid, "error": {"code": -32000, "message": "Authentication required"}})
        elif method == "session/new" and LEGACY:
            send({"id": mid, "result": {"sessionId": "fake-1", "models": {"currentModelId": "a", "availableModels": [
                {"modelId": "a", "name": "A"}, {"modelId": "b", "name": "B"}]}}})
        elif method == "session/set_model":
            send({"id": mid, "result": {}})
        elif method == "session/new":
            send({"id": mid, "result": {"sessionId": "fake-1", "configOptions": [
                {"id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "a",
                 "options": [{"value": "a", "name": "A"}, {"value": "b", "name": "B"}]}]}})
        elif method == "session/load":
            update(params["sessionId"], sessionUpdate="agent_message_chunk", content={"type": "text", "text": "OLD HISTORY\n"})
            send({"id": mid, "result": {}})
        elif method == "session/set_config_option":
            send({"id": mid, "result": {"configOptions": []}})
        elif method == "session/prompt":
            sid, text = params["sessionId"], params["prompt"][0]["text"]
            if "CRASH-NOW" in text:
                sys.stderr.write("boom: something broke\n")
                sys.exit(3)
            if "HANG-NOW" in text:
                time.sleep(600)
            update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "Hello "})
            update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "world\n"})
            answer = ""
            if "REQUEST-PERMISSION" in text:
                # a different command each time, so deck's "you just said yes to exactly this" memory doesn't apply
                n = sum('"permission_response"' in l for l in open(LOG)) if LOG and os.path.exists(LOG) else 0
                cmd = "rm -rf build" + (str(n) if n else "")
                update(sid, sessionUpdate="tool_call", toolCallId="t1", title="shell", kind="execute", status="pending")
                send({"id": 1000, "method": "session/request_permission", "params": {
                    "sessionId": sid, "toolCall": {"toolCallId": "t1", "title": cmd, "kind": "execute",
                                                   "rawInput": {"command": cmd}},
                    "options": [{"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                                {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}})
                while (r := read()) is not None and r.get("id") != 1000:
                    pass
                outcome = (r or {}).get("result", {}).get("outcome", {})
                log("permission_response", outcome)
                answer = {"once": "ALLOWED", "always": "ALLOWED", "reject": "DENIED"}.get(outcome.get("optionId"), "CANCELLED")
                update(sid, sessionUpdate="tool_call_update", toolCallId="t1", status="in_progress", title=cmd,
                       rawInput={"command": cmd})
                update(sid, sessionUpdate="tool_call_update", toolCallId="t1", status="completed")
                update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": answer + "\n"})
            update(sid, sessionUpdate="usage_update", used=1234, size=200000)
            send({"id": mid, "result": {"stopReason": "end_turn", "usage": {"totalTokens": 1500}}})
        elif mid is not None:
            send({"id": mid, "error": {"code": -32601, "message": "unknown method"}})


main()
