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


# --------------------------------------------------------------------------- #
# The Claude route: forecast files
# --------------------------------------------------------------------------- #

REASONING = "The base rate is about one in four, and this week's report moves it up a little. " * 4
SEVEN = {"5": 41.0, "10": 43.5, "25": 47.0, "50": 51.0, "75": 55.5, "90": 60.0, "95": 63.0}


def forecast(kind="binary", **fields):
    data = {"question_id": 12345, "post_id": 67890, "type": kind, "reasoning": REASONING}
    if kind == "binary":
        data["probability"] = 0.27
    data.update(fields)
    return data


def test_forecast_file_yes_no():
    parsed = fm.parse_forecast_file(forecast())
    assert (parsed.question_id, parsed.post_id, parsed.kind) == (12345, 67890, "binary")
    assert parsed.probability == 0.27 and parsed.reasoning == REASONING.strip()
    assert parsed.options == () and parsed.percentiles == ()
    # the ends are allowed here; the cap is applied when the forecast is posted
    assert fm.parse_forecast_file(forecast(probability=1)).probability == 1.0
    assert fm.parse_forecast_file(forecast(probability=0)).probability == 0.0
    # extra fields a session may add are ignored
    assert fm.parse_forecast_file(forecast(title="Will it?", sources=["a"])).probability == 0.27


def test_forecast_file_multiple_choice_keeps_names_as_written():
    parsed = fm.parse_forecast_file(
        forecast("multiple_choice", probabilities={"Option one": 0.55, "Option two": 0.35, "Other": 0.10})
    )
    assert parsed.options == (("Option one", 0.55), ("Option two", 0.35), ("Other", 0.10))


def test_forecast_file_numbers_are_sorted_fractions():
    shuffled = {"90": 60.0, "5": 41.0, "50": 51.0, "10": 43.5, "95": 63.0, "25": 47.0, "75": 55.5}
    parsed = fm.parse_forecast_file(forecast("numeric", percentiles=shuffled))
    assert [p for p, _ in parsed.percentiles] == [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]
    assert [v for _, v in parsed.percentiles] == sorted(SEVEN.values())
    assert fm.parse_forecast_file(forecast("discrete", percentiles=SEVEN)).kind == "discrete"
    # level stretches are fine, for example a count that is very likely zero
    flat = {"5": 0, "10": 0, "25": 0, "50": 0, "75": 1, "90": 2, "95": 3}
    assert fm.parse_forecast_file(forecast("discrete", percentiles=flat)).percentiles[0] == (0.05, 0.0)


def test_forecast_file_dates_become_utc_seconds():
    dates = {
        "5": "2027-01-10", "10": "2027-01-20", "25": "2027-02-15", "50": "2027-03-20",
        "75": "2027-05-01T12:00:00Z", "90": "2027-07-01T00:00:00+02:00", "95": "2027-09-01",
    }
    parsed = fm.parse_forecast_file(forecast("date", percentiles=dates))
    first = datetime(2027, 1, 10, tzinfo=UTC).timestamp()
    assert parsed.percentiles[0] == (0.05, first)
    assert parsed.percentiles[4][1] == datetime(2027, 5, 1, 12, tzinfo=UTC).timestamp()
    assert parsed.percentiles[5][1] == datetime(2027, 6, 30, 22, tzinfo=UTC).timestamp()


def test_parse_when_and_file_name():
    assert fm.parse_when("2027-03-01") == datetime(2027, 3, 1, tzinfo=UTC)
    assert fm.parse_when(" 2027-03-01T06:30:00Z ") == datetime(2027, 3, 1, 6, 30, tzinfo=UTC)
    for bad in ("March 1st", "", None, 20270301):
        with pytest.raises(ValueError):
            fm.parse_when(bad)
    assert fm.forecast_file_name(12345) == "q12345.json"


