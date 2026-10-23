"""Drives daemon/deck-mcp over stdio like Claude Code would, against tests/fake_acp_agent.py. No real agent runs.

    python3 tests/smoke_mcp.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import _env  # noqa: E402

cfg = _env.BASE / "config" / "deck"
cfg.mkdir(parents=True)
work = _env.BASE / "work"
work.mkdir()
(cfg / "config.toml").write_text(
    f'only = ["fake"]\n[agents.fake]\nacp = ["{sys.executable}", "{ROOT / "tests" / "fake_acp_agent.py"}"]\nfenced = false\n')

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail and not ok else ""))


def session(elicit, answer=None):
    p = subprocess.Popen([sys.executable, str(ROOT / "daemon" / "deck-mcp")], cwd=work, text=True,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=os.environ)
    n = 0

    def rpc(method, params=None):
        nonlocal n
        n += 1
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}) + "\n")
        p.stdin.flush()
        while True:
            m = json.loads(p.stdout.readline())
            if m.get("method") == "elicitation/create":
                asked.append(m["params"]["message"])
                p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": answer}) + "\n")
                p.stdin.flush()
            elif m.get("id") == n:
                return m

    def tool(name, **args):
        r = rpc("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r["isError"]

    asked = []
    rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {"elicitation": {}} if elicit else {}})
    return p, rpc, tool, asked


p, rpc, tool, asked = session(True, {"action": "accept", "content": {"decision": "allow"}})
names = [t["name"] for t in rpc("tools/list")["result"]["tools"]]
check("tools listed", {"delegate", "wait", "poll", "reply", "stop", "agents"} <= set(names), str(names))
check("agents lists the fake", tool("agents")[0].startswith("fake"))
text, err = tool("delegate", agent="nope", prompt="x")
check("unknown agent is an error", err and "not available" in text, text)
text, err = tool("delegate", agent="fake", prompt="hello there", wait=True)
check("delegate+wait returns the answer", not err and "[fake done]" in text, text)
text, _ = tool("delegate", agent="fake", prompt="REQUEST-PERMISSION")
rid = int(text.split("id=")[1].split(")")[0])
text, _ = tool("wait", id=rid)
check("permission asked via elicitation", len(asked) == 1 and "rm -rf build" in asked[0], str(asked))
check("allowed answer reaches the agent", "ALLOWED" in text, text)
check("poll shows both", tool("poll")[0].count("fake") >= 2)
p.stdin.close()
p.wait(5)

p, rpc, tool, asked = session(False)
text, _ = tool("delegate", agent="fake", prompt="REQUEST-PERMISSION", wait=True)
check("no elicitation support: denied, never allowed", not asked and "DENIED" in text or "CANCELLED" in text, text)
p.stdin.close()
p.wait(5)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
