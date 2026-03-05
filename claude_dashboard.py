#!/usr/bin/env python3
"""Claude Code Usage Dashboard - live terminal dashboard for tracking usage."""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from rich.align import Align

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
TELEMETRY_DIR = CLAUDE_DIR / "telemetry"
BUDGET_CONFIG_PATH = CLAUDE_DIR / "dashboard_config.json"

# Cost per million tokens (approximate)
COST_PER_M: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cached": 1.875},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cached": 0.375},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0, "cached": 0.08},
}

BAR_CHARS = "▁▂▃▄▅▆▇█"

# -- Modern color palette --
CLR_ACCENT = "#7c3aed"        # violet
CLR_ACCENT_DIM = "#6d28d9"
CLR_INPUT = "#06b6d4"          # cyan/teal
CLR_OUTPUT = "#f472b6"         # pink
CLR_CACHED = "#34d399"         # emerald
CLR_COST_LOW = "#34d399"
CLR_COST_MED = "#fbbf24"      # amber
CLR_COST_HIGH = "#ef4444"     # red
CLR_BORDER = "#4c1d95"        # deep violet
CLR_HEADER_BG = "#4c1d95"
CLR_DIM = "#9ca3af"            # gray-400
CLR_TEXT = "#e5e7eb"           # gray-200
CLR_BOLD = "#f9fafb"           # gray-50
CLR_SURFACE = "#1e1b4b"       # indigo-950
CLR_TODAY = "#fbbf24"          # highlight today

CLR_PRO = "#a78bfa"           # light violet for Pro plan badge

DEFAULT_BUDGET_CONFIG = {
    "daily_budget": 5.0,
    "monthly_budget": 50.0,
    "plan": "auto",  # "pro" = Claude Pro (all tokens free), "api" = API billing, "auto" = detect from telemetry
}

TIME_RANGES: dict[str, timedelta | None] = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


@dataclass
class SessionStats:
    session_id: str
    project: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    message_count: int = 0
    jsonl_path: Path | None = None


@dataclass
class DashboardData:
    sessions: dict[str, SessionStats] = field(default_factory=dict)
    daily_tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    model_totals: dict[str, dict[str, float]] = field(default_factory=dict)
    total_cost: float = 0.0
    daily_cost: dict[str, float] = field(default_factory=dict)
    interface_totals: dict[str, dict[str, int | float]] = field(default_factory=dict)
    project_totals: dict[str, dict[str, int | float]] = field(default_factory=dict)


SORT_KEYS = ["date", "cost", "tokens", "msgs", "project"]


@dataclass
class DashboardState:
    selected_idx: int | None = None
    time_range: str = "all"
    detail_view: bool = False
    pulse_tick: int = 0
    needs_refresh: bool = False
    sort_key: str = "date"


def _detect_plan() -> str:
    """Detect plan from telemetry subscriptionType or provider."""
    if not TELEMETRY_DIR.exists():
        return "api"
    for f in TELEMETRY_DIR.glob("1p_failed_events.*.json"):
        try:
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ed = d.get("event_data", {})
                name = ed.get("event_name", "")
                meta_str = ed.get("additional_metadata", "")
                if not meta_str:
                    continue
                meta = json.loads(meta_str)
                if name == "tengu_startup_manual_model_config":
                    sub = meta.get("subscriptionType", "")
                    if sub in ("pro", "max"):
                        return "pro"
                    if sub:
                        return "api"
                if name == "tengu_api_success":
                    if meta.get("provider") == "firstParty":
                        return "pro"
        except (json.JSONDecodeError, OSError):
            continue
    return "api"


ENTRYPOINT_LABELS: dict[str, str] = {
    "claude": "CLI",
    "vscode": "VS Code",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "jetbrains": "JetBrains",
}


def parse_telemetry_interfaces() -> dict[str, dict[str, int | float]]:
    """Parse telemetry for per-session interface (entrypoint) and isTTY info.

    Returns session_id -> {"entrypoint": str, "isTTY": bool} but we aggregate
    into interface_totals: label -> {"sessions": int, "tokens": int}.
    We return per-session entrypoint mapping instead.
    """
    session_interface: dict[str, str] = {}
    if not TELEMETRY_DIR.exists():
        return session_interface  # type: ignore
    for f in TELEMETRY_DIR.glob("1p_failed_events.*.json"):
        try:
            # Extract session_id from filename: 1p_failed_events.<session_id>.<uuid>.json
            parts = f.stem.split(".")
            if len(parts) >= 2:
                sid = parts[1]
            else:
                continue
            for line in f.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                ed = d.get("event_data", {})
                if ed.get("event_name") != "tengu_init":
                    continue
                meta = json.loads(ed.get("additional_metadata", "{}"))
                entrypoint = meta.get("entrypoint", "unknown")
                is_tty = meta.get("isTTY", True)
                label = ENTRYPOINT_LABELS.get(entrypoint, entrypoint)
                if not is_tty and entrypoint == "claude":
                    label = "CLI (piped)"
                session_interface[sid] = label
                break
        except (json.JSONDecodeError, OSError):
            continue
    return session_interface  # type: ignore


