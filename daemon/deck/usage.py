"""Usage limits per agent, shown as progress bars.

Where the numbers come from (nothing here calls an undocumented web API):
  claude  - the `rate_limit_event` in its stream-json output (5-hour and 7-day windows). It arrives with
            every run, so the bars are as fresh as your last claude run; `/usage refresh` makes one tiny run.
  codex   - `account/rateLimits/read` on `codex app-server`, polled while deck is open and pushed live
            via `account/rateLimits/updated` during a run.
  agy     - its own `/usage` slash command, run in print mode (`agy --output-format stream-json -p=/usage`):
            no model turn, just a structured `command_result` with each model group's 5h and weekly buckets.
  copilot - GitHub's `copilot_internal/user` endpoint (undocumented, but it's what Copilot's own editor plugins
            call), read through `gh api` so deck never touches a token. Monthly chat / premium / completions quotas.
  opencode - has no limits (free Zen models or pay-per-use), so instead of bars it shows what you've used:
            tokens today and over 7 days from `opencode stats --json`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from dataclasses import dataclass, field


from .paths import STATE

USAGE_FILE = STATE / "usage.json"
NOT_EXPOSED = {
    "copilot": "needs the gh CLI, logged in, to read the Copilot quota",
}
STALE = 30 * 60  # seconds before a reading is shown as old


@dataclass
class Window:
    label: str
    used: float                  # 0..1
    resets_at: float | None = None


@dataclass
class AgentUsage:
    windows: list[Window] = field(default_factory=list)
    note: str = ""
    updated: float = 0.0


def window_label(minutes: int | None, key: str = "") -> str:
    if key:
        return {"five_hour": "5h", "seven_day": "7d"}.get(key, key.replace("_", " "))
    if not minutes:
        return "?"
    return f"{minutes // 1440}d" if minutes % 1440 == 0 and minutes >= 1440 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def claude_windows(info: dict) -> list[Window]:
    """From a stream-json rate_limit_event's rate_limit_info."""
    out = []
    for key, w in (info.get("unifiedWindows") or {}).items():
        if isinstance(w, dict) and w.get("utilization") is not None:
            out.append(Window(window_label(None, key), float(w["utilization"]), w.get("resetsAt")))
    return out


AGY_GROUPS = {"gemini": "gem", "3p": "claude/gpt"}


def agy_windows(result: dict) -> list[Window]:
    """From the `command_result` event of agy's `/usage`: groups of buckets with `remaining_fraction`.
    Most-used first, so the one-line strip headlines the limit you're closest to (usually a weekly one)."""
    out = []
    for group in (result.get("command", {}).get("data", {}).get("groups") or []):
        for b in group.get("buckets") or []:
            if b.get("remaining_fraction") is None:
                continue
            short, _, _ = b.get("id", "").rpartition("-")
            win = {"weekly": "7d"}.get(b.get("window"), b.get("window") or "?")
            try:
                reset = datetime.fromisoformat(b["reset_time"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, AttributeError):
                reset = None
            out.append(Window(f"{AGY_GROUPS.get(short, short or group.get('name', '?'))} {win}",
                              1 - float(b["remaining_fraction"]), reset))
    return sorted(out, key=lambda w: -w.used)


COPILOT_QUOTAS = {"chat": "chat", "premium_interactions": "premium", "completions": "compl"}


def copilot_windows(user: dict) -> tuple[list[Window], str]:
    """From `copilot_internal/user`: monthly quotas (skipping unlimited ones and ones the plan doesn't have)."""
    try:
        reset = datetime.fromisoformat(user["quota_reset_date_utc"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        reset = None
    out = []
    for key, q in (user.get("quota_snapshots") or {}).items():
        if not isinstance(q, dict) or q.get("unlimited") or not q.get("has_quota") or q.get("percent_remaining") is None:
            continue
        out.append(Window(COPILOT_QUOTAS.get(key, key), 1 - float(q["percent_remaining"]) / 100, reset))
    return sorted(out, key=lambda w: -w.used), user.get("copilot_plan") or ""


def tokens_short(n: float) -> str:
    return f"{n / 1e6:.1f}m" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n:.0f}"


def opencode_note(today: dict, week: dict) -> str:
    """No limit windows exist, so say what was used. Cache reads are left out: they dwarf real work."""
    def tok(st):
        t = st.get("tokens") or {}
        return (t.get("input") or 0) + (t.get("output") or 0) + (t.get("reasoning") or 0)
    parts = [f"{tokens_short(tok(today))} tokens today", f"{tokens_short(tok(week))} in 7d"]
    if week.get("cost"):
        parts.append(f"${week['cost']:.2f} in 7d")
    return " · ".join(parts)


def codex_windows(limits: dict) -> tuple[list[Window], str]:
    """From codex's `rateLimits` object (primary + secondary window, optional credits)."""
    out = []
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if isinstance(w, dict) and w.get("usedPercent") is not None:
            out.append(Window(window_label(w.get("windowDurationMins")), w["usedPercent"] / 100, w.get("resetsAt")))
    credits = limits.get("credits") or {}
    parts = [limits["planType"]] if limits.get("planType") else []
    if credits.get("hasCredits") and not credits.get("unlimited") and credits.get("balance"):
        parts.append(f"{float(credits['balance']):,.0f} credits")
    return out, " · ".join(parts)


class UsageBook:
    """Latest reading per agent, remembered across restarts."""

    def __init__(self):
        self.data: dict[str, AgentUsage] = {}
        try:
            raw = json.loads(USAGE_FILE.read_text())
            for agent, u in raw.items():
                self.data[agent] = AgentUsage([Window(**w) for w in u["windows"]], u.get("note", ""), u.get("updated", 0))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def set(self, agent: str, windows: list[Window], note: str = ""):
        if not windows and not note:
            return
        self.data[agent] = AgentUsage(windows, note, time.time())
        try:
            USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps({a: {"windows": [w.__dict__ for w in u.windows], "note": u.note,
                                                  "updated": u.updated} for a, u in self.data.items()}))
        except OSError:
            pass
