import math
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forecast_math as fm  # noqa: E402

UTC = timezone.utc


# ------------------------------------------------------------------ settings


def test_defaults_are_standard_main_and_lite_minibench():
    settings = fm.load_settings({})
    assert settings.main.name == "standard"
    assert settings.main.forecasters == ("opus", "opus", "opus", "argon")
    assert settings.minibench.forecasters == ("opus", "opus", "opus")
    assert settings.backup_models == ("opus", "argon")
    assert settings.main.research == ("news", "background")
    assert settings.minibench is not None
    assert settings.minibench.name == "lite"
    assert settings.prob_floor == 0.02
    assert settings.option_floor == 0.01
    assert settings.dry_run is False


def test_empty_strings_count_as_unset():
    settings = fm.load_settings(
        {"BOT_TIER": "", "BOT_MINIBENCH_TIER": "  ", "BOT_PROB_FLOOR": ""}
    )
    assert settings.main.name == "standard"
    assert settings.minibench.name == "lite"
    assert settings.prob_floor == 0.02


def test_minibench_can_be_switched_off():
    assert fm.load_settings({"BOT_MINIBENCH_TIER": "off"}).minibench is None


def test_custom_forecasters_and_research():
    settings = fm.load_settings(
        {
            "BOT_TIER": "lite",
            "BOT_FORECASTERS": "opus, openrouter/x-ai/grok-4.7 ,argon",
            "BOT_RESEARCH": "news,signals",
        }
    )
    assert settings.main.forecasters == ("opus", "openrouter/x-ai/grok-4.7", "argon")
    assert settings.main.research == ("news", "signals")


@pytest.mark.parametrize(
    "env",
    [
        {"BOT_TIER": "turbo"},
        {"BOT_FORECASTERS": "not-a-model"},
        {"BOT_FORECASTERS": " , "},
        {"BOT_RESEARCH": "gossip"},
        {"BOT_PROB_FLOOR": "0.9"},
        {"BOT_PROB_FLOOR": "abc"},
        {"BOT_MAX_QUESTIONS_PER_RUN": "0"},
        {"BOT_MAX_QUESTIONS_PER_RUN": "2.5"},
        {"BOT_SEARCH_MODEL": "nope"},
    ],
)
def test_bad_settings_raise_readable_errors(env):
    with pytest.raises(ValueError):
        fm.load_settings(env)


def test_every_tier_uses_known_models_and_sources():
    for tier in fm.TIERS.values():
        assert tier["forecasters"]
        for name in tier["forecasters"]:
            assert fm.resolve_model_spec(name)["model"].startswith("openrouter/")
        for source in tier["research"]:
            assert source in fm.RESEARCH_SOURCES


def test_resolve_model_spec_returns_a_copy():
    spec = fm.resolve_model_spec("opus")
    spec["model"] = "changed"
    assert fm.MODEL_PRESETS["opus"]["model"] != "changed"


def test_raw_model_id_is_accepted():
    spec = fm.resolve_model_spec("openrouter/x-ai/grok-4.7")
    assert spec == {
        "model": "openrouter/x-ai/grok-4.7",
        "temperature": None,
        "timeout": 300,
    }


def test_fallback_order_rotates_and_dedupes():
    seats = ("opus", "opus", "argon")
    assert fm.fallback_order(seats, 0) == ["opus", "argon"]
    assert fm.fallback_order(seats, 1) == ["opus", "argon"]
    assert fm.fallback_order(seats, 2) == ["argon", "opus"]
    assert fm.fallback_order(seats, 5) == ["argon", "opus"]
    # backups come last and never repeat a model that is already on the panel
    assert fm.fallback_order(("opus", "opus"), 1, ("opus", "argon")) == ["opus", "argon"]
    assert fm.fallback_order(("sol",), 0, ("opus", "argon")) == ["sol", "opus", "argon"]
    # the plain model is the first stand-in for its high-reasoning twin
    max_seats = fm.TIERS["max"]["forecasters"]
    assert fm.fallback_order(max_seats, 0)[:2] == ["opus-high", "opus"]
    with pytest.raises(ValueError):
        fm.fallback_order((), 0)


# ------------------------------------------------------------- probabilities


def test_clamp_probability():
    assert fm.clamp_probability(0.5, 0.02) == 0.5
    assert fm.clamp_probability(0.0, 0.02) == 0.02
    assert fm.clamp_probability(0.999, 0.02) == 0.98
    with pytest.raises(ValueError):
        fm.clamp_probability(float("nan"), 0.02)
    with pytest.raises(ValueError):
        fm.clamp_probability(0.5, 0.6)


