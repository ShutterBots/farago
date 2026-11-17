"""
Settings and forecasting maths for the bot.

Everything in this file is plain Python with no third-party imports, so it can be
unit-tested without the forecasting framework or any API keys
(`python -m pytest tests/`).

main.py holds everything that talks to models or to Metaculus.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

# Every model goes through OpenRouter, so one OPENROUTER_API_KEY covers them all.
# "opus" is the framework's own default forecaster. "argon" and "sol" copy the
# settings Metaculus uses for its benchmark bots (forecasting-tools, November
# 2026). "opus-high" is the least proven: Metaculus runs its high-reasoning
# Claude bots through Anthropic's API directly, while this asks OpenRouter for
# the same thing. If it is refused, that seat falls back to another model.
MODEL_PRESETS: dict[str, dict[str, Any]] = {
    # Forecasters
    "opus": {
        "model": "openrouter/anthropic/claude-opus-5.5",
        "temperature": 0.3,
        "timeout": 200,
    },
    "opus-high": {
        "model": "openrouter/anthropic/claude-opus-5.5",
        "reasoning_effort": "high",
        "temperature": None,
        "max_tokens": 32000,
        "timeout": 300,
    },
    "argon": {
        "model": "openrouter/google/gemini-4-argon",
        "reasoning_effort": "high",
        "temperature": None,
        "timeout": 300,
    },
    "sol": {
        "model": "openrouter/openai/gpt-6-sol",
        "reasoning_effort": "high",
        "temperature": None,
        "timeout": 420,
    },
    # Helpers
    "haiku": {
        "model": "openrouter/anthropic/claude-haiku-5.5",
        "temperature": 0.3,
        "timeout": 90,
    },
    "sonar-pro": {
        "model": "openrouter/perplexity/sonar-pro",
        "temperature": 0.1,
        "timeout": 150,
    },
    "sonar-reasoning-pro": {
        "model": "openrouter/perplexity/sonar-reasoning-pro",
        "temperature": None,
        "timeout": 240,
    },
}

RESEARCH_SOURCES = ("news", "background", "signals")

# A tier is a list of forecaster "seats" plus the research passes each question
# gets. The same name twice means two independent runs of that model.
#
# What drives cost is hidden reasoning, which is billed as output. A plain Opus
# answer costs a few cents; the same question at high reasoning effort costs
# roughly ten times that. So the cheaper tiers lean on plain Opus (the strongest
# family on Metaculus's model leaderboard) and add deep-reasoning seats from
# other labs for a second opinion. BOT_NOTES.md has the rough cost of each tier.
TIERS: dict[str, dict[str, tuple[str, ...]]] = {
    "lite": {
        "forecasters": ("opus", "opus", "opus"),
        "research": ("news",),
    },
    "standard": {
        "forecasters": ("opus", "opus", "opus", "argon"),
        "research": ("news", "background"),
    },
    "max": {
        "forecasters": ("opus-high", "opus-high", "opus", "argon", "sol"),
        "research": ("news", "background", "signals"),
    },
}

# Tried, in this order, only when every model on the panel has failed.
DEFAULT_BACKUP_MODELS = ("opus", "argon")


def resolve_model_spec(name: str) -> dict[str, Any]:
    """
    Turns a preset name ("opus") or a raw model id ("openrouter/x-ai/grok-4.7")
    into the keyword arguments for forecasting_tools.GeneralLlm.
    """
    key = name.strip()
    if key in MODEL_PRESETS:
        return dict(MODEL_PRESETS[key])
    if "/" in key:
        return {"model": key, "temperature": None, "timeout": 300}
    raise ValueError(
        f"Unknown model '{name}'. Use one of {sorted(MODEL_PRESETS)} "
        "or a full model id such as 'openrouter/x-ai/grok-4.7'."
    )


def fallback_order(
    seats: Sequence[str], seat_index: int, backups: Sequence[str] = ()
) -> list[str]:
    """
    The models to try for one forecast, best choice first: the seat's own model,
    then every other distinct model on the panel, then the backups (so a provider
    outage costs variety, not the forecast).
    """
    if not seats:
        raise ValueError("At least one forecaster is required")
    start = seat_index % len(seats)
    rotated = list(seats[start:]) + list(seats[:start]) + list(backups)
    ordered: list[str] = []
    for name in rotated:
        if name not in ordered:
            ordered.append(name)
    return ordered


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TierPlan:
    name: str
    forecasters: tuple[str, ...]
    research: tuple[str, ...]


@dataclass(frozen=True)
class BotSettings:
    main: TierPlan
    minibench: TierPlan | None  # None means MiniBench is switched off
    prob_floor: float  # yes/no forecasts stay inside [floor, 1 - floor]
    option_floor: float  # smallest probability any multiple-choice option gets
    max_usd_per_run: float  # stop taking new questions past this (0 = no cap)
    max_questions_per_run: int
    batch_size: int
    max_run_minutes: float
    dry_run: bool  # True = forecast but post nothing to Metaculus
    search_model: str
    parser_model: str
    backup_models: tuple[str, ...] = DEFAULT_BACKUP_MODELS


def _get(env: Mapping[str, str], key: str) -> str | None:
    value = env.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_float(
    env: Mapping[str, str], key: str, default: float, low: float, high: float
) -> float:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{key} must be a number, got '{raw}'") from error
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got '{raw}'")
    return value


def _parse_int(
    env: Mapping[str, str], key: str, default: int, low: int, high: int
) -> int:
    raw = _get(env, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{key} must be a whole number, got '{raw}'") from error
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}, got '{raw}'")
    return value


def _parse_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _build_tier(
    env: Mapping[str, str], tier_name: str, forecasters_key: str, research_key: str
) -> TierPlan:
    if tier_name not in TIERS:
        raise ValueError(
            f"Unknown tier '{tier_name}'. Choose one of {sorted(TIERS)}."
        )
    forecasters = TIERS[tier_name]["forecasters"]
    research = TIERS[tier_name]["research"]

    custom_forecasters = _get(env, forecasters_key)
    if custom_forecasters is not None:
        forecasters = _parse_list(custom_forecasters)
        if not forecasters:
            raise ValueError(f"{forecasters_key} is set but lists no models")
        for name in forecasters:
            resolve_model_spec(name)  # raises a readable error on a bad name

    custom_research = _get(env, research_key)
    if custom_research is not None:
        research = _parse_list(custom_research)
        unknown = [source for source in research if source not in RESEARCH_SOURCES]
        if unknown or not research:
            raise ValueError(
                f"{research_key} must list one or more of {RESEARCH_SOURCES}, "
                f"got '{custom_research}'"
            )

    return TierPlan(name=tier_name, forecasters=forecasters, research=research)


def load_settings(env: Mapping[str, str]) -> BotSettings:
    """
    Reads the bot's dials from a mapping of names to text values (the settings
    file merged with the environment, see merge_settings). All are optional and
    an empty value counts as unset.
    """
    main_tier = (_get(env, "BOT_TIER") or "standard").lower()
    main = _build_tier(env, main_tier, "BOT_FORECASTERS", "BOT_RESEARCH")

    minibench_tier = (_get(env, "BOT_MINIBENCH_TIER") or "lite").lower()
    if minibench_tier in ("off", "none", "false", "0"):
        minibench = None
    else:
        minibench = _build_tier(
            env, minibench_tier, "BOT_MINIBENCH_FORECASTERS", "BOT_MINIBENCH_RESEARCH"
        )

    search_model = _get(env, "BOT_SEARCH_MODEL") or "sonar-pro"
    parser_model = _get(env, "BOT_PARSER_MODEL") or "haiku"
    resolve_model_spec(search_model)
    resolve_model_spec(parser_model)

    raw_backups = _get(env, "BOT_BACKUP_MODELS")
    if raw_backups is None:
        backup_models = DEFAULT_BACKUP_MODELS
    elif raw_backups.lower() in ("off", "none", "false", "0"):
        backup_models = ()
    else:
        backup_models = _parse_list(raw_backups)
        for name in backup_models:
            resolve_model_spec(name)

    dry_run = (_get(env, "BOT_DRY_RUN") or "").lower() in ("1", "true", "yes", "on")

    return BotSettings(
        main=main,
        minibench=minibench,
        prob_floor=_parse_float(env, "BOT_PROB_FLOOR", 0.02, 0.001, 0.2),
        option_floor=_parse_float(env, "BOT_OPTION_FLOOR", 0.01, 0.001, 0.1),
        max_usd_per_run=_parse_float(env, "BOT_MAX_USD_PER_RUN", 6.0, 0.0, 1000.0),
        max_questions_per_run=_parse_int(env, "BOT_MAX_QUESTIONS_PER_RUN", 20, 1, 500),
        batch_size=_parse_int(env, "BOT_BATCH_SIZE", 5, 1, 50),
        max_run_minutes=_parse_float(env, "BOT_MAX_RUN_MINUTES", 40.0, 1.0, 300.0),
        dry_run=dry_run,
        search_model=search_model,
        parser_model=parser_model,
        backup_models=backup_models,
    )


SETTING_NAMES = (
    "BOT_TIER",
    "BOT_MINIBENCH_TIER",
    "BOT_FORECASTERS",
    "BOT_RESEARCH",
    "BOT_MINIBENCH_FORECASTERS",
    "BOT_MINIBENCH_RESEARCH",
    "BOT_BACKUP_MODELS",
    "BOT_SEARCH_MODEL",
    "BOT_PARSER_MODEL",
    "BOT_MAX_USD_PER_RUN",
    "BOT_MAX_QUESTIONS_PER_RUN",
    "BOT_BATCH_SIZE",
    "BOT_MAX_RUN_MINUTES",
    "BOT_PROB_FLOOR",
    "BOT_OPTION_FLOOR",
    "BOT_DRY_RUN",
)


def parse_settings_text(text: str) -> dict[str, str]:
    """
    Reads NAME=value lines from the settings file (bot_settings.txt). Blank lines
    and lines starting with # are skipped, and so is anything after " #" on a line.

    Only the bot's own settings are accepted. The file sits in a public
    repository, so a key or token pasted into it by mistake stops the run. The
    error names the line and never repeats what was on it.
    """
    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name = name.strip().upper()
        if not separator or name not in SETTING_NAMES:
            raise ValueError(
                f"line {number} of the settings file is not one of the bot's "
                "settings (NAME=value, with a name from BOT_NOTES.md). Keys and "
                "tokens never go in this file."
            )
        value = value.split(" #", 1)[0].strip().strip("\"'").strip()
        values[name] = value
    return values


def merge_settings(
    file_values: Mapping[str, str], env: Mapping[str, str]
) -> dict[str, str]:
    """
    The settings file gives the starting values and an environment variable with
    the same name wins. An empty variable counts as unset, which is what GitHub
    passes for a repository variable that does not exist.
    """
    merged = {
        name: value
        for name, value in file_values.items()
        if name in SETTING_NAMES and value.strip()
    }
    for name in SETTING_NAMES:
        value = _get(env, name)
        if value is not None:
            merged[name] = value
    return merged


def describe_settings(settings: BotSettings) -> str:
    """A few lines for the run log, so every run shows what it was set to do."""

    def tier_line(label: str, plan: TierPlan | None) -> str:
        if plan is None:
            return f"{label}: off"
        return (
            f"{label}: tier '{plan.name}', panel {', '.join(plan.forecasters)}, "
            f"research {', '.join(plan.research)}"
        )

    cost_cap = (
        f"${settings.max_usd_per_run:.2f}" if settings.max_usd_per_run > 0 else "none"
    )
    return "\n".join(
        [
            tier_line("Seasonal tournament", settings.main),
            tier_line("MiniBench", settings.minibench),
            f"Per run: cost cap {cost_cap}, at most "
            f"{settings.max_questions_per_run} questions, "
            f"{settings.max_run_minutes:.0f} minutes",
            f"Backups if the whole panel fails: "
            f"{', '.join(settings.backup_models) or 'none'}",
        ]
    )


# --------------------------------------------------------------------------- #
# Probabilities
# --------------------------------------------------------------------------- #


def clamp_probability(probability: float, floor: float) -> float:
    """
    Keeps a yes/no forecast inside [floor, 1 - floor]. A confident miss costs far
    more under log scoring than the little a cap gives up on sure things.
    """
    value = float(probability)
    if not math.isfinite(value):
        raise ValueError(f"Probability is not a number: {probability}")
    if not 0 < floor < 0.5:
        raise ValueError(f"Floor must be between 0 and 0.5, got {floor}")
    return min(1.0 - floor, max(floor, value))


def effective_option_floor(option_count: int, floor: float) -> float:
    """The per-option floor, shrunk when there are so many options it would eat the forecast."""
    if option_count < 1:
        raise ValueError("A question needs at least one option")
    return min(floor, 0.5 / option_count)


def floor_and_normalize(probabilities: Sequence[float], floor: float) -> list[float]:
    """
    Rescales option probabilities to sum to 1 with every option at or above
    `floor`. Options under the floor are pinned to it and the rest keep their
    proportions.
    """
    count = len(probabilities)
    if count == 0:
        raise ValueError("No probabilities given")
    if count == 1:
        return [1.0]
    if floor < 0 or floor * count >= 1:
        raise ValueError(
            f"A floor of {floor} cannot be given to each of {count} options"
        )

    cleaned = [
        max(0.0, float(p)) if math.isfinite(float(p)) else 0.0 for p in probabilities
    ]
    total = sum(cleaned)
    base = [1.0 / count] * count if total <= 0 else [p / total for p in cleaned]

    result = list(base)
    pinned: set[int] = set()
    while True:
        newly_pinned = {
            index
            for index, value in enumerate(result)
            if value < floor and index not in pinned
        }
        if not newly_pinned:
            break
        pinned |= newly_pinned
        free_mass = 1.0 - floor * len(pinned)
        free_total = sum(base[index] for index in range(count) if index not in pinned)
        if free_total <= 0:
            result = [1.0 / count] * count
            break
        result = [
            floor if index in pinned else base[index] * free_mass / free_total
            for index in range(count)
        ]

    # Put any floating-point drift on the largest option so the sum is exactly 1.
    largest = max(range(count), key=lambda index: result[index])
    result[largest] = 1.0 - sum(
        value for index, value in enumerate(result) if index != largest
    )
    return result


def _normalize_name(name: str) -> str:
    stripped = name.strip().strip("\"'`*").strip()
    return " ".join(stripped.casefold().split())


def _normalize_name_without_prefix(name: str) -> str:
    normalized = _normalize_name(name)
    if normalized.startswith("option "):
        normalized = normalized[len("option ") :].lstrip(":-. ").strip()
    return normalized


def align_options(
    parsed: Sequence[tuple[str, float]], expected: Sequence[str]
) -> list[float]:
    """
    Maps the options a model answered with onto the question's real option names
    and returns the probabilities in the question's order. Raises if an option is
    missing, repeated or unrecognised, so one garbled answer is dropped instead
    of spoiling the whole question.
    """
    if len(set(expected)) != len(expected):
        raise ValueError(f"The question repeats an option name: {list(expected)}")

    remaining = [(str(name), float(probability)) for name, probability in parsed]
    matched: dict[str, float] = {}
    for normalize in (str, _normalize_name, _normalize_name_without_prefix):
        for option in expected:
            if option in matched:
                continue
            target = normalize(option)
            hits = [
                index
                for index, (name, _) in enumerate(remaining)
                if normalize(name) == target
            ]
            if len(hits) > 1:
                raise ValueError(f"Option '{option}' was answered more than once")
            if len(hits) == 1:
                matched[option] = remaining.pop(hits[0])[1]

    missing = [option for option in expected if option not in matched]
    if missing:
        raise ValueError(f"The answer left out these options: {missing}")
    if remaining:
        raise ValueError(
            f"The answer included options the question does not have: "
            f"{[name for name, _ in remaining]}"
        )
    return [matched[option] for option in expected]


# --------------------------------------------------------------------------- #
# Numeric forecasts
# --------------------------------------------------------------------------- #


def clean_percentiles(
    pairs: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Tidies a model's (percentile, value) list: percentiles are fractions strictly
    between 0 and 1, sorted, without repeats. Values must run in one direction;
    a fully reversed list is flipped (the model read percentiles as "chance of
    exceeding"), a jumbled one is rejected.
    """
    by_percentile: dict[float, float] = {}
    for percentile, value in pairs:
        p = float(percentile)
        v = float(value)
        if not (math.isfinite(p) and math.isfinite(v)):
            continue
        if not 0.0 < p < 1.0:
            continue
        key = round(p, 6)
        if key not in by_percentile:
            by_percentile[key] = v

    ordered = sorted(by_percentile.items())
    if len(ordered) < 2:
        raise ValueError(
            "A numeric forecast needs at least two percentiles between 0 and 1"
        )

    values = [value for _, value in ordered]
    rising = all(a <= b for a, b in zip(values, values[1:]))
    falling = all(a >= b for a, b in zip(values, values[1:]))
    if rising:
        return ordered
    if falling:
        return [(p, v) for (p, _), v in zip(ordered, reversed(values))]
    raise ValueError(f"Percentile values are not in a consistent order: {ordered}")


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


