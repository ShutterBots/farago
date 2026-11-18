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


# --------------------------------------------------------------------------- #
# The Claude route: forecast files written by a scheduled Claude session
# --------------------------------------------------------------------------- #

FORECAST_KINDS = ("binary", "multiple_choice", "numeric", "discrete", "date")
CONTINUOUS_KINDS = ("numeric", "discrete", "date")
MIN_REASONING_CHARS = 200
MAX_REASONING_CHARS = 12000
MIN_PERCENTILE_POINTS = 5


@dataclass(frozen=True)
class ForecastFile:
    """One forecast as a Claude session wrote it, after it has passed every check."""

    question_id: int
    post_id: int
    kind: str
    reasoning: str
    probability: float | None = None  # yes/no questions
    options: tuple[tuple[str, float], ...] = ()  # multiple choice, as written
    # numeric, discrete and date questions: (fraction between 0 and 1, value),
    # lowest percentile first. Dates are seconds since 1970 (UTC).
    percentiles: tuple[tuple[float, float], ...] = ()


def forecast_file_name(question_id: int) -> str:
    return f"q{int(question_id)}.json"


def parse_when(text: Any) -> datetime:
    """Reads a date ("2027-03-01") or an ISO date-time as a moment in UTC."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"Expected a date like 2027-03-01, got {text!r}")
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"Not a date I can read: {text!r}") from None
    result = as_utc(moment)
    assert result is not None
    return result


def _real_number(value: Any, what: str) -> float:
    # bool is a kind of int in Python, and "true" is never a sensible number here
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return number


def _identifier(data: Mapping[str, Any], name: str) -> int:
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"'{name}' must be a positive whole number, got {value!r}")
    return value


def parse_forecast_file(data: Any) -> ForecastFile:
    """
    Checks one forecast file and returns it in a fixed shape. Anything that is
    missing, out of range or ambiguous raises ValueError with a plain reason, so
    a malformed forecast is refused instead of being posted.

    The numbers are checked here as written. The guard rails (the 2% to 98% cap,
    the per-option floor, the distribution curve) are applied later, when the
    forecast is matched to the live question.
    """
    if not isinstance(data, Mapping):
        raise ValueError("A forecast file must hold one JSON object")

    question_id = _identifier(data, "question_id")
    post_id = _identifier(data, "post_id")

    kind = data.get("type")
    if kind not in FORECAST_KINDS:
        raise ValueError(f"'type' must be one of {list(FORECAST_KINDS)}, got {kind!r}")

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str):
        raise ValueError("'reasoning' must be text")
    reasoning = reasoning.strip()
    if len(reasoning) < MIN_REASONING_CHARS:
        raise ValueError(
            f"'reasoning' is too short ({len(reasoning)} characters, the minimum is "
            f"{MIN_REASONING_CHARS}): every forecast needs its reasoning"
        )
    if len(reasoning) > MAX_REASONING_CHARS:
        raise ValueError(
            f"'reasoning' is too long ({len(reasoning)} characters, the maximum is "
            f"{MAX_REASONING_CHARS})"
        )

    if kind == "binary":
        probability = _real_number(data.get("probability"), "'probability'")
        if not 0.0 <= probability <= 1.0:
            raise ValueError(
                f"'probability' must be a fraction between 0 and 1, got {probability} "
                "(write 0.37, not 37)"
            )
        return ForecastFile(question_id, post_id, kind, reasoning, probability=probability)

    if kind == "multiple_choice":
        raw = data.get("probabilities")
        if not isinstance(raw, Mapping) or len(raw) < 2:
            raise ValueError("'probabilities' must map at least two option names to numbers")
        options: list[tuple[str, float]] = []
        for name, value in raw.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Option names must be text, got {name!r}")
            number = _real_number(value, f"The probability of '{name}'")
            if number < 0:
                raise ValueError(f"The probability of '{name}' is negative")
            options.append((name, number))
        total = sum(number for _, number in options)
        if not 0.98 <= total <= 1.02:
            raise ValueError(
                f"The option probabilities add up to {total:.4f}; they must add up "
                "to 1 (write 0.25, not 25)"
            )
        return ForecastFile(question_id, post_id, kind, reasoning, options=tuple(options))

    raw = data.get("percentiles")
    if not isinstance(raw, Mapping):
        raise ValueError("'percentiles' must map percentiles such as \"10\" to values")
    points: dict[float, float] = {}
    for key, value in raw.items():
        try:
            percentile = float(key)
        except (TypeError, ValueError):
            raise ValueError(f"Not a percentile: {key!r}") from None
        if not (math.isfinite(percentile) and 0.0 < percentile < 100.0):
            raise ValueError(f"Percentiles must be between 0 and 100, got {key!r}")
        if kind == "date":
            number = parse_when(value).timestamp()
        else:
            number = _real_number(value, f"The value at percentile {key}")
        fraction = round(percentile / 100.0, 6)
        if fraction in points:
            raise ValueError(f"Percentile {key} is given twice")
        points[fraction] = number
    ordered = sorted(points.items())
    if len(ordered) < MIN_PERCENTILE_POINTS:
        raise ValueError(
            f"A {kind} forecast needs at least {MIN_PERCENTILE_POINTS} percentiles, "
            f"got {len(ordered)}"
        )
    if ordered[0][0] > 0.10 + 1e-9 or ordered[-1][0] < 0.90 - 1e-9:
        raise ValueError(
            "The percentiles must reach down to 10 or lower and up to 90 or higher, "
            "so both tails are stated"
        )
    values = [value for _, value in ordered]
    if any(later < earlier for earlier, later in zip(values, values[1:])):
        raise ValueError(
            "Values must rise (or stay level) as the percentile rises: the 10th "
            "percentile is the low end, the 90th the high end"
        )
    return ForecastFile(question_id, post_id, kind, reasoning, percentiles=tuple(ordered))


# The framework refuses a forecast with any value more than twice the width of
# the question's range beyond a bound, and wants neighbouring percentiles at
# least 0.00005 apart. Both limits are kept at a distance here.
FAR_REACH = 1.9
MIN_FRACTION_GAP = 1e-4
# It also wants at least one value inside the range, give or take a quarter of its width.
NEAR_REACH = 0.25


def _fraction_at(low: tuple[float, float], high: tuple[float, float], value: float) -> float:
    """Where a straight line between two (fraction, value) points reaches the value."""
    share = (value - low[1]) / (high[1] - low[1])
    return low[0] + (high[0] - low[0]) * share


def trim_far_points(
    points: Sequence[tuple[float, float]],
    lower: float,
    upper: float,
    open_lower: bool,
    open_upper: bool,
) -> tuple[tuple[float, float], ...]:
    """
    Cuts a forecast off where it runs far past an open bound.

    "Not before 2037" on a question whose range ends in 2027 is a fair thing to
    say, but the framework refuses values that far out. Everything past an open
    bound counts the same, so the far points are replaced by one point on the
    straight line the forecast already draws. Inside the range, and for some
    way beyond it, the forecast is exactly what it was.

    Points are (fraction, value) with the lowest fraction first. Closed bounds
    are left alone: a value far beyond one is a mistake, and is refused later.
    """
    result = list(points)
    span = upper - lower
    if span <= 0 or len(result) < 2:
        return tuple(result)

    if open_upper:
        limit = upper + FAR_REACH * span
        kept = [point for point in result if point[1] <= limit]
        if kept and len(kept) < len(result):
            fraction = _fraction_at(kept[-1], result[len(kept)], limit)
            if fraction - kept[-1][0] >= MIN_FRACTION_GAP:
                kept.append((fraction, limit))
            result = kept

    if open_lower:
        limit = lower - FAR_REACH * span
        kept = [point for point in result if point[1] >= limit]
        if kept and len(kept) < len(result):
            fraction = _fraction_at(result[len(result) - len(kept) - 1], kept[0], limit)
            if kept[0][0] - fraction >= MIN_FRACTION_GAP:
                kept.insert(0, (fraction, limit))
            result = kept

    return tuple(result)


def _card_edges(card: Mapping[str, Any], kind: str) -> tuple[float, float, float] | None:
    """The lowest and highest answer a question's card allows, and the slack a whole-number question gets."""
    lower, upper = card.get("lower_bound"), card.get("upper_bound")
    if lower is None or upper is None:
        return None
    try:
        if kind == "date":
            return parse_when(lower).timestamp(), parse_when(upper).timestamp(), 0.0
        slack = 0.0
        if kind == "discrete":
            # the count k is the stretch from k - step/2 to k + step/2
            slack = float(card.get("step") or 1.0) / 2.0
        return float(lower), float(upper), slack
    except (TypeError, ValueError):
        return None


