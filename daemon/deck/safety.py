"""What counts as dangerous, in one place.

Every agent's guard uses this: the hook that makes Claude ask, the approval handler for codex, and the
shell shims that sit in front of rm and friends for agents that can't ask on their own. It errs on the
side of asking: a false alarm costs one keypress, a miss costs your files.
Stdlib only, because the guard scripts import it under whatever python3 is on PATH.
"""
from __future__ import annotations

import os
import re
import shlex

DELETERS = {"rm", "rmdir", "unlink", "shred"}
DISK = {"dd", "fdisk", "sfdisk", "cfdisk", "parted", "wipefs", "mkswap", "blkdiscard"}
ROOT = {"sudo", "doas", "su", "pkexec", "run0"}
POWER = {"shutdown", "reboot", "poweroff", "halt"}
KILLERS = {"kill", "killall", "pkill"}
PACKAGERS = {"pacman", "yay", "paru", "apt", "apt-get", "dnf", "yum", "zypper", "brew", "snap", "flatpak"}
WRAPPERS = {"env", "command", "nohup", "time", "nice", "exec", "builtin", "xargs", "timeout", "stdbuf", "setsid"}
INTERPRETERS = {"python", "python3", "perl", "ruby", "node", "php", "bash", "sh", "zsh", "fish"}
INLINE_BAD = re.compile(r"rmtree|unlink|os\.remove|os\.rmdir|shutil\.move|rm\s+-|\brm\b|system\(|subprocess|popen|exec\(", re.I)
PKG_CHANGE = re.compile(r"-[SRU]\w*|--sync|--remove|--upgrade|install|remove|purge|upgrade|full-upgrade|"
                        r"dist-upgrade|autoremove|uninstall|erase|update")
SPLIT = re.compile(r"&&|\|\||[;|\n&]|\$\(|`|\(|\)|\{|\}")
RAW = [
    (re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b"), "runs downloaded code"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), "writes to a disk device"),
    (re.compile(r"\bof=/dev/"), "writes to a disk device"),
    (re.compile(r":\s*\(\s*\)\s*\{"), "fork bomb"),
    (re.compile(r"\s-delete\b|-exec\s+(sudo\s+)?rm\b"), "deletes files"),
]


def _words(segment: str) -> list[str]:
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _strip(words: list[str]) -> list[str]:
    """Drop VAR=x prefixes and wrappers like env/nohup/xargs/timeout N so the real command is first."""
    while words:
        w = words[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w):
            words = words[1:]
        elif os.path.basename(w) in WRAPPERS:
            name = os.path.basename(w)
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[1:]
            if name == "timeout" and words:
                words = words[1:]  # the duration
        else:
            break
    return words


INLINE = re.compile(r"\b(python3?|perl|ruby|node|php|bash|sh|zsh)\s+-\w*[cepr]\w*\s")


def danger(command: str) -> str | None:
    """Why this shell command needs a human's yes, or None if it looks ordinary."""
    for pattern, why in RAW:
        if pattern.search(command):
            return why
    if INLINE.search(command) and INLINE_BAD.search(command):  # before splitting: code has ; and ( in it
        return "inline code that deletes files or runs commands"
    for segment in SPLIT.split(command):
        words = _strip(_words(segment.strip()))
        if not words:
            continue
        name, args = os.path.basename(words[0]), words[1:]
        if name in DELETERS or name.startswith("rm."):
            return "deletes files"
        if name in DISK or name.startswith("mkfs"):
            return "writes to a disk directly"
        if name in ROOT:
            return "runs as root"
        if name in POWER:
            return "shuts the machine down"
        if name in KILLERS:
            return "kills processes"
        if name == "truncate":
            return "truncates files"
        if name in ("chmod", "chown", "chgrp") and any(a in ("-R", "--recursive") or re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", a) for a in args):
            return "changes permissions recursively"
        if name in PACKAGERS and any(PKG_CHANGE.fullmatch(a) for a in args):
            return "changes installed software"
        if name == "systemctl" and any(a in ("stop", "disable", "mask", "restart", "kill", "reboot", "poweroff") for a in args):
            return "changes services"
        if name in ("pip", "pip3", "npm", "pnpm", "yarn", "cargo", "gem") and any(a in ("-g", "--global", "--break-system-packages") for a in args):
            return "installs software system-wide"
        if name == "git" and args:
            sub, rest, skip = "", [], False
            for i, a in enumerate(args):  # the subcommand is the first word that isn't an option (or its value)
                if skip:
                    skip = False
                elif a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
                    skip = True
                elif not a.startswith("-"):
                    sub, rest = a, args[i + 1:]
                    break
            if sub == "push":
                return "publishes to a remote"
            if sub == "clean":
                return "deletes untracked files"
            if sub == "reset" and "--hard" in rest:
                return "discards work (git reset --hard)"
            if sub in ("checkout", "restore", "switch") and any(a in ("--", ".", "-f", "--force", "--discard-changes") for a in rest):
                return "discards uncommitted changes"
            if sub == "branch" and any(a in ("-D", "--delete", "-d") for a in rest):
                return "deletes a branch"
            if sub in ("stash",) and rest[:1] in (["drop"], ["clear"]):
                return "discards stashed work"
            if sub in ("filter-branch", "filter-repo", "replace") or (sub == "reflog" and "expire" in rest) \
                    or (sub == "gc" and any("prune" in a for a in rest)):
                return "rewrites git history"
            if sub == "rebase" or (sub in ("commit", "merge") and "--amend" in rest):
                return "rewrites git history"
        if name in INTERPRETERS:
            code = " ".join(args)
            if any(a in ("-c", "-e", "-p", "-r", "-pe", "-ne") for a in args) and INLINE_BAD.search(code):
                return "inline code that deletes files or runs commands"
    return None
