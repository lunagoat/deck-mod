"""Find past sessions of each agent so `/resume` can pick them up again."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .core import STATE, sh

DECK_INDEX = STATE / "sessions.jsonl"  # one line per finished deck run: the only source for agy/copilot
LIMIT = 40


@dataclass
class Session:
    agent: str
    id: str
    cwd: str
    title: str
    ts: float
    repo: str = ""


def _clean(text: str) -> str:
    """First line of a prompt, minus the delegation note deck prepends for some agents."""
    text = text.split("\n---\n", 1)[-1] if text.startswith("You are running inside `deck`") else text
    return " ".join(text.split())[:100]


def record(agent: str, session: str, cwd: str, repo: str, title: str):
    DECK_INDEX.parent.mkdir(parents=True, exist_ok=True)
    with DECK_INDEX.open("a") as f:
        f.write(json.dumps({"agent": agent, "id": session, "cwd": cwd, "repo": repo,
                            "title": title, "ts": time.time()}) + "\n")


def _deck_sessions() -> list[Session]:
    out = []
    try:
        for line in DECK_INDEX.read_text().splitlines():
            d = json.loads(line)
            out.append(Session(d["agent"], d["id"], d["cwd"], d["title"], d["ts"], d.get("repo", "")))
    except (OSError, ValueError, KeyError):
        pass
    return out


def _claude_sessions(cwd: str, everywhere: bool) -> list[Session]:
    root = Path.home() / ".claude" / "projects"
    dirs = list(root.iterdir()) if everywhere else [root / re.sub(r"[^A-Za-z0-9]", "-", cwd)]
    files = sorted((f for d in dirs if d.is_dir() for f in d.glob("*.jsonl")),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:LIMIT]
    out = []
    for f in files:
        title, scwd = "", cwd
        with f.open() as fh:
            for i, line in enumerate(fh):
                if i > 300:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                scwd = d.get("cwd", scwd)
                c = d.get("message", {}).get("content") if d.get("type") == "user" else None
                if isinstance(c, str) and c.strip() and not c.startswith("<"):
                    title = _clean(c)
                    break
        out.append(Session("claude", f.stem, scwd, title or "(no prompt)", f.stat().st_mtime))
    return out


def _codex_sessions(cwd: str, everywhere: bool) -> list[Session]:
    files = sorted((Path.home() / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"), reverse=True)
    out = []
    for f in files[:300]:
        sid = scwd = title = ""
        with f.open() as fh:
            for i, line in enumerate(fh):
                if i > 80:
                    break
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload", {})
                if d.get("type") == "session_meta":
                    sid, scwd = p.get("id", ""), p.get("cwd", "")
                    if not everywhere and not _under(scwd, cwd):
                        break
                item = p.get("item", {})
                if item.get("type") == "UserMessage" and not title:
                    title = _clean(" ".join(c.get("text", "") for c in item.get("content", [])))
                    break
        if sid and title and (everywhere or _under(scwd, cwd)):
            out.append(Session("codex", sid, scwd, title, f.stat().st_mtime))
        if len(out) >= LIMIT:
            break
    return out


async def _opencode_sessions(cwd: str) -> list[Session]:
    try:
        (rc, raw), (_, table) = await asyncio.wait_for(asyncio.gather(
            sh("opencode", "session", "list", "--format", "json", "-n", str(LIMIT), cwd=cwd),
            sh("opencode", "session", "list", "-n", str(LIMIT), cwd=cwd)), 10)
    except asyncio.TimeoutError:
        return []
    titles = {l.split("\t")[0]: l.split("\t")[1] for l in table.splitlines() if l.count("\t") >= 2}
    try:
        rows = json.loads(raw)
    except ValueError:
        return []
    return [Session("opencode", r["id"], r.get("directory", cwd), titles.get(r["id"], "(untitled)"),
                    r.get("updated", 0) / 1000) for r in rows]


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


async def find_sessions(cwd: str, everywhere: bool = False) -> list[Session]:
    claude, codex, oc = await asyncio.gather(
        asyncio.to_thread(_claude_sessions, cwd, everywhere),
        asyncio.to_thread(_codex_sessions, cwd, everywhere),
        _opencode_sessions(cwd))
    deck = [s for s in _deck_sessions() if everywhere or _under(s.cwd, cwd) or _under(s.repo, cwd)]
    merged: dict[tuple[str, str], Session] = {}
    for s in [*claude, *codex, *oc, *deck]:  # deck records last: they know the worktree cwd
        old = merged.get((s.agent, s.id))
        merged[(s.agent, s.id)] = Session(s.agent, s.id, s.cwd, old.title if old and s.title.startswith("(") else s.title,
                                          max(s.ts, old.ts if old else 0), s.repo)
    live = [s for s in merged.values() if os.path.isdir(s.cwd)]  # a discarded worktree can't be resumed
    return sorted(live, key=lambda s: s.ts, reverse=True)


def transcript(s: Session, turns: int = 8) -> list[tuple[str, str]]:
    """Last few messages, so the pane isn't empty after resuming. Best effort."""
    msgs: list[tuple[str, str]] = []
    try:
        if s.agent == "claude":
            f = next((Path.home() / ".claude" / "projects").glob(f"*/{s.id}.jsonl"))
            for line in f.open():
                d = json.loads(line)
                c = d.get("message", {}).get("content")
                if d.get("type") == "user" and isinstance(c, str) and not c.startswith("<"):
                    msgs.append(("user", _clean(c)))
                elif d.get("type") == "assistant" and isinstance(c, list):
                    t = "\n".join(b["text"] for b in c if b.get("type") == "text").strip()
                    if t:
                        msgs.append(("text", t))
        elif s.agent == "codex":
            f = next((Path.home() / ".codex" / "sessions").glob(f"*/*/*/rollout-*{s.id}.jsonl"))
            for line in f.open():
                item = json.loads(line).get("payload", {}).get("item", {})
                t = " ".join(c.get("text", "") for c in item.get("content", [])).strip()
                if item.get("type") == "UserMessage" and t:
                    msgs.append(("user", _clean(t)))
                elif item.get("type") == "AgentMessage" and t:
                    msgs.append(("text", t))
    except (StopIteration, OSError, ValueError):
        pass
    return [("text", f"── you: {t}" if k == "user" else t) for k, t in msgs[-turns:]]