def load_config() -> dict:
    if BUDGET_CONFIG_PATH.exists():
        try:
            cfg = json.loads(BUDGET_CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            cfg = dict(DEFAULT_BUDGET_CONFIG)
    else:
        cfg = dict(DEFAULT_BUDGET_CONFIG)
        save_default_config()
    if cfg.get("plan") == "auto":
        cfg["plan"] = _detect_plan()
    return cfg


def save_default_config() -> None:
    try:
        BUDGET_CONFIG_PATH.write_text(json.dumps(DEFAULT_BUDGET_CONFIG, indent=2))
    except OSError:
        pass


def _parse_ts(ts: str | int | float | None) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _project_name(path: str) -> str:
    name = Path(path).name
    # Project dirs encode the full path with / replaced by -
    # e.g. "-Users-alice-Code-myapp" -> "myapp"
    home_encoded = str(Path.home()).replace("/", "-")
    if name.startswith(home_encoded):
        remainder = name[len(home_encoded):].lstrip("-")
        if remainder:
            return remainder.replace("-", " ")
        return "Claude"
    # Fallback without leading dash
    home_no_dash = home_encoded.lstrip("-")
    idx = name.find(home_no_dash)
    if idx >= 0:
        remainder = name[idx + len(home_no_dash):].lstrip("-")
        if remainder:
            return remainder.replace("-", " ")
    return "Claude"


def _estimate_cost(model: str, inp: int, out: int, cached: int) -> float:
    rates = COST_PER_M.get(model)
    if not rates:
        for key, val in COST_PER_M.items():
            if key.split("-")[1] in model:
                rates = val
                break
    if not rates:
        rates = COST_PER_M["claude-sonnet-4-6"]
    return (inp * rates["input"] + out * rates["output"] + cached * rates["cached"]) / 1_000_000


def _read_jsonl(path: Path) -> list[dict]:
    results = []
    try:
        text = path.read_text(errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return results


def parse_session_files() -> dict[str, SessionStats]:
    sessions: dict[str, SessionStats] = {}
    if not PROJECTS_DIR.exists():
        return sessions
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        proj = _project_name(str(project_dir))
        for jsonl in project_dir.glob("*.jsonl"):
            sid = jsonl.stem
            for line in _read_jsonl(jsonl):
                if not isinstance(line, dict):
                    continue
                msg = line.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                model = msg.get("model", line.get("model", "unknown"))
                if sid not in sessions:
                    sessions[sid] = SessionStats(
                        session_id=sid, project=proj, model=model, jsonl_path=jsonl
                    )
                s = sessions[sid]
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cached = usage.get("cache_read_input_tokens", 0)
                s.input_tokens += inp
                s.output_tokens += out
                s.cached_tokens += cached
                s.message_count += 1
                ts = _parse_ts(line.get("timestamp"))
                if ts:
                    if s.first_ts is None or ts < s.first_ts:
                        s.first_ts = ts
                    if s.last_ts is None or ts > s.last_ts:
                        s.last_ts = ts
    return sessions


def parse_telemetry() -> dict[str, float]:
    costs: dict[str, float] = {}
    if not TELEMETRY_DIR.exists():
        return costs
    for f in TELEMETRY_DIR.glob("1p_failed_events.*.json"):
        for line in _read_jsonl(f):
            if not isinstance(line, dict):
                continue
            ed = line.get("event_data", {})
            if ed.get("event_name") != "tengu_api_success":
                continue
            sid = ed.get("session_id", "")
            meta_str = ed.get("additional_metadata", "")
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
            except (json.JSONDecodeError, TypeError):
                continue
            cost = meta.get("costUSD", 0)
            if cost and sid:
                costs[sid] = costs.get(sid, 0) + cost
    return costs


def gather_data(since: datetime | None = None) -> DashboardData:
    data = DashboardData()
    all_sessions = parse_session_files()
    telemetry_costs = parse_telemetry()
    session_interfaces = parse_telemetry_interfaces()

    for sid, s in all_sessions.items():
        if sid in telemetry_costs:
            s.cost_usd = telemetry_costs[sid]
        else:
            s.cost_usd = _estimate_cost(s.model, s.input_tokens, s.output_tokens, s.cached_tokens)

        if since and s.first_ts and s.first_ts < since:
            continue

        data.sessions[sid] = s

        if s.first_ts:
            day = s.first_ts.strftime("%Y-%m-%d")
            if day not in data.daily_tokens:
                data.daily_tokens[day] = {"input": 0, "output": 0, "cached": 0}
            data.daily_tokens[day]["input"] += s.input_tokens
            data.daily_tokens[day]["output"] += s.output_tokens
            data.daily_tokens[day]["cached"] += s.cached_tokens
            data.daily_cost[day] = data.daily_cost.get(day, 0.0) + s.cost_usd

        model_key = s.model.split("-20")[0] if "-20" in s.model else s.model
        if model_key not in data.model_totals:
            data.model_totals[model_key] = {"input": 0, "output": 0, "cached": 0, "cost": 0.0, "sessions": 0}
        data.model_totals[model_key]["input"] += s.input_tokens
        data.model_totals[model_key]["output"] += s.output_tokens
        data.model_totals[model_key]["cached"] += s.cached_tokens
        data.model_totals[model_key]["cost"] += s.cost_usd
        data.model_totals[model_key]["sessions"] += 1

        # Interface aggregation
        iface = session_interfaces.get(sid, "CLI")
        if iface not in data.interface_totals:
            data.interface_totals[iface] = {"sessions": 0, "tokens": 0, "cost": 0.0}
        data.interface_totals[iface]["sessions"] += 1
        data.interface_totals[iface]["tokens"] += s.input_tokens + s.output_tokens + s.cached_tokens
        data.interface_totals[iface]["cost"] += s.cost_usd

        # Project aggregation
        proj = s.project
        if proj not in data.project_totals:
            data.project_totals[proj] = {"sessions": 0, "tokens": 0, "cost": 0.0, "messages": 0}
        data.project_totals[proj]["sessions"] += 1
        data.project_totals[proj]["tokens"] += s.input_tokens + s.output_tokens + s.cached_tokens
        data.project_totals[proj]["cost"] += s.cost_usd
        data.project_totals[proj]["messages"] += s.message_count

        data.total_cost += s.cost_usd

    return data


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _color_cost(cost: float) -> str:
    if cost < 0.01:
        return CLR_COST_LOW
    if cost < 0.10:
        return CLR_COST_MED
    return CLR_COST_HIGH


def _bar(value: int, max_val: int, width: int = 20) -> str:
    if max_val == 0:
        return ""
    ratio = min(value / max_val, 1.0)
    filled = int(ratio * width)
    char_idx = min(int(ratio * (len(BAR_CHARS) - 1)), len(BAR_CHARS) - 1)
    return BAR_CHARS[char_idx] * filled


def _today_cost(data: DashboardData) -> float:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return data.daily_cost.get(today, 0.0)


def _today_tokens(data: DashboardData) -> int:
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    d = data.daily_tokens.get(today, {})
    return d.get("input", 0) + d.get("output", 0) + d.get("cached", 0)


def _month_cost(data: DashboardData) -> float:
    now = datetime.now(tz=timezone.utc)
    prefix = now.strftime("%Y-%m-")
    return sum(v for k, v in data.daily_cost.items() if k.startswith(prefix))


def _compute_forecast(data: DashboardData) -> tuple[float, float, str]:
    """Returns (projected_month_cost, avg_daily, trend_arrow)."""
    now = datetime.now(tz=timezone.utc)
    costs_7d: list[float] = []
    for i in range(7):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        costs_7d.append(data.daily_cost.get(day, 0.0))

    avg_daily = sum(costs_7d) / 7 if costs_7d else 0.0

    import calendar
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - now.day
    month_so_far = _month_cost(data)
    projected = month_so_far + avg_daily * days_remaining

    if len(costs_7d) >= 4:
        first_half = sum(costs_7d[4:]) / max(len(costs_7d[4:]), 1)
        second_half = sum(costs_7d[:3]) / 3
        if second_half > first_half * 1.15:
            trend = "\u2191"  # up
        elif second_half < first_half * 0.85:
            trend = "\u2193"  # down
        else:
            trend = "\u2192"  # flat
    else:
        trend = "\u2192"

    return projected, avg_daily, trend


def _detect_live_sessions(data: DashboardData) -> list[str]:
    now = datetime.now(tz=timezone.utc)
    threshold = now - timedelta(minutes=5)
    live: list[str] = []
    for sid, s in data.sessions.items():
        if s.last_ts and s.last_ts > threshold:
            if s.jsonl_path and s.jsonl_path.exists():
                try:
                    mtime = datetime.fromtimestamp(s.jsonl_path.stat().st_mtime, tz=timezone.utc)
                    if mtime > threshold:
                        live.append(sid)
                        continue
                except OSError:
                    pass
            if s.last_ts > threshold:
                live.append(sid)
    return live


def render_budget(data: DashboardData, config: dict) -> Panel:
    daily_limit = config.get("daily_budget", 5.0)
    monthly_limit = config.get("monthly_budget", 50.0)
    today_cost = _today_cost(data)
    month_cost = _month_cost(data)
    is_pro = config.get("plan") == "pro"

    def budget_bar(spent: float, limit: float, label: str) -> Text:
        pct = min(spent / limit, 1.0) if limit > 0 else 0.0
        bar_width = 20
        filled = int(pct * bar_width)
        empty = bar_width - filled

        if is_pro:
            color = CLR_PRO
        elif pct > 0.9:
            color = CLR_COST_HIGH
        elif pct > 0.6:
            color = CLR_COST_MED
        else:
            color = CLR_COST_LOW

        t = Text()
        t.append(f"  {label} ", style=CLR_DIM)
        t.append("\u25b0" * filled, style=f"bold {color}")
        t.append("\u25b1" * empty, style=CLR_DIM)
        suffix = " equiv" if is_pro else ""
        t.append(f" ${spent:.2f}/${limit:.0f}{suffix}", style=f"bold {color}")
        t.append(f" ({pct * 100:.0f}%)", style=CLR_DIM)
        return t

    text = Text()
    if is_pro:
        text.append("  PRO ", style=f"bold on {CLR_ACCENT}")
        text.append(" ", style=CLR_DIM)
    daily_bar = budget_bar(today_cost, daily_limit, "Daily ")
    monthly_bar = budget_bar(month_cost, monthly_limit, "Month ")
    text.append_text(daily_bar)
    text.append("    ")
    text.append_text(monthly_bar)

    if is_pro:
        border = CLR_ACCENT
    elif daily_limit and today_cost / daily_limit > 0.9:
        border = CLR_COST_HIGH
    else:
        border = CLR_BORDER
    return Panel(
        text,
        border_style=Style(color=border),
        padding=(0, 0),
    )


def render_summary(data: DashboardData, state: DashboardState, config: dict) -> Panel:
    total_input = sum(s.input_tokens for s in data.sessions.values())
    total_output = sum(s.output_tokens for s in data.sessions.values())
    total_cached = sum(s.cached_tokens for s in data.sessions.values())
    total_tokens = total_input + total_output + total_cached
    n = len(data.sessions)
    today_cost = _today_cost(data)
    today_tok = _today_tokens(data)
    live_sids = _detect_live_sessions(data)
    is_pro = config.get("plan") == "pro"

    text = Text()

    # Plan badge
    if is_pro:
        text.append("  PRO PLAN ", style=f"bold on {CLR_ACCENT}")
        text.append("  All tokens included\n\n", style=CLR_PRO)

    text.append("  SESSIONS       ", style=CLR_DIM)
    text.append(f"{n}\n", style=f"bold {CLR_BOLD}")
    text.append("  TOTAL TOKENS   ", style=CLR_DIM)
    text.append(f"{_fmt_tokens(total_tokens)}\n", style=f"bold {CLR_BOLD}")
    text.append("    Input        ", style=CLR_DIM)
    text.append(f"{_fmt_tokens(total_input)}\n", style=CLR_INPUT)
    text.append("    Output       ", style=CLR_DIM)
    text.append(f"{_fmt_tokens(total_output)}\n", style=CLR_OUTPUT)
    text.append("    Cached       ", style=CLR_DIM)
    text.append(f"{_fmt_tokens(total_cached)}\n", style=CLR_CACHED)
    text.append("\n")

    if is_pro:
        text.append("  API EQUIVALENT ", style=CLR_DIM)
        text.append(f"${data.total_cost:.4f}", style=f"bold {CLR_PRO}")
        text.append("  (saved)\n", style=CLR_COST_LOW)
        text.append("  TODAY VALUE    ", style=CLR_DIM)
        text.append(f"${today_cost:.4f}", style=f"bold {CLR_PRO}")
        text.append(f"  ({_fmt_tokens(today_tok)} tokens)\n", style=CLR_DIM)
        text.append("  ACTUAL COST    ", style=CLR_DIM)
        text.append("$0.00", style=f"bold {CLR_COST_LOW}")
        text.append("  (included in Pro)\n", style=CLR_DIM)
    else:
        text.append("  TOTAL COST     ", style=CLR_DIM)
        text.append(f"${data.total_cost:.4f}\n", style=f"bold {_color_cost(data.total_cost)}")
        text.append("  TODAY COST     ", style=CLR_DIM)
        text.append(f"${today_cost:.4f}", style=f"bold {CLR_TODAY}")
        text.append(f"  ({_fmt_tokens(today_tok)} tokens)\n", style=CLR_DIM)

    if n > 0:
        avg = total_tokens / n
        avg_cost = data.total_cost / n
        text.append("  AVG/SESSION    ", style=CLR_DIM)
        label = "equiv" if is_pro else "cost"
        text.append(f"{_fmt_tokens(int(avg))} tok, ${avg_cost:.4f} {label}\n", style=CLR_DIM)

    # Forecast
    projected, avg_daily, trend = _compute_forecast(data)
    text.append("\n")
    if is_pro:
        text.append(f"  FORECAST (30d) ", style=CLR_DIM)
        text.append(f"${projected:.2f} equiv {trend}\n", style=f"bold {CLR_PRO}")
        text.append(f"  7d avg         ", style=CLR_DIM)
        text.append(f"${avg_daily:.2f}/day equiv\n", style=CLR_DIM)
    else:
        text.append(f"  FORECAST (30d) ", style=CLR_DIM)
        text.append(f"${projected:.2f} {trend}\n", style=f"bold {CLR_COST_MED}")
        text.append(f"  7d avg         ", style=CLR_DIM)
        text.append(f"${avg_daily:.2f}/day\n", style=CLR_DIM)

    # Live sessions
    if live_sids:
        pulse_char = "\u25cf" if state.pulse_tick % 2 == 0 else "\u25cb"
        pulse_color = CLR_ACCENT if state.pulse_tick % 2 == 0 else CLR_DIM
        text.append(f"\n  {pulse_char} ", style=f"bold {pulse_color}")
        text.append(f"LIVE: {len(live_sids)} active session{'s' if len(live_sids) != 1 else ''}\n",
                     style=f"bold {CLR_COST_LOW}")

    return Panel(
        text,
        title=f"[bold {CLR_ACCENT}] SUMMARY [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(1, 0),
    )


def render_token_chart(data: DashboardData) -> Panel:
    if not data.daily_tokens:
        return Panel(
            Text("  No daily data yet", style=CLR_DIM),
            title=f"[bold {CLR_ACCENT}] TOKENS (14d) [/bold {CLR_ACCENT}]",
            border_style=Style(color=CLR_BORDER),
        )

    today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    days = sorted(data.daily_tokens.keys())[-14:]
    max_total = max(
        (data.daily_tokens[d]["input"] + data.daily_tokens[d]["output"] + data.daily_tokens[d]["cached"])
        for d in days
    )

    text = Text()
    for day in days:
        d = data.daily_tokens[day]
        total = d["input"] + d["output"] + d["cached"]
        label = day[5:]  # MM-DD
        is_today = day == today_str
        label_style = f"bold {CLR_TODAY}" if is_today else CLR_DIM
        marker = ">" if is_today else " "
        text.append(f" {marker}{label} ", style=label_style)
        bar_width = 28
        if max_total > 0:
            inp_w = max(int(d["input"] / max_total * bar_width), 0)
            out_w = max(int(d["output"] / max_total * bar_width), 0)
            cac_w = max(int(d["cached"] / max_total * bar_width), 0)
            text.append("\u2501" * inp_w, style=CLR_INPUT)
            text.append("\u2501" * out_w, style=CLR_OUTPUT)
            text.append("\u2501" * cac_w, style=CLR_CACHED)
        text.append(f" {_fmt_tokens(total)}\n", style=CLR_DIM)

    text.append("\n ")
    text.append(" \u2501 Input ", style=CLR_INPUT)
    text.append(" \u2501 Output ", style=CLR_OUTPUT)
    text.append(" \u2501 Cached", style=CLR_CACHED)

    return Panel(
        text,
        title=f"[bold {CLR_ACCENT}] TOKENS (14d) [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(1, 0),
    )


def _duration_str(s: SessionStats) -> str:
    if not s.first_ts or not s.last_ts:
        return "-"
    delta = s.last_ts - s.first_ts
    mins = int(delta.total_seconds() / 60)
    if mins < 1:
        return "<1m"
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h{mins % 60}m"


def _sorted_sessions(data: DashboardData, sort_key: str = "date") -> list[SessionStats]:
    if sort_key == "cost":
        return sorted(data.sessions.values(), key=lambda s: s.cost_usd, reverse=True)
    if sort_key == "tokens":
        return sorted(
            data.sessions.values(),
            key=lambda s: s.input_tokens + s.output_tokens + s.cached_tokens,
            reverse=True,
        )
    if sort_key == "msgs":
        return sorted(data.sessions.values(), key=lambda s: s.message_count, reverse=True)
    if sort_key == "project":
        return sorted(data.sessions.values(), key=lambda s: (s.project.lower(), -(s.first_ts or datetime.min.replace(tzinfo=timezone.utc)).timestamp()))
    # Default: date
    return sorted(
        data.sessions.values(),
        key=lambda s: s.first_ts or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def render_sessions(data: DashboardData, state: DashboardState, config: dict | None = None) -> Panel:
    live_sids = set(_detect_live_sessions(data))

    table = Table(
        expand=True,
        show_header=True,
        header_style=f"bold {CLR_ACCENT}",
        padding=(0, 1),
        border_style=Style(color=CLR_BORDER),
        show_lines=False,
    )
    table.add_column("", width=1)
    table.add_column("Date", style=CLR_DIM, width=12)
    table.add_column("Project", style=CLR_TEXT)
    table.add_column("Model", style=CLR_DIM)
    table.add_column("Dur", justify="right", style=CLR_DIM, width=5)
    table.add_column("Msgs", justify="right", style=CLR_DIM, width=4)
    table.add_column("Tokens", justify="right")
    is_pro = (config or {}).get("plan") == "pro"
    cost_label = "Equiv" if is_pro else "Cost"
    table.add_column(cost_label, justify="right")

    sorted_sess = _sorted_sessions(data, state.sort_key)[:15]

    for idx, s in enumerate(sorted_sess):
        total = s.input_tokens + s.output_tokens + s.cached_tokens
        if total < 1000:
            tok_color = CLR_COST_LOW
        elif total < 100_000:
            tok_color = CLR_COST_MED
        else:
            tok_color = CLR_COST_HIGH

        date_str = s.first_ts.strftime("%m-%d %H:%M") if s.first_ts else "?"
        model_short = s.model.replace("claude-", "").split("-20")[0]

        is_selected = state.selected_idx == idx
        row_style = f"on {CLR_ACCENT_DIM}" if is_selected else ""

        # Live indicator
        if s.session_id in live_sids:
            pulse_color = CLR_ACCENT if state.pulse_tick % 2 == 0 else CLR_DIM
            live_marker = Text("\u25cf", style=f"bold {pulse_color}")
        else:
            live_marker = Text(" ")

        cost_color = CLR_PRO if is_pro else _color_cost(s.cost_usd)
        table.add_row(
            live_marker,
            Text(date_str, style=f"{CLR_DIM} {row_style}"),
            Text(s.project[:18], style=f"{CLR_TEXT} {row_style}"),
            Text(model_short, style=f"{CLR_DIM} {row_style}"),
            Text(_duration_str(s), style=f"{CLR_DIM} {row_style}"),
            Text(str(s.message_count), style=f"{CLR_DIM} {row_style}"),
            Text(_fmt_tokens(total), style=f"{tok_color} {row_style}"),
            Text(f"${s.cost_usd:.4f}", style=f"{cost_color} {row_style}"),
        )

    sort_label = state.sort_key.upper()
    return Panel(
        table,
        title=f"[bold {CLR_ACCENT}] SESSIONS [/bold {CLR_ACCENT}][{CLR_DIM}] by {sort_label} [/{CLR_DIM}]",
        border_style=Style(color=CLR_BORDER),
        padding=(0, 0),
    )


def render_session_detail(data: DashboardData, state: DashboardState, config: dict | None = None) -> Panel:
    sorted_sess = _sorted_sessions(data, state.sort_key)
    if state.selected_idx is None or state.selected_idx >= len(sorted_sess):
        return Panel(Text("  No session selected", style=CLR_DIM), border_style=Style(color=CLR_BORDER))

    session = sorted_sess[state.selected_idx]
    text = Text()
    text.append(f"  Project: ", style=CLR_DIM)
    text.append(f"{session.project}\n", style=f"bold {CLR_BOLD}")
    text.append(f"  Session: ", style=CLR_DIM)
    text.append(f"{session.session_id[:16]}...\n", style=CLR_TEXT)
    text.append(f"  Model:   ", style=CLR_DIM)
    text.append(f"{session.model}\n", style=CLR_INPUT)

    date_str = session.first_ts.strftime("%Y-%m-%d %H:%M:%S") if session.first_ts else "?"
    text.append(f"  Started: ", style=CLR_DIM)
    text.append(f"{date_str}    Duration: {_duration_str(session)}\n\n", style=CLR_TEXT)

    # Token breakdown
    total = session.input_tokens + session.output_tokens + session.cached_tokens
    if total > 0:
        inp_pct = session.input_tokens / total * 100
        out_pct = session.output_tokens / total * 100
        cac_pct = session.cached_tokens / total * 100

        text.append("  TOKEN BREAKDOWN\n", style=f"bold {CLR_ACCENT}")
        bar_w = 40

        inp_w = max(int(inp_pct / 100 * bar_w), 0)
        out_w = max(int(out_pct / 100 * bar_w), 0)
        cac_w = max(int(cac_pct / 100 * bar_w), 0)
        text.append("  ")
        text.append("\u2588" * inp_w, style=CLR_INPUT)
        text.append("\u2588" * out_w, style=CLR_OUTPUT)
        text.append("\u2588" * cac_w, style=CLR_CACHED)
        text.append("\n")

        text.append(f"    Input:  {_fmt_tokens(session.input_tokens)}", style=CLR_INPUT)
        text.append(f" ({inp_pct:.1f}%)\n", style=CLR_DIM)
        text.append(f"    Output: {_fmt_tokens(session.output_tokens)}", style=CLR_OUTPUT)
        text.append(f" ({out_pct:.1f}%)\n", style=CLR_DIM)
        text.append(f"    Cached: {_fmt_tokens(session.cached_tokens)}", style=CLR_CACHED)
        text.append(f" ({cac_pct:.1f}%)\n", style=CLR_DIM)

    is_pro = (config or {}).get("plan") == "pro"
    if is_pro:
        text.append(f"\n  API EQUIV  ", style=CLR_DIM)
        text.append(f"${session.cost_usd:.4f}", style=f"bold {CLR_PRO}")
        text.append("  (free with Pro)\n", style=CLR_COST_LOW)
    else:
        text.append(f"\n  COST       ", style=CLR_DIM)
        text.append(f"${session.cost_usd:.4f}\n", style=f"bold {_color_cost(session.cost_usd)}")
    text.append(f"  MESSAGES   ", style=CLR_DIM)
    text.append(f"{session.message_count}\n", style=f"bold {CLR_BOLD}")

    # Parse message timeline from JSONL
    if session.jsonl_path and session.jsonl_path.exists():
        text.append("\n  MESSAGE TIMELINE\n", style=f"bold {CLR_ACCENT}")
        messages = _read_jsonl(session.jsonl_path)
        count = 0
        running_cost = 0.0
        for line in messages:
            if not isinstance(line, dict):
                continue
            msg = line.get("message")
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            count += 1
            if count > 20:
                text.append(f"    ... and {len(messages) - 20} more\n", style=CLR_DIM)
                break

            ts = _parse_ts(line.get("timestamp"))
            ts_str = ts.strftime("%H:%M:%S") if ts else "??:??:??"
            role = msg.get("role", "?")
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cached = usage.get("cache_read_input_tokens", 0)
            msg_cost = _estimate_cost(session.model, inp, out, cached)
            running_cost += msg_cost

            # Check for tool use
            tools: list[str] = []
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tools.append(block.get("name", "?"))

            role_color = CLR_INPUT if role == "assistant" else CLR_OUTPUT
            text.append(f"    {ts_str} ", style=CLR_DIM)
            text.append(f"{role:<10}", style=role_color)
            text.append(f" {_fmt_tokens(inp + out + cached):>6}", style=CLR_DIM)
            if tools:
                text.append(f"  [{', '.join(tools[:3])}]", style=CLR_CACHED)
            text.append(f"  ${running_cost:.4f}\n", style=CLR_DIM)

    title_str = session.first_ts.strftime("%m-%d %H:%M") if session.first_ts else ""
    return Panel(
        text,
        title=f"[bold {CLR_ACCENT}] SESSION DETAIL: {session.project}  {title_str} [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(1, 0),
    )


def render_models(data: DashboardData) -> Panel:
    if not data.model_totals:
        return Panel(
            Text("  No model data", style=CLR_DIM),
            title=f"[bold {CLR_ACCENT}] MODELS [/bold {CLR_ACCENT}]",
            border_style=Style(color=CLR_BORDER),
        )

    text = Text()
    max_tokens = max(
        (m["input"] + m["output"] + m["cached"]) for m in data.model_totals.values()
    ) or 1

    for model, stats in sorted(data.model_totals.items(), key=lambda x: x[1]["cost"], reverse=True):
        total = stats["input"] + stats["output"] + stats["cached"]
        model_short = model.replace("claude-", "")
        pct = (stats["cost"] / data.total_cost * 100) if data.total_cost > 0 else 0
        text.append(f"  {model_short}", style=f"bold {CLR_BOLD}")
        text.append(f"  {pct:.0f}%\n", style=CLR_DIM)
        text.append(f"    {int(stats['sessions'])} sessions", style=CLR_DIM)
        text.append(f"  {_fmt_tokens(total)} tok", style=CLR_INPUT)
        text.append(f"  ${stats['cost']:.4f}\n", style=_color_cost(stats["cost"]))

        bar_w = 28
        inp_w = max(int(stats["input"] / max_tokens * bar_w), 0)
        out_w = max(int(stats["output"] / max_tokens * bar_w), 0)
        cac_w = max(int(stats["cached"] / max_tokens * bar_w), 0)
        text.append("    ")
        text.append("\u25b0" * inp_w, style=CLR_INPUT)
        text.append("\u25b0" * out_w, style=CLR_OUTPUT)
        text.append("\u25b0" * cac_w, style=CLR_CACHED)
        remaining = bar_w - inp_w - out_w - cac_w
        if remaining > 0:
            text.append("\u25b1" * remaining, style=CLR_DIM)
        text.append("\n\n")

    text.append("  ")
    text.append(" \u25b0 Input ", style=CLR_INPUT)
    text.append(" \u25b0 Output ", style=CLR_OUTPUT)
    text.append(" \u25b0 Cached", style=CLR_CACHED)

    # Interface breakdown
    if data.interface_totals:
        text.append("\n\n")
        text.append("  INTERFACE\n", style=f"bold {CLR_ACCENT}")
        total_sessions = sum(int(v["sessions"]) for v in data.interface_totals.values()) or 1
        for iface, stats in sorted(data.interface_totals.items(), key=lambda x: -int(x[1]["sessions"])):
            pct = int(stats["sessions"]) / total_sessions * 100
            bar_w = 16
            filled = max(int(pct / 100 * bar_w), 1) if pct > 0 else 0
            text.append(f"  {iface:<12}", style=f"bold {CLR_BOLD}")
            text.append("\u2588" * filled, style=CLR_INPUT)
            text.append("\u2591" * (bar_w - filled), style=CLR_DIM)
            text.append(f" {int(stats['sessions'])} sess", style=CLR_DIM)
            text.append(f" ({pct:.0f}%)\n", style=CLR_DIM)

    return Panel(
        text,
        title=f"[bold {CLR_ACCENT}] MODELS [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(1, 0),
    )


def render_heatmap(data: DashboardData) -> Panel:
    # Build 7x24 grid: rows = days of week (Mon-Sun), cols = hours (0-23)
    grid: list[list[int]] = [[0] * 24 for _ in range(7)]

    for s in data.sessions.values():
        if s.first_ts:
            dow = s.first_ts.weekday()  # 0=Mon
            hour = s.first_ts.hour
            grid[dow][hour] += s.message_count

    max_val = max(max(row) for row in grid) or 1
    heat_chars = "\u00b7\u2591\u2592\u2593\u2588"  # ·░▒▓█
    heat_colors = [CLR_DIM, CLR_DIM, CLR_INPUT, CLR_ACCENT, CLR_COST_HIGH]

    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    text = Text()

    # Hour labels
    text.append("     ", style=CLR_DIM)
    for h in range(24):
        if h % 3 == 0:
            text.append(f"{h:>2} ", style=CLR_DIM)
        else:
            text.append("   ")
    text.append("\n")

    for dow in range(7):
        text.append(f"  {day_labels[dow]} ", style=CLR_DIM)
        for h in range(24):
            v = grid[dow][h]
            if v == 0:
                idx = 0
            else:
                ratio = v / max_val
                if ratio < 0.25:
                    idx = 1
                elif ratio < 0.5:
                    idx = 2
                elif ratio < 0.75:
                    idx = 3
                else:
                    idx = 4
            text.append(f" {heat_chars[idx]} ", style=heat_colors[idx])
        text.append("\n")

    return Panel(
        text,
        title=f"[bold {CLR_ACCENT}] ACTIVITY HEATMAP [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(0, 0),
    )


def render_projects(data: DashboardData, config: dict) -> Panel:
    if not data.project_totals:
        return Panel(
            Text("  No project data", style=CLR_DIM),
            title=f"[bold {CLR_ACCENT}] PROJECTS [/bold {CLR_ACCENT}]",
            border_style=Style(color=CLR_BORDER),
        )

    is_pro = config.get("plan") == "pro"
    cost_label = "Equiv" if is_pro else "Cost"

    table = Table(
        expand=True,
        show_header=True,
        header_style=f"bold {CLR_ACCENT}",
        padding=(0, 1),
        border_style=Style(color=CLR_BORDER),
        show_lines=False,
    )
    table.add_column("Project", style=CLR_TEXT)
    table.add_column("Sess", justify="right", style=CLR_DIM, width=4)
    table.add_column("Msgs", justify="right", style=CLR_DIM, width=5)
    table.add_column("Tokens", justify="right")
    table.add_column(cost_label, justify="right")
    table.add_column("", width=16)

    max_tokens = max(int(v["tokens"]) for v in data.project_totals.values()) or 1

    sorted_projects = sorted(
        data.project_totals.items(), key=lambda x: x[1]["cost"], reverse=True
    )

    for proj, stats in sorted_projects[:10]:
        tokens = int(stats["tokens"])
        cost = float(stats["cost"])
        bar_w = 14
        filled = max(int(tokens / max_tokens * bar_w), 1)
        bar = Text()
        bar.append("\u2588" * filled, style=CLR_INPUT)
        bar.append("\u2591" * (bar_w - filled), style=CLR_DIM)

        cost_color = CLR_PRO if is_pro else _color_cost(cost)
        table.add_row(
            Text(proj[:20], style=CLR_TEXT),
            str(int(stats["sessions"])),
            str(int(stats["messages"])),
            Text(_fmt_tokens(tokens), style=CLR_INPUT),
            Text(f"${cost:.4f}", style=cost_color),
            bar,
        )

    return Panel(
        table,
        title=f"[bold {CLR_ACCENT}] PROJECTS [/bold {CLR_ACCENT}]",
        border_style=Style(color=CLR_BORDER),
        padding=(0, 0),
    )


def build_layout(data: DashboardData, state: DashboardState, config: dict) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="budget", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )

    # Header with time range indicator
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_text = Text()
    header_text.append("  CLAUDE CODE ", style=f"bold {CLR_BOLD}")
    header_text.append("DASHBOARD", style=f"bold {CLR_ACCENT}")
    header_text.append(f"    {now}    ", style=CLR_DIM)
    for key, label in [("1d", "1d"), ("7d", "7d"), ("30d", "30d"), ("all", "all")]:
        if state.time_range == key:
            header_text.append(f" [{label}] ", style=f"bold {CLR_TODAY}")
        else:
            header_text.append(f"  {label}  ", style=CLR_DIM)
    layout["header"].update(
        Panel(header_text, border_style=Style(color=CLR_BORDER), style=f"on {CLR_SURFACE}")
    )

    # Budget bar
    layout["budget"].update(render_budget(data, config))

    # Footer
    sort_hint = f"sort:{state.sort_key}"
    footer = Text()
    footer.append(" q quit  r refresh  1/7/3/a filter  s sort  j/k nav  Enter detail  Esc back  ", style=CLR_DIM)
    footer.append(f"[{sort_hint}]", style=CLR_ACCENT)
    layout["footer"].update(footer)

    if state.detail_view and state.selected_idx is not None:
        # Detail mode
        layout["body"].split_column(
            Layout(name="top", ratio=2),
            Layout(name="detail", ratio=3),
        )
        layout["top"].split_row(
            Layout(name="summary"),
            Layout(name="tokens"),
        )
        layout["summary"].update(render_summary(data, state, config))
        layout["tokens"].update(render_token_chart(data))
        layout["detail"].update(render_session_detail(data, state, config))
    else:
        # Main view
        layout["body"].split_column(
            Layout(name="top", ratio=2),
            Layout(name="middle", ratio=2),
            Layout(name="bottom", ratio=3),
        )
        layout["top"].split_row(
            Layout(name="summary"),
            Layout(name="tokens"),
        )
        layout["middle"].split_row(
            Layout(name="projects", ratio=3),
            Layout(name="heatmap", ratio=2),
        )
        layout["bottom"].split_row(
            Layout(name="sessions", ratio=3),
            Layout(name="models", ratio=2),
        )

        layout["summary"].update(render_summary(data, state, config))
        layout["tokens"].update(render_token_chart(data))
        layout["projects"].update(render_projects(data, config))
        layout["heatmap"].update(render_heatmap(data))
        layout["sessions"].update(render_sessions(data, state, config))
        layout["models"].update(render_models(data))

    return layout


def main() -> None:
    console = Console()
    stop_event = threading.Event()
    state = DashboardState()
    config = load_config()
    state_lock = threading.Lock()

    def key_listener() -> None:
        import tty
        import termios

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop_event.is_set():
                ch = sys.stdin.read(1)
                with state_lock:
                    if ch in ("q", "Q"):
                        stop_event.set()
                    elif ch in ("r", "R"):
                        state.needs_refresh = True
                    elif ch == "1":
                        state.time_range = "1d"
                        state.needs_refresh = True
                    elif ch == "7":
                        state.time_range = "7d"
                        state.needs_refresh = True
                    elif ch == "3":
                        state.time_range = "30d"
                        state.needs_refresh = True
                    elif ch == "a":
                        state.time_range = "all"
                        state.needs_refresh = True
                    elif ch == "s":
                        idx = SORT_KEYS.index(state.sort_key)
                        state.sort_key = SORT_KEYS[(idx + 1) % len(SORT_KEYS)]
                        state.needs_refresh = True
                    elif ch == "j":
                        if state.selected_idx is None:
                            state.selected_idx = 0
                        else:
                            state.selected_idx += 1
                        state.needs_refresh = True
                    elif ch == "k":
                        if state.selected_idx is not None and state.selected_idx > 0:
                            state.selected_idx -= 1
                        state.needs_refresh = True
                    elif ch == "\r" or ch == "\n":
                        if state.selected_idx is not None:
                            state.detail_view = True
                            state.needs_refresh = True
                    elif ch == "\x1b":
                        # Escape - could be arrow key or plain Esc
                        import select
                        if select.select([sys.stdin], [], [], 0.05)[0]:
                            next_ch = sys.stdin.read(1)
                            if next_ch == "[":
                                arrow = sys.stdin.read(1)
                                if arrow == "A":  # up
                                    if state.selected_idx is not None and state.selected_idx > 0:
                                        state.selected_idx -= 1
                                    state.needs_refresh = True
                                elif arrow == "B":  # down
                                    if state.selected_idx is None:
                                        state.selected_idx = 0
                                    else:
                                        state.selected_idx += 1
                                    state.needs_refresh = True
                        else:
                            # Plain Esc
                            state.detail_view = False
                            state.needs_refresh = True
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    listener = threading.Thread(target=key_listener, daemon=True)
    listener.start()

    try:
        with Live(console=console, refresh_per_second=1, screen=True) as live:
            last_data_fetch = 0.0
            data = DashboardData()
            while not stop_event.is_set():
                now = time.monotonic()
                with state_lock:
                    needs_refresh = state.needs_refresh
                    state.needs_refresh = False
                    state.pulse_tick += 1

                # Determine refresh interval based on live sessions
                live_sids = _detect_live_sessions(data) if data.sessions else []
                refresh_interval = 5.0 if live_sids else 30.0

                if needs_refresh or (now - last_data_fetch) >= refresh_interval:
                    since = None
                    td = TIME_RANGES.get(state.time_range)
                    if td is not None:
                        since = datetime.now(tz=timezone.utc) - td
                    data = gather_data(since=since)
                    last_data_fetch = now

                    # Clamp selected_idx
                    with state_lock:
                        max_idx = min(len(data.sessions), 15) - 1
                        if state.selected_idx is not None and max_idx >= 0:
                            state.selected_idx = min(state.selected_idx, max_idx)
                        elif max_idx < 0:
                            state.selected_idx = None

                live.update(build_layout(data, state, config))
                stop_event.wait(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
