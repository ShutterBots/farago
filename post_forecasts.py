"""
Step three of the Claude route (see FORECASTING.md).

Reads the forecast files a Claude session left in forecasts/pending, checks
each one against the live question, applies the guard rails and posts the
forecast with its reasoning to Metaculus. Posted files move to
forecasts/posted; refused ones move to forecasts/failed with the reason.

This runs inside GitHub Actions, the only place the Metaculus token lives. It
calls no AI model: every number it posts was written by the Claude session and
is only checked, capped and reshaped here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forecasting_tools import (
    MetaculusClient,
    MetaculusQuestion,
    NumericDistribution,
    Percentile,
)

import forecast_math as fm

logger = logging.getLogger(__name__)

PENDING = Path("forecasts/pending")
POSTED = Path("forecasts/posted")
FAILED = Path("forecasts/failed")
SETTINGS_FILE = Path(__file__).with_name("bot_settings.txt")
TESTING_AREA = "bot-testing-area"
# A forecast that keeps failing for outside reasons (network, Metaculus down)
# is tried this many times, once per run, before it is set aside.
MAX_ATTEMPTS = 3

COMMENT_FOOTER = (
    "\n\n---\n*This forecast and its reasoning were written by an automated Claude "
    "session doing its own web research. No person saw or edited it before it was "
    "posted. The checks and the posting are done by the code in this bot's repository.*"
)

_ACCOUNT_ERROR_PATTERN = re.compile(
    r"\b(401|403)\b|unauthorized|authentication|invalid token|forbidden", re.IGNORECASE
)


class Refused(Exception):
    """A forecast that must not be posted, with a reason a person can read."""


def load_floors() -> tuple[float, float]:
    """The yes/no cap and the per-option floor, from bot_settings.txt and the environment."""
    file_values: dict[str, str] = {}
    if SETTINGS_FILE.is_file():
        file_values = fm.parse_settings_text(SETTINGS_FILE.read_text(encoding="utf-8"))
    settings = fm.load_settings(fm.merge_settings(file_values, os.environ))
    return settings.prob_floor, settings.option_floor


def find_question(client: MetaculusClient, forecast: fm.ForecastFile) -> MetaculusQuestion:
    found = client.get_question_by_post_id(
        forecast.post_id, group_question_mode="unpack_subquestions"
    )
    candidates = found if isinstance(found, list) else [found]
    for question in candidates:
        if question.id_of_question == forecast.question_id:
            return question
    raise Refused(f"post {forecast.post_id} has no question {forecast.question_id}")


def in_testing_area(question: MetaculusQuestion) -> bool:
    return TESTING_AREA in (question.tournament_slugs or [])


def check_can_forecast(question: MetaculusQuestion, now: datetime) -> None:
    closes = fm.as_utc(question.close_time)
    if closes is not None and closes <= now:
        raise Refused(f"the question closed at {fm.format_utc(closes)}")
    state = getattr(getattr(question, "state", None), "value", None)
    if state is not None and state != "open":
        raise Refused(f"the question is {state}, not open")
    # One forecast per scored question. The testing area can be forecast again.
    if question.already_forecasted and not in_testing_area(question):
        raise Refused("the bot has already forecast this question")


def _percent(value: float) -> str:
    """A probability as a percentage, with enough digits to show a very small or very large one."""
    return f"{value:.1%}" if 0.01 <= value <= 0.99 else f"{value:.2%}"


def _describe_percentiles(forecast: fm.ForecastFile) -> str:
    parts = []
    for fraction, value in forecast.percentiles:
        if forecast.kind == "date":
            shown = datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            shown = f"{value:g}"
        parts.append(f"{fraction * 100:g}%: {shown}")
    return ", ".join(parts)


def build_prediction(
    forecast: fm.ForecastFile,
    question: MetaculusQuestion,
    prob_floor: float,
    option_floor: float,
) -> tuple[Any, str]:
    """
    Turns the checked forecast into exactly what Metaculus takes for this
    question, with the guard rails applied. Returns it with a readable summary.
    """
    kind = getattr(question, "question_type", None)
    if kind != forecast.kind:
        raise Refused(f"the forecast is '{forecast.kind}' but the question is '{kind}'")

    if kind == "binary":
        assert forecast.probability is not None
        probability = fm.clamp_probability(forecast.probability, prob_floor)
        summary = f"{probability:.1%}"
        if abs(probability - forecast.probability) >= 0.0005:
            summary += (
                f" (the reasoning lands on {_percent(forecast.probability)}; this bot never "
                f"posts below {prob_floor * 100:g}% or above {(1 - prob_floor) * 100:g}%)"
            )
        return probability, summary

    if kind == "multiple_choice":
        options = list(question.options)  # type: ignore[attr-defined]
        try:
            aligned = fm.align_options(forecast.options, options)
        except ValueError as error:
            raise Refused(str(error)) from None
        floor = fm.effective_option_floor(len(options), option_floor)
        probabilities = fm.floor_and_normalize(aligned, floor)
        summary = ", ".join(f"{name}: {p:.1%}" for name, p in zip(options, probabilities))
        if max(abs(a - b) for a, b in zip(aligned, probabilities)) >= 0.005:
            summary += (
                f" (adjusted so every option has at least {floor * 100:g}% and the total is 100%)"
            )
        return dict(zip(options, probabilities)), summary

    percentiles = [
        Percentile(percentile=fraction, value=value) for fraction, value in forecast.percentiles
    ]
    try:
        distribution = NumericDistribution.from_question(percentiles, question)  # type: ignore[arg-type]
        curve = [point.percentile for point in distribution.get_cdf()]
    except Exception as error:
        raise Refused(
            "the percentiles do not make a valid distribution for this question: "
            f"{type(error).__name__}: {str(error)[:300]}"
        ) from None
    return curve, _describe_percentiles(forecast)


def build_comment(forecast: fm.ForecastFile, summary: str) -> str:
    """The comment that goes with the forecast: what was posted, then the session's reasoning."""
    label = "Forecast posted (percentiles)" if forecast.kind in fm.CONTINUOUS_KINDS else "Forecast posted"
    heading = f"**{label}:** {summary}\n\n" if summary else ""
    return heading + forecast.reasoning + COMMENT_FOOTER


