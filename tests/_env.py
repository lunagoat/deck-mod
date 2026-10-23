"""A throwaway environment for the tests, imported first by each one.

Everything deck writes (state, config, worktrees) goes to a temp dir that is deleted afterwards, and the work
folders are outside any git repo, so a test can never touch your real deck state or create worktrees and
branches in a real repository (it used to, whenever the checkout itself was a git repo).
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

BASE = Path(tempfile.mkdtemp(prefix="deck-test-"))
atexit.register(shutil.rmtree, BASE, True)
os.environ.update(XDG_STATE_HOME=str(BASE / "state"), XDG_CONFIG_HOME=str(BASE / "config"),
                  XDG_DATA_HOME=str(BASE / "data"))