def test_floor_and_normalize_basic():
    result = fm.floor_and_normalize([0.97, 0.03, 0.0], 0.01)
    assert math.isclose(sum(result), 1.0, abs_tol=1e-12)
    assert min(result) >= 0.01 - 1e-12
    assert result[2] == pytest.approx(0.01)
    # the two unpinned options keep their 97:3 proportion
    assert result[0] / result[1] == pytest.approx(0.97 / 0.03)


def test_floor_and_normalize_leaves_good_forecasts_alone():
    result = fm.floor_and_normalize([0.5, 0.3, 0.2], 0.01)
    assert result == pytest.approx([0.5, 0.3, 0.2])


def test_floor_and_normalize_rescales_percent_style_input():
    result = fm.floor_and_normalize([50, 30, 20], 0.01)
    assert result == pytest.approx([0.5, 0.3, 0.2])


def test_floor_and_normalize_handles_all_zero_and_bad_values():
    assert fm.floor_and_normalize([0, 0, 0, 0], 0.01) == pytest.approx([0.25] * 4)
    result = fm.floor_and_normalize([float("nan"), -0.2, 1.0], 0.05)
    assert result == pytest.approx([0.05, 0.05, 0.90])


def test_floor_and_normalize_cascading_pins():
    # After the first rescale another option falls under the floor.
    result = fm.floor_and_normalize([0.90, 0.052, 0.048, 0.0], 0.05)
    assert math.isclose(sum(result), 1.0, abs_tol=1e-12)
    assert all(value >= 0.05 - 1e-12 for value in result)
    assert result[0] == max(result)


def test_floor_and_normalize_many_random_cases():
    import random

    rng = random.Random(7)
    for _ in range(2000):
        count = rng.randint(2, 12)
        raw = [rng.random() ** rng.choice([1, 3, 8]) for _ in range(count)]
        floor = fm.effective_option_floor(count, rng.choice([0.01, 0.03, 0.08]))
        result = fm.floor_and_normalize(raw, floor)
        assert math.isclose(sum(result), 1.0, abs_tol=1e-9)
        assert all(floor - 1e-9 <= value <= 1.0 for value in result)
        # order of options by size is preserved
        ranked_raw = sorted(range(count), key=lambda i: raw[i])
        for a, b in zip(ranked_raw, ranked_raw[1:]):
            assert result[a] <= result[b] + 1e-9


def test_floor_too_high_raises():
    with pytest.raises(ValueError):
        fm.floor_and_normalize([0.5, 0.5], 0.5)


def test_effective_option_floor():
    assert fm.effective_option_floor(4, 0.01) == 0.01
    assert fm.effective_option_floor(100, 0.01) == 0.005
    with pytest.raises(ValueError):
        fm.effective_option_floor(0, 0.01)


def test_align_options_exact_and_reordered():
    expected = ["Yes, before June", "Yes, after June", "No"]
    parsed = [("No", 0.6), ("Yes, after June", 0.3), ("Yes, before June", 0.1)]
    assert fm.align_options(parsed, expected) == [0.1, 0.3, 0.6]


def test_align_options_forgives_case_spacing_quotes_and_prefix():
    expected = ["0 or 1", "2 or 3", "4+"]
    parsed = [("Option 0 or 1", 0.2), ('"2  OR 3"', 0.5), ("**4+**", 0.3)]
    assert fm.align_options(parsed, expected) == [0.2, 0.5, 0.3]


@pytest.mark.parametrize(
    "parsed",
    [
        [("A", 0.5), ("B", 0.5)],  # missing C
        [("A", 0.4), ("B", 0.3), ("C", 0.2), ("D", 0.1)],  # extra option
        [("A", 0.4), ("A", 0.3), ("B", 0.2), ("C", 0.1)],  # repeated
        [("A", 0.4), ("B", 0.3), ("Z", 0.3)],  # wrong name
    ],
)
def test_align_options_rejects_garbled_answers(parsed):
    with pytest.raises(ValueError):
        fm.align_options(parsed, ["A", "B", "C"])


# ------------------------------------------------------------------- numeric


def test_clean_percentiles_sorts_and_dedupes():
    pairs = [(0.9, 50), (0.1, 10), (0.5, 30), (0.5, 999), (0.1000000001, 77)]
    assert fm.clean_percentiles(pairs) == [(0.1, 10.0), (0.5, 30.0), (0.9, 50.0)]


def test_clean_percentiles_drops_edges_and_bad_numbers():
    pairs = [(0.0, 1), (1.0, 99), (0.2, 5), (0.8, 9), (float("nan"), 3), (0.5, float("inf"))]
    assert fm.clean_percentiles(pairs) == [(0.2, 5.0), (0.8, 9.0)]