@pytest.mark.parametrize(
    "data, reason",
    [
        ([forecast()], "one JSON object"),
        (forecast(question_id="12345"), "'question_id'"),
        (forecast(question_id=True), "'question_id'"),
        (forecast(post_id=0), "'post_id'"),
        (forecast(post_id=1.5), "'post_id'"),
        (forecast(type="conditional"), "'type'"),
        (forecast(reasoning="Seems likely."), "too short"),
        (forecast(reasoning="x" * 12001), "too long"),
        (forecast(reasoning=None), "must be text"),
        (forecast(probability=27), "write 0.37, not 37"),
        (forecast(probability=-0.1), "between 0 and 1"),
        (forecast(probability="0.27"), "must be a number"),
        (forecast(probability=True), "must be a number"),
        (forecast(probability=float("nan")), "finite"),
        ({k: v for k, v in forecast().items() if k != "probability"}, "must be a number"),
        (forecast("multiple_choice", probabilities={"A": 60, "B": 40}), "add up to 1"),
        (forecast("multiple_choice", probabilities={"A": 0.6, "B": 0.3}), "add up to 1"),
        (forecast("multiple_choice", probabilities={"A": 1.2, "B": -0.2}), "negative"),
        (forecast("multiple_choice", probabilities={"A": 1.0}), "at least two"),
        (forecast("multiple_choice", probabilities=[0.5, 0.5]), "at least two"),
        (forecast("numeric"), "'percentiles'"),
        (forecast("numeric", percentiles={"10": 1, "50": 2, "90": 3}), "at least 5"),
        (forecast("numeric", percentiles={"20": 1, "30": 2, "50": 3, "70": 4, "90": 5}), "both tails"),
        (forecast("numeric", percentiles={"10": 1, "30": 2, "50": 3, "70": 4, "80": 5}), "both tails"),
        (forecast("numeric", percentiles={**SEVEN, "50": 40.0}), "Values must rise"),
        (forecast("numeric", percentiles={**SEVEN, "10.0": 43.5}), "given twice"),
        (forecast("numeric", percentiles={**SEVEN, "100": 70.0}), "between 0 and 100"),
        (forecast("numeric", percentiles={**SEVEN, "median": 51.0}), "Not a percentile"),
        (forecast("numeric", percentiles={**SEVEN, "50": "51"}), "must be a number"),
        (forecast("date", percentiles=SEVEN), "date"),
        (forecast("date", percentiles={**{k: "2027-01-01" for k in SEVEN}, "95": "soon"}), "Not a date"),
    ],
)
def test_forecast_files_that_are_refused(data, reason):
    with pytest.raises(ValueError) as caught:
        fm.parse_forecast_file(data)
    assert reason in str(caught.value), str(caught.value)


# --------------------------------------------------------------------------- #
# The Claude route: forecasts that run far past an open bound
# --------------------------------------------------------------------------- #


def height_at(points, value):
    """The height of the straight-line curve through (fraction, value) points at a value."""
    for (f0, v0), (f1, v1) in zip(points, points[1:]):
        if v0 <= value <= v1:
            return f0 if v1 == v0 else f0 + (f1 - f0) * (value - v0) / (v1 - v0)
    raise AssertionError(f"{value} is outside the curve")


def test_trim_far_points_leaves_ordinary_forecasts_alone():
    points = ((0.05, 10.0), (0.25, 30.0), (0.5, 50.0), (0.75, 70.0), (0.95, 250.0))
    assert fm.trim_far_points(points, 0.0, 100.0, True, True) == points
    # exactly on the limit is not beyond it
    on_limit = points[:-1] + ((0.95, 100.0 + fm.FAR_REACH * 100.0),)
    assert fm.trim_far_points(on_limit, 0.0, 100.0, True, True) == on_limit


def test_trim_far_points_cuts_the_far_end_without_moving_the_curve():
    points = ((0.02, 80.0), (0.05, 95.0), (0.10, 140.0), (0.5, 900.0), (0.9, 4000.0), (0.95, 9000.0))
    trimmed = fm.trim_far_points(points, 0.0, 100.0, False, True)
    limit = 100.0 + fm.FAR_REACH * 100.0
    assert trimmed[:3] == points[:3] and len(trimmed) == 4
    assert trimmed[-1][1] == limit and 0.10 < trimmed[-1][0] < 0.5
    assert max(value for _, value in trimmed) <= 100.0 + 2 * 100.0  # what the framework allows
    # the chance of landing inside the range is exactly what it was
    for value in (85.0, 100.0, 120.0, 200.0, limit):
        assert height_at(trimmed, value) == pytest.approx(height_at(points, value))


