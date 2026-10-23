#!/usr/bin/env python3
"""A tiny stand-in for `codex app-server`, for image/thought regression tests.

Logs every request to $FAKE_CODEX_LOG, then answers a turn with the notifications deck cares about:
a reasoning summary (thought), command output (tool_output), an agent message and turn/completed.
"""
import json
import os
import sys

LOG = os.environ.get("FAKE_CODEX_LOG")


def log(kind, payload):
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps({"kind": kind, "payload": payload}) + "\n")


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def note(method, params):
    send({"method": method, "params": params})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def main():
    while (m := read()) is not None:
        method, params, mid = m.get("method"), m.get("params") or {}, m.get("id")
        if method:
            log(method, params)
        if method == "initialize":
            send({"id": mid, "result": {"userAgent": "fake-codex"}})
        elif method == "thread/start":
            send({"id": mid, "result": {"thread": {"id": "fake-thread"}}})
        elif method == "thread/resume":
            send({"id": mid, "result": {"thread": {"id": params.get("threadId", "fake-thread")}}})
        elif method == "turn/start":
            send({"id": mid, "result": {"turn": {"id": "turn-1"}}})
            note("item/completed", {"item": {"id": "r1", "type": "reasoning",
                                             "summary": ["Looked at the image.", "Nothing alarming."]}})
            note("item/completed", {"item": {"id": "c1", "type": "commandExecution", "status": "completed",
                                             "command": "/usr/bin/bash -lc 'ls'", "aggregatedOutput": "a.txt\n"}})
            note("item/completed", {"item": {"id": "m1", "type": "agentMessage", "text": "all done\n"}})
            note("thread/tokenUsage/updated", {"tokenUsage": {"total": {"totalTokens": 4242}}})
            note("turn/completed", {"turn": {"id": "turn-1", "status": "completed"}})
        elif mid is not None:
            send({"id": mid, "error": {"code": -32601, "message": "unknown method"}})


main()