def test_clean_percentiles_flips_a_fully_reversed_list():
    pairs = [(0.1, 500), (0.5, 300), (0.9, 100)]
    assert fm.clean_percentiles(pairs) == [(0.1, 100.0), (0.5, 300.0), (0.9, 500.0)]


def test_clean_percentiles_keeps_ties():
    pairs = [(0.1, 0), (0.2, 0), (0.6, 4), (0.9, 4)]
    assert fm.clean_percentiles(pairs) == [(0.1, 0.0), (0.2, 0.0), (0.6, 4.0), (0.9, 4.0)]


def test_clean_percentiles_rejects_jumbles_and_too_few():
    with pytest.raises(ValueError):
        fm.clean_percentiles([(0.1, 5), (0.5, 50), (0.9, 20)])
    with pytest.raises(ValueError):
        fm.clean_percentiles([(0.5, 5)])
    with pytest.raises(ValueError):
        fm.clean_percentiles([(10, 5), (90, 50)])  # percents, not fractions


# ---------------------------------------------------------------------- text


def test_demote_headings():
    text = "# Big title\nplain line\n  ### **Nested**\n#hashtag stays\n####### seven stays\n#\n## "
    assert fm.demote_headings(text).split("\n") == [
        "**Big title**",
        "plain line",
        "**Nested**",
        "#hashtag stays",
        "####### seven stays",
        "#",
        "",
    ]


def test_demote_headings_leaves_no_heading_lines():
    text = "## A\ntext\n# B\n- item\n### C"
    cleaned = fm.demote_headings(text)
    assert not any(line.lstrip().startswith("# ") for line in cleaned.split("\n"))
    assert not any(line.lstrip().startswith("## ") for line in cleaned.split("\n"))


def test_shorten():
    assert fm.shorten("short", 100) == "short"
    long_text = "a" * 500 + "b" * 500
    cut = fm.shorten(long_text, 200)
    assert len(cut) <= 200
    assert cut.startswith("a") and cut.endswith("b") and "trimmed" in cut
    with pytest.raises(ValueError):
        fm.shorten("x", 10)


# ---------------------------------------------------------------- scheduling


def test_should_attempt_new_questions_always():
    now = datetime(2026, 11, 17, 13, 47, tzinfo=UTC)
    assert fm.should_attempt(now - timedelta(minutes=30), now + timedelta(hours=1), now)
    assert fm.should_attempt(None, None, now)


def test_should_attempt_stale_questions_only_in_retry_window():
    opened = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    closes = datetime(2026, 11, 30, 0, 0, tzinfo=UTC)
    assert not fm.should_attempt(opened, closes, datetime(2026, 11, 17, 13, 7, tzinfo=UTC))
    assert not fm.should_attempt(opened, closes, datetime(2026, 11, 17, 12, 47, tzinfo=UTC))
    assert fm.should_attempt(opened, closes, datetime(2026, 11, 17, 12, 7, tzinfo=UTC))
    assert fm.should_attempt(opened, closes, datetime(2026, 11, 17, 12, 27, tzinfo=UTC))
    assert fm.should_attempt(opened, closes, datetime(2026, 11, 17, 18, 44, tzinfo=UTC))
    # at most a handful of paid retries a day for a question that keeps failing
    day = [datetime(2026, 11, 17, h, m, tzinfo=UTC) for h in range(24) for m in (7, 27, 47)]
    assert sum(fm.should_attempt(opened, closes, moment) for moment in day) == 8


def test_should_attempt_last_chance_before_close():
    opened = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)
    now = datetime(2026, 11, 17, 13, 47, tzinfo=UTC)
    assert fm.should_attempt(opened, now + timedelta(minutes=90), now)
    assert not fm.should_attempt(opened, now + timedelta(hours=5), now)
    assert not fm.should_attempt(opened, now - timedelta(minutes=5), now)


def test_should_attempt_accepts_naive_datetimes():
    now = datetime(2026, 11, 17, 13, 47)
    assert fm.should_attempt(datetime(2026, 11, 17, 13, 0), None, now)


def test_urgency_key_orders_soonest_first_and_unknown_last():
    soon = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)
    later = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
    ordered = sorted([None, later, soon], key=fm.urgency_key)
    assert ordered == [soon, later, None]


def test_chunks():
    assert fm.chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert fm.chunks([], 3) == []
    with pytest.raises(ValueError):
        fm.chunks([1], 0)