def test_trim_far_points_works_on_the_low_side_too():
    points = ((0.05, -9000.0), (0.1, -4000.0), (0.5, -900.0), (0.9, -140.0), (0.95, -95.0), (0.98, -80.0))
    trimmed = fm.trim_far_points(points, -100.0, 0.0, True, False)
    limit = -100.0 - fm.FAR_REACH * 100.0
    assert trimmed[1:] == points[3:] and trimmed[0][1] == limit and 0.5 < trimmed[0][0] < 0.9
    for value in (limit, -200.0, -100.0, -90.0):
        assert height_at(trimmed, value) == pytest.approx(height_at(points, value))


def test_trim_far_points_respects_closed_bounds_and_hopeless_cases():
    far = ((0.05, 10.0), (0.5, 50.0), (0.95, 5000.0))
    # a closed bound: a value far beyond it is a mistake, and is left for the refusal
    assert fm.trim_far_points(far, 0.0, 100.0, False, False) == far
    # everything beyond: nothing sensible can be kept, so nothing is changed
    lost = ((0.05, 4000.0), (0.5, 5000.0), (0.95, 6000.0))
    assert fm.trim_far_points(lost, 0.0, 100.0, True, True) == lost
    # a degenerate range
    assert fm.trim_far_points(far, 5.0, 5.0, True, True) == far


def test_trim_far_points_never_leaves_two_points_too_close_together():
    # the last point kept sits a hair before the limit: no extra point is added on top of it
    limit = 100.0 + fm.FAR_REACH * 100.0
    points = ((0.05, 50.0), (0.5, limit - 1e-9), (0.95, 9000.0))
    assert fm.trim_far_points(points, 0.0, 100.0, True, True) == points[:2]


# --------------------------------------------------------------------------- #
# The Claude route: a forecast held against its question card
# --------------------------------------------------------------------------- #

NUMBER_CARD = {
    "question_id": 12345, "post_id": 67890, "type": "numeric", "unit": "%",
    "lower_bound": 30.0, "upper_bound": 70.0, "open_lower_bound": False, "open_upper_bound": True,
}
COUNT_CARD = {
    "question_id": 12345, "post_id": 67890, "type": "discrete", "unit": "officials",
    "lower_bound": 0.0, "upper_bound": 15.0, "open_lower_bound": False, "open_upper_bound": False, "step": 1.0,
}
DATE_CARD = {
    "question_id": 12345, "post_id": 67890, "type": "date",
    "lower_bound": "2022-05-29T00:00:00Z", "upper_bound": "2027-05-28T00:00:00Z",
    "open_lower_bound": False, "open_upper_bound": True,
}
CHOICE_CARD = {
    "question_id": 12345, "post_id": 67890, "type": "multiple_choice",
    "options": ["Democratic Party", "Republican Party", "Other"],
}


def verdict(card, kind=None, **fields):
    return fm.check_against_card(fm.parse_forecast_file(forecast(kind or card["type"], **fields)), card)


def test_card_check_passes_forecasts_that_fit():
    assert verdict(NUMBER_CARD, percentiles=SEVEN) is None
    assert verdict(NUMBER_CARD, percentiles={**SEVEN, "95": 400.0}) is None  # past the open top
    assert verdict({**NUMBER_CARD, "type": "binary"}, probability=0.4) is None
    assert verdict(CHOICE_CARD, probabilities={"Democratic Party": 0.6, "Republican Party": 0.38, "Other": 0.02}) is None
    # a count may sit half a step outside the first and last whole number
    counts = {"0.5": -0.5, "5": 3.6, "25": 4.6, "50": 5.3, "75": 6.1, "95": 7.4, "99.5": 15.5}
    assert verdict(COUNT_CARD, percentiles=counts) is None
    later = {"2": "2027-02-15", "5": "2027-05-12", "25": "2028-03-01", "50": "2029-03-01", "75": "2031-01-01", "95": "2037-01-01"}
    assert verdict(DATE_CARD, percentiles=later) is None
    # a card with no bounds on it cannot be checked, and is not blamed
    assert verdict({"question_id": 12345, "post_id": 67890, "type": "numeric"}, percentiles=SEVEN) is None