def format_number(value: float) -> str:
    """A number the way a person writes it: 8,100,000 and -23.5, never 8.1e+06."""
    size = abs(value)
    if size >= 1e15 or (0 < size < 1e-4):
        return f"{value:g}"
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _show_value(value: float, kind: str) -> str:
    if kind == "date":
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    return format_number(value)


def check_against_card(forecast: ForecastFile, card: Mapping[str, Any]) -> str | None:
    """
    What is wrong with a forecast for this particular question, judged from the
    question's card in state/open-questions.json. Returns None when it fits.

    This catches, before anything is pushed, the mistakes the posting step would
    refuse or silently bend: the wrong type, a misnamed option, a value outside a
    closed bound, or numbers in the wrong unit.
    """
    if card.get("type") != forecast.kind:
        return f"the question list says this question is '{card.get('type')}', the file says '{forecast.kind}'"
    if card.get("post_id") != forecast.post_id:
        return f"'post_id' must be {card.get('post_id')} for this question"

    if forecast.kind == "multiple_choice":
        options = card.get("options")
        if isinstance(options, list) and options:
            try:
                align_options(forecast.options, [str(option) for option in options])
            except ValueError as error:
                return f"{error} (the options are {options})"
        return None

    if forecast.kind not in CONTINUOUS_KINDS:
        return None
    edges = _card_edges(card, forecast.kind)
    if edges is None:
        return None
    lower, upper, slack = edges
    span = upper - lower
    if span <= 0:
        return None
    tiny = span * 1e-9

    if not card.get("open_lower_bound", True):
        fraction, value = forecast.percentiles[0]
        if value < lower - slack - tiny:
            return (
                f"the answer cannot be below {_show_value(lower, forecast.kind)}, but the value at "
                f"percentile {fraction * 100:g} is {_show_value(value, forecast.kind)}"
            )
    if not card.get("open_upper_bound", True):
        fraction, value = forecast.percentiles[-1]
        if value > upper + slack + tiny:
            return (
                f"the answer cannot be above {_show_value(upper, forecast.kind)}, but the value at "
                f"percentile {fraction * 100:g} is {_show_value(value, forecast.kind)}"
            )

    reach = NEAR_REACH * span * 0.9
    if not any(lower - reach <= value <= upper + reach for _, value in forecast.percentiles):
        return (
            f"none of the values lies inside the question's range "
            f"({_show_value(lower, forecast.kind)} to {_show_value(upper, forecast.kind)}): check the "
            "unit and the scale, and give at least one percentile inside the range even "
            "when most of the probability is beyond an open bound"
        )
    return None