def test_describe_timing():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    text = fm.describe_timing(
        now,
        datetime(2026, 11, 17, 13, 30, tzinfo=UTC),
        datetime(2026, 12, 1, 12, 0, tzinfo=UTC),
    )
    assert "Today is 2026-11-17 12:00 UTC." in text
    assert "closes 2026-11-17 13:30 UTC" in text
    assert "about 14 day(s) from now" in text
    assert fm.describe_timing(now, None, None) == "Today is 2026-11-17 12:00 UTC."
    past = fm.describe_timing(now, None, datetime(2026, 11, 1, tzinfo=UTC))
    assert "was scheduled to resolve" in past


# --------------------------------------------------------------------------- #
# Backups, the settings file, and the run-log summary
# --------------------------------------------------------------------------- #


def test_backup_models_can_be_changed_or_switched_off():
    assert fm.load_settings({"BOT_BACKUP_MODELS": "none"}).backup_models == ()
    assert fm.load_settings({"BOT_BACKUP_MODELS": "sol, haiku"}).backup_models == (
        "sol",
        "haiku",
    )
    with pytest.raises(ValueError):
        fm.load_settings({"BOT_BACKUP_MODELS": "not-a-model"})


def test_parse_settings_text_reads_names_values_and_comments():
    text = """\ufeff# The bot's dials
    BOT_TIER = lite      # cheaper

    bot_minibench_tier=off
    #BOT_MAX_USD_PER_RUN=3
    BOT_FORECASTERS="opus, openrouter/x-ai/grok-4.7"
    BOT_DRY_RUN=
    """
    assert fm.parse_settings_text(text) == {
        "BOT_TIER": "lite",
        "BOT_MINIBENCH_TIER": "off",
        "BOT_FORECASTERS": "opus, openrouter/x-ai/grok-4.7",
        "BOT_DRY_RUN": "",
    }
    assert fm.parse_settings_text("") == {}


@pytest.mark.parametrize(
    "line",
    [
        "OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef",
        "METACULUS_TOKEN=0123456789abcdef0123456789abcdef01234567",
        "sk-or-v1-0123456789abcdef",
        "BOT_TEIR=lite",
        "just some words",
    ],
)
def test_parse_settings_text_refuses_anything_else_without_repeating_it(line):
    with pytest.raises(ValueError) as caught:
        fm.parse_settings_text("# dials\nBOT_TIER=lite\n" + line + "\n")
    message = str(caught.value)
    assert "line 3" in message
    assert "0123456789abcdef" not in message
    assert "TEIR" not in message and "some words" not in message


def test_merge_settings_lets_the_environment_win_and_ignores_blanks():
    merged = fm.merge_settings(
        {"BOT_TIER": "lite", "BOT_MAX_USD_PER_RUN": "3", "BOT_DRY_RUN": ""},
        {
            "BOT_TIER": "max",
            "BOT_MAX_USD_PER_RUN": "  ",
            "BOT_PROB_FLOOR": "0.03",
            "OPENROUTER_API_KEY": "never copied",
            "PATH": "/usr/bin",
        },
    )
    assert merged == {
        "BOT_TIER": "max",
        "BOT_MAX_USD_PER_RUN": "3",
        "BOT_PROB_FLOOR": "0.03",
    }
    settings = fm.load_settings(merged)
    assert settings.main.name == "max" and settings.max_usd_per_run == 3


def test_every_setting_the_loader_reads_can_be_put_in_the_file():
    import inspect
    import re

    read_by_loader = set(re.findall(r'"(BOT_[A-Z_]+)"', inspect.getsource(fm.load_settings)))
    assert read_by_loader == set(fm.SETTING_NAMES)


def test_describe_settings_names_the_panels_and_caps():
    text = fm.describe_settings(fm.load_settings({}))
    assert "Seasonal tournament: tier 'standard', panel opus, opus, opus, argon" in text
    assert "MiniBench: tier 'lite', panel opus, opus, opus, research news" in text
    assert "cost cap $6.00" in text and "at most 20 questions" in text
    off = fm.describe_settings(
        fm.load_settings(
            {"BOT_MINIBENCH_TIER": "off", "BOT_MAX_USD_PER_RUN": "0", "BOT_BACKUP_MODELS": "off"}
        )
    )
    assert "MiniBench: off" in off and "cost cap none" in off
    assert "Backups if the whole panel fails: none" in off


def test_pick_variety_takes_one_of_each_kind_first():
    kinds = ["binary", "binary", "numeric", "binary", "date", "numeric", "choice"]
    assert fm.pick_variety(kinds, 4) == [0, 2, 4, 6]
    assert fm.pick_variety(kinds, 6) == [0, 2, 4, 6, 1, 5]
    assert fm.pick_variety(kinds, 99) == [0, 2, 4, 6, 1, 5, 3]
    assert fm.pick_variety(kinds, 2) == [0, 2]
    assert fm.pick_variety(kinds, 0) == []
    assert fm.pick_variety([], 5) == []