def send_forecast(client: MetaculusClient, question: MetaculusQuestion, prediction: Any) -> None:
    kind = getattr(question, "question_type", None)
    assert question.id_of_question is not None
    if kind == "binary":
        client.post_binary_question_prediction(question.id_of_question, prediction)
    elif kind == "multiple_choice":
        client.post_multiple_choice_question_prediction(question.id_of_question, prediction)
    else:
        client.post_numeric_question_prediction(question.id_of_question, prediction)


def send_comment(client: MetaculusClient, question: MetaculusQuestion, comment: str) -> None:
    assert question.id_of_post is not None
    client.post_question_comment(question.id_of_post, comment)


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def process(
    path: Path,
    client: MetaculusClient,
    floors: tuple[float, float],
    now: datetime,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Handles one pending file and says what became of it: posted, checked, failed or waiting."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        data = {"unreadable_file": path.name}
        reason = f"the file is not valid JSON: {error}"
        if not dry_run:
            _write(FAILED / path.name, {**data, "error": reason, "failed_at": list_time(now)})
            path.unlink(missing_ok=True)
        return {"file": path.name, "status": "failed", "detail": reason}
    if not isinstance(data, dict):
        data = {"content": data}

    try:
        forecast = fm.parse_forecast_file(data)
        expected_name = fm.forecast_file_name(forecast.question_id)
        if path.name != expected_name:
            raise Refused(f"the file must be named {expected_name}")
        question = find_question(client, forecast)
        # An earlier run may have posted the number and then failed on the comment.
        # Then only the comment is owed, and the question's state no longer matters.
        already_sent = bool(data.get("forecast_sent_at"))
        if already_sent:
            prediction, summary = None, str(data.get("posted_as") or "")
        else:
            check_can_forecast(question, now)
            prediction, summary = build_prediction(forecast, question, *floors)
        comment = build_comment(forecast, summary)
        if dry_run:
            return {"file": path.name, "status": "checked", "detail": summary}
        if not already_sent:
            send_forecast(client, question, prediction)
            # Written down at once, so a failure from here on can never lead to a
            # second forecast or to a forecast left without its reasoning.
            data = {**data, "forecast_sent_at": list_time(now), "posted_as": summary}
            _write(path, data)
        send_comment(client, question, comment)
    except (Refused, ValueError) as error:
        # Wrong in itself: trying again cannot help.
        reason = str(error)
        if not dry_run:
            _write(FAILED / path.name, {**data, "error": reason, "failed_at": list_time(now)})
            path.unlink(missing_ok=True)
        return {"file": path.name, "status": "failed", "detail": reason}
    except Exception as error:
        # Something outside went wrong (network, Metaculus). Try again next run.
        reason = f"{type(error).__name__}: {str(error)[:300]}"
        if data.get("forecast_sent_at"):
            reason = f"the forecast was posted but its comment was not: {reason}"
        attempts = int(data.get("attempts", 0) or 0) + 1
        if dry_run:
            return {"file": path.name, "status": "waiting", "detail": reason}
        if attempts >= MAX_ATTEMPTS:
            _write(
                FAILED / path.name,
                {**data, "attempts": attempts, "error": reason, "failed_at": list_time(now)},
            )
            path.unlink(missing_ok=True)
            return {"file": path.name, "status": "failed", "detail": reason}
        _write(path, {**data, "attempts": attempts, "last_error": reason})
        return {"file": path.name, "status": "waiting", "detail": reason}

    record = {
        **data,
        "posted_at": str(data.get("forecast_sent_at") or list_time(now)),
        "posted_as": summary,
        "question_title": question.question_text,
        "question_url": question.page_url,
        "tournaments": list(question.tournament_slugs or []),
    }
    record.pop("last_error", None)
    record.pop("forecast_sent_at", None)
    _write(POSTED / path.name, record)
    path.unlink(missing_ok=True)
    return {"file": path.name, "status": "posted", "detail": summary}


def list_time(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def pending_files(folder: Path | None = None) -> list[Path]:
    folder = PENDING if folder is None else folder
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.glob("q*.json") if path.is_file())


def run(client: MetaculusClient, now: datetime, dry_run: bool = False) -> list[dict[str, Any]]:
    floors = load_floors()
    return [process(path, client, floors, now, dry_run) for path in pending_files()]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Post the forecasts waiting in forecasts/pending")
    parser.add_argument("--dry-run", action="store_true", help="Check everything, post nothing")
    args = parser.parse_args()

    if not pending_files():
        print("No forecasts are waiting.")
        return

    results = run(MetaculusClient(), datetime.now(timezone.utc), dry_run=args.dry_run)
    for result in results:
        print(f"{result['status']:8s} {result['file']}: {result['detail']}")
    counts = {status: sum(r["status"] == status for r in results) for status in ("posted", "checked", "failed", "waiting")}
    print(
        f"\n{counts['posted']} posted, {counts['checked']} checked only, "
        f"{counts['failed']} failed, {counts['waiting']} waiting for another try"
    )

    nothing_got_through = counts["posted"] == 0 and counts["checked"] == 0
    blocked = [r for r in results if _ACCOUNT_ERROR_PATTERN.search(r["detail"])]
    if nothing_got_through and blocked:
        print(
            "Metaculus refused the bot's token. Check the METACULUS_TOKEN secret at "
            "https://www.metaculus.com/futureeval/participate/"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
