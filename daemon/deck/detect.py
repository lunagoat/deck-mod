"""Find out which coding agents are installed, so deck only offers the ones you actually have."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Where agent CLIs tend to land when the shell's PATH doesn't include them (npm -g, cargo, bun, pipx...).
EXTRA_DIRS = [".local/bin", ".cargo/bin", ".bun/bin", ".npm-global/bin", ".volta/bin", "go/bin",
              ".opencode/bin", ".claude/local", ".cache/.bun/bin", "bin"]
SYSTEM_DIRS = ["/usr/local/bin", "/opt/homebrew/bin", "/home/linuxbrew/.linuxbrew/bin", "/snap/bin"]

# Popular coding-agent CLIs deck can't drive yet (no ACP server mode, or none we could verify): binary -> product. Only used to tell you
# "you have this, deck can't drive it yet" (and how to add it in config.toml). Best-effort list.
KNOWN_UNSUPPORTED = {
    "aider": "Aider", "amp": "Amp", "crush": "Crush", "droid": "Droid (Factory)", "plandex": "Plandex",
    "openhands": "OpenHands",
}


@dataclass
class Found:
    name: str
    path: str


def search_path() -> str:
    home = Path.home()
    dirs = os.environ.get("PATH", "").split(os.pathsep)
    dirs += [str(home / d) for d in EXTRA_DIRS] + SYSTEM_DIRS
    seen: list[str] = []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.append(d)
    return os.pathsep.join(seen)


def find(binary: str) -> str | None:
    """Absolute path of an executable on PATH or in the usual install dirs, else None."""
    for name in binary.split("|"):  # "cursor-agent|agent": the first one that exists
        if (path := shutil.which(name, path=search_path())):
            return path
    return None


def scan(adapters: dict) -> dict[str, Found]:
    """name -> where it lives, for every supported agent that is actually installed."""
    out = {}
    for name, ad in adapters.items():
        if (path := find(ad.binary)):
            out[name] = Found(name, path)
    return out


def scan_unsupported(adapters: dict) -> dict[str, str]:
    """Popular agents found on this machine that deck can't drive yet: product -> path."""
    ours = {a.binary for a in adapters.values()}
    return {product: path for binary, product in KNOWN_UNSUPPORTED.items()
            if binary not in ours and (path := find(binary))}