@pytest.mark.parametrize(
    "card, fields, reason",
    [
        (NUMBER_CARD, {"kind": "discrete", "percentiles": SEVEN}, "says this question is 'numeric'"),
        ({**NUMBER_CARD, "post_id": 111}, {"percentiles": SEVEN}, "'post_id' must be 111"),
        (NUMBER_CARD, {"percentiles": {**SEVEN, "5": 29.0}}, "cannot be below 30"),
        ({**NUMBER_CARD, "open_upper_bound": False}, {"percentiles": {**SEVEN, "95": 71.0}}, "cannot be above 70"),
        (COUNT_CARD, {"percentiles": {**SEVEN, "5": -0.6}}, "cannot be below 0"),
        (COUNT_CARD, {"percentiles": {"5": 1, "25": 2, "50": 3, "75": 5, "95": 15.6}}, "cannot be above 15"),
        (NUMBER_CARD, {"percentiles": {k: v * 1000 for k, v in SEVEN.items()}}, "check the unit"),
        (
            DATE_CARD,
            {"percentiles": {"5": "2029-01-01", "25": "2030-01-01", "50": "2031-01-01", "75": "2033-01-01", "95": "2037-01-01"}},
            "at least one percentile inside the range",
        ),
        (
            DATE_CARD,
            {"percentiles": {"5": "2021-01-01", "25": "2026-12-01", "50": "2027-01-01", "75": "2027-02-01", "95": "2027-03-01"}},
            "cannot be below 2022-05-29",
        ),
        (CHOICE_CARD, {"probabilities": {"Democrats": 0.6, "Republicans": 0.38, "Other": 0.02}}, "the options are"),
        (CHOICE_CARD, {"probabilities": {"Democratic Party": 0.6, "Republican Party": 0.4}}, "the options are"),
    ],
)
def test_card_check_names_what_does_not_fit(card, fields, reason):
    fields = dict(fields)
    problem = verdict(card, fields.pop("kind", None), **fields)
    assert problem is not None and reason in problem, problem


@pytest.mark.parametrize(
    "value, shown",
    [
        (7500000, "7,500,000"), (8100000.0, "8,100,000"), (-23.5, "-23.5"), (3.53, "3.53"), (0, "0"),
        (-0.0, "0"), (0.25, "0.25"), (1234.5678, "1,234.5678"), (15.0, "15"), (0.00001, "1e-05"),
        (-0.00004, "-4e-05"), (2.5e17, "2.5e+17"), (0.1 + 0.2, "0.3"),
    ],
)
def test_format_number_reads_like_a_person_wrote_it(value, shown):
    assert fm.format_number(value) == shown


# --------------------------------------------------------------------------- #
# Tournaments where a forecast is kept up to date (Market Pulse)
# --------------------------------------------------------------------------- #

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def hours(count):
    return timedelta(hours=count)


def test_scoring_moment_prefers_what_metaculus_states():
    spot, reveal, close = T0 + hours(10), T0 + hours(20), T0 + hours(30)
    assert fm.scoring_moment(spot, reveal, close) == spot
    # nothing stated: the earlier of the reveal and the close
    assert fm.scoring_moment(None, reveal, close) == reveal
    assert fm.scoring_moment(None, close, reveal) == reveal
    assert fm.scoring_moment(None, None, close) == close
    assert fm.scoring_moment(None, reveal, None) == reveal
    assert fm.scoring_moment(None, None, None) is None
    # never later than the close, and naive datetimes are read as UTC
    assert fm.scoring_moment(close + hours(5), None, close) == close
    assert fm.scoring_moment(datetime(2026, 11, 20, 22, 0), None, None) == T0 + hours(10)


def test_update_reason_first_forecast_and_deadlines_that_have_gone():
    assert fm.update_reason(T0, T0 + hours(500), None) == "new"
    assert fm.update_reason(T0, T0 + hours(1), None) == "new"
    assert fm.update_reason(T0, None, None) == "new"
    # too late to research and post, or already past
    assert fm.update_reason(T0, T0 + timedelta(minutes=9), None) is None
    assert fm.update_reason(T0, T0 - hours(1), None) is None
    assert fm.update_reason(T0, T0 - hours(1), T0 - hours(300)) is None


