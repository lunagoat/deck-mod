"""Fence in agents that can't ask for permission themselves (agy, opencode, copilot).

Their headless modes run every tool call without asking, so deck puts two things around them:

1. bubblewrap: the whole filesystem is read-only except the run's own folder (plus the repo's .git for
   worktrees, a private /tmp, and the agent's own state directory). `rm -rf ~` can't touch anything
   that matters, whatever the agent does and however it spells the command.
2. shims: `rm`, `rmdir`, `shred`, `dd`... on PATH ahead of the real ones. They ask deck for a yes first
   (the same dialog claude and codex use), then run the real command. Inside the folder that is the
   only thing between an agent and `rm -rf .`, so it matters; agents that call /usr/bin/rm by absolute
   path skip the shim but are still stuck inside the fence.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SHIMS = Path(__file__).resolve().parent.parent / "bin" / "shims"

# Where each agent keeps its own state: it needs to write there or it can't log in / save the session.
STATE = {
    "agy": [".gemini"],
    "opencode": [".local/share/opencode", ".local/state/opencode", ".config/opencode", ".cache/opencode"],
    "copilot": [".copilot", ".config/github-copilot", ".cache/copilot", ".local/state/copilot"],
}


def _git_common_dir(cwd: str) -> str | None:
    """Worktrees keep their objects and index in the main repo's .git, so commits there must work."""
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def state_dirs(agent: str, names: tuple[str, ...] = ()) -> list[str]:
    """Where an agent keeps its own state, relative to $HOME. Known for the built-ins; for anything else a
    guess from its name, which covers the usual ~/.name, ~/.config/name, ~/.local/share/name... layouts."""
    if agent in STATE:
        return STATE[agent]
    out = []
    for n in dict.fromkeys([agent, *[x.split("|")[0] for x in names]]):
        out += [f".{n}", f".config/{n}", f".local/share/{n}", f".local/state/{n}", f".cache/{n}"]
    return out


def wrap(cmd: list[str], env: dict, cwd: str, root: str | None, agent: str,
         names: tuple[str, ...] = (), guard: bool = False) -> tuple[list[str], dict]:
    if not shutil.which("bwrap"):
        raise RuntimeError(f"bubblewrap (bwrap) isn't installed, so deck won't run {agent} unfenced")
    home = Path.home()
    writable = [os.path.realpath(cwd)]
    if root and (common := _git_common_dir(cwd)):
        writable.append(os.path.realpath(common))
    writable += [str(home / rel) for rel in state_dirs(agent, names)]
    args = ["bwrap", "--die-with-parent", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for path in dict.fromkeys(writable):  # after /tmp, so a folder under /tmp stays visible
        args += ["--bind-try", path, path]
    env = {**env, "DECK_FENCED": "1"}
    if guard:  # optional: ask before rm & co, on top of whatever the agent itself asks
        env["PATH"] = f"{SHIMS}{os.pathsep}{env.get('PATH', '')}"
    return [*args, "--", *cmd], env