def demote_headings(text: str) -> str:
    """
    Turns markdown headings inside a research result into bold lines. The
    framework rebuilds the comment's heading tree, and stray top-level headings
    from a search result would break it.
    """
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        hashes = len(stripped) - len(stripped.lstrip("#"))
        if 1 <= hashes <= 6 and stripped[hashes : hashes + 1] in (" ", "\t"):
            title = stripped[hashes:].strip().strip("*").strip()
            lines.append(f"**{title}**" if title else "")
        else:
            lines.append(line)
    return "\n".join(lines)


def shorten(text: str, limit: int) -> str:
    """Trims very long text in the middle, keeping the start and the end."""
    if limit < 40:
        raise ValueError("Limit is too small to be useful")
    if len(text) <= limit:
        return text
    keep = (limit - 30) // 2
    return f"{text[:keep]}\n\n[... trimmed ...]\n\n{text[-keep:]}"


# --------------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------------- #


def as_utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def should_attempt(
    open_time: datetime | None,
    close_time: datetime | None,
    now: datetime,
    *,
    fresh_hours: float = 3.0,
    retry_every_hours: int = 6,
    last_chance_hours: float = 2.0,
    retry_window_minutes: int = 45,
) -> bool:
    """
    Decides whether to spend money on an open question this run.

    New questions (and ones about to close) are always attempted. A question that
    has sat unforecast for hours has usually failed before, for example because
    every model judged it already decided, so it is retried only in the first
    `retry_window_minutes` of every `retry_every_hours`-th hour (wide enough to
    survive a late-starting scheduled run). Without this, one awkward question
    that stays open for days would be paid for seventy times a day.
    """
    current = as_utc(now)
    opened = as_utc(open_time)
    closes = as_utc(close_time)
    assert current is not None

    if opened is None:
        return True
    if current - opened <= timedelta(hours=fresh_hours):
        return True
    if closes is not None:
        time_left = closes - current
        if timedelta(0) <= time_left <= timedelta(hours=last_chance_hours):
            return True
    return (
        current.hour % retry_every_hours == 0 and current.minute < retry_window_minutes
    )


