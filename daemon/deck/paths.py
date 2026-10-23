"""Where deck keeps its config, state and worktrees (XDG, overridable for tests)."""
import os
from pathlib import Path

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "deck" / "config.toml"
STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "deck"
WORKTREES = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "deck" / "worktrees"
