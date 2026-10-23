"""Where the /model picker gets its suggestions. Free-text models are always allowed."""
from __future__ import annotations

import json

from .core import sh

CLAUDE = ["opus", "sonnet", "haiku", "claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]


async def list_models(agent: str) -> list[tuple[str, str]]:
    """[(model id, description)] for one agent; empty if the CLI can't list them."""
    if agent == "claude":
        return [(m, "") for m in CLAUDE]
    if agent == "codex":
        rc, out = await sh("codex", "debug", "models")
        try:
            return [(m["slug"], m.get("display_name", "")) for m in json.loads(out)["models"]]
        except (ValueError, KeyError):
            return []
    if agent == "opencode":
        rc, out = await sh("opencode", "models")
        return [(l.strip(), "") for l in out.splitlines() if "/" in l]
    if agent == "agy":
        rc, out = await sh("agy", "models")
        rows = [l.split("\t", 1) for l in out.splitlines() if "\t" in l]
        return [(r[0], r[1] if len(r) > 1 else "") for r in rows]
    return []