def urgency_key(close_time: datetime | None) -> float:
    """Sort key that puts the questions closing soonest first."""
    closes = as_utc(close_time)
    return closes.timestamp() if closes is not None else float("inf")


def pick_variety(kinds: Sequence[str], limit: int) -> list[int]:
    """
    Positions of up to `limit` items, taking one of every kind before a second of
    any kind, and keeping the original order within a kind. The smoke test uses
    this so a handful of questions still covers every question type.
    """
    by_kind: dict[str, list[int]] = {}
    for position, kind in enumerate(kinds):
        by_kind.setdefault(kind, []).append(position)
    picked: list[int] = []
    depth = 0
    while len(picked) < limit and any(depth < len(group) for group in by_kind.values()):
        for group in by_kind.values():
            if depth < len(group) and len(picked) < limit:
                picked.append(group[depth])
        depth += 1
    return picked


def chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    if size < 1:
        raise ValueError("Chunk size must be at least 1")
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def format_utc(moment: datetime | None) -> str:
    utc_moment = as_utc(moment)
    if utc_moment is None:
        return "not stated"
    return utc_moment.strftime("%Y-%m-%d %H:%M UTC")


def describe_timing(
    now: datetime, close_time: datetime | None, resolve_time: datetime | None
) -> str:
    """One line for the prompt saying when forecasting closes and when the answer is due."""
    parts = [f"Today is {format_utc(now)}."]
    if close_time is not None:
        parts.append(f"Forecasting on this question closes {format_utc(close_time)}.")
    if resolve_time is not None:
        resolves = as_utc(resolve_time)
        current = as_utc(now)
        assert resolves is not None and current is not None
        days_left = (resolves - current).total_seconds() / 86400
        if days_left >= 0:
            parts.append(
                f"It is scheduled to resolve by {format_utc(resolve_time)}, "
                f"about {days_left:.0f} day(s) from now."
            )
        else:
            parts.append(f"It was scheduled to resolve by {format_utc(resolve_time)}.")
    return " ".join(parts)