def test_update_reason_leaves_a_recent_forecast_alone():
    deadline = T0 + hours(200)
    assert fm.update_reason(T0, deadline, T0 - hours(1)) is None
    assert fm.update_reason(T0, deadline, T0 - hours(167)) is None
    assert fm.update_reason(T0, deadline, T0 - hours(168)) == "weekly refresh"
    assert fm.update_reason(T0, None, T0 - hours(169)) == "weekly refresh"
    # a clock that runs a little ahead does not confuse it
    assert fm.update_reason(T0, deadline, T0 + timedelta(minutes=3)) is None


def test_update_reason_one_forecast_in_the_last_day_and_one_in_the_last_hours():
    deadline = T0 + hours(20)
    # last forecast two days ago: the final-day forecast is due
    assert fm.update_reason(T0, deadline, T0 - hours(48)) == "final day"
    # one was already made inside the last day: nothing until the last hours
    assert fm.update_reason(T0, deadline, T0 - hours(2)) is None
    assert fm.update_reason(T0, deadline, deadline - hours(25)) is None
    # just outside the window, but made less than six hours ago: wait
    near = T0 + hours(25)
    assert fm.update_reason(T0, near, T0 - hours(3)) is None
    assert fm.update_reason(T0 + hours(4), near, T0 - hours(3)) == "final day"

    soon = T0 + hours(2)
    assert fm.update_reason(T0, soon, T0 - hours(10)) == "last call"
    assert fm.update_reason(T0, soon, T0 - hours(0.5)) is None  # already made inside the last hours
    # made 90 minutes ago, just before the window opened: wait until it is two hours old
    edge = T0 + hours(2.9)
    assert fm.update_reason(T0, edge, T0 - hours(1.5)) is None
    assert fm.update_reason(T0 + hours(0.6), edge, T0 - hours(1.5)) == "last call"
    # the last call outranks the final day
    assert fm.update_reason(T0, soon, T0 - hours(100)) == "last call"


def test_update_schedule_over_a_question_s_whole_life():
    """Hourly runs from six weeks out: how many forecasts, and when."""
    deadline = T0 + timedelta(days=42)
    last, made = None, []
    moment = T0
    while moment < deadline:
        reason = fm.update_reason(moment, deadline, last)
        if reason:
            made.append((reason, round((deadline - moment).total_seconds() / 3600)))
            last = moment
        moment += hours(1)
    reasons = [reason for reason, _ in made]
    assert reasons[0] == "new" and reasons[-1] == "last call" and reasons[-2] == "final day"
    assert reasons.count("weekly refresh") == 5 and len(made) == 8, made
    assert made[-1][1] <= 3 and 3 < made[-2][1] <= 26, made


def test_urgent_and_patient_work():
    assert fm.is_urgent(T0, T0 + hours(500), "last call")
    assert fm.is_urgent(T0, T0 + hours(20), "final day")
    assert fm.is_urgent(T0, T0 + hours(72), "new")
    assert not fm.is_urgent(T0, T0 + hours(73), "new")
    assert not fm.is_urgent(T0, None, "new")
    assert not fm.is_urgent(T0, T0 + hours(10), "weekly refresh")


def test_pick_update_work_takes_urgent_first_and_trickles_the_rest():
    candidates = [
        (T0 + hours(900), False),  # 0 patient, late
        (T0 + hours(5), True),  # 1 urgent
        (T0 + hours(100), False),  # 2 patient, soonest of the patient ones
        (T0 + hours(2), True),  # 3 urgent, soonest
        (None, False),  # 4 patient, no deadline: last
        (T0 + hours(300), False),  # 5 patient
    ]
    assert fm.pick_update_work(candidates) == [3, 1, 2, 5, 0, 4]
    assert fm.pick_update_work(candidates, max_urgent=1, max_patient=2) == [3, 2, 5]
    assert fm.pick_update_work([]) == []
    crowd = [(T0 + hours(n), True) for n in range(30)] + [(T0 + hours(200 + n), False) for n in range(30)]
    picked = fm.pick_update_work(crowd)
    assert picked == list(range(12)) + list(range(30, 34))


def test_is_updated_tournament():
    assert fm.is_updated_tournament(["market-pulse-26q4"])
    assert fm.is_updated_tournament(["something-else", "market-pulse-26q4"])
    assert not fm.is_updated_tournament(["fall-futureeval-2026", "minibench"])
    assert not fm.is_updated_tournament([]) and not fm.is_updated_tournament(None)
