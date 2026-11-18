"""
Step one of the Claude route (see FORECASTING.md).

Writes the open tournament questions this bot has not answered yet to
state/open-questions.json, so that a Claude session can read them, research
them and write a forecast for each.

This runs inside GitHub Actions, the only place the Metaculus token lives. It
reads questions and nothing else: it posts nothing and calls no AI model.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forecasting_tools import MetaculusClient, MetaculusQuestion

import forecast_math as fm

logger = logging.getLogger(__name__)

STATE_FILE = Path("state/open-questions.json")
REQUEST_FILE = Path("state/poll-request.json")
MODES = ("tournament", "test_questions")
TESTING_AREA = "bot-testing-area"
# A question this close to closing cannot be researched and posted in time.
MIN_MINUTES_LEFT = 10


def iso(moment: datetime | None) -> str | None:
    utc_moment = fm.as_utc(moment)
    return None if utc_moment is None else utc_moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def tournaments_for(mode: str, client: MetaculusClient) -> list[tuple[str, int | str]]:
    if mode == "test_questions":
        return [("Bot testing area", TESTING_AREA)]
    return [
        ("Seasonal tournament", client.CURRENT_AI_COMPETITION_ID),
        ("MiniBench", client.CURRENT_MINIBENCH_ID),
    ]


def describe(question: MetaculusQuestion, tournament: str) -> dict[str, Any]:
    """Everything a forecaster needs to know about one question, and nothing it must not use."""
    kind = getattr(question, "question_type", None)
    card: dict[str, Any] = {
        "question_id": question.id_of_question,
        "post_id": question.id_of_post,
        "type": kind,
        "tournament": tournament,
        "title": question.question_text,
        "url": question.page_url,
        "background": question.background_info or "",
        "resolution_criteria": question.resolution_criteria or "",
        "fine_print": question.fine_print or "",
        "open_time": iso(question.open_time),
        "close_time": iso(question.close_time),
        "scheduled_resolution_time": iso(question.scheduled_resolution_time),
    }
    if question.group_question_option:
        card["group_option"] = question.group_question_option
    if question.already_forecasted:
        card["already_forecasted"] = True

    if kind == "multiple_choice":
        card["options"] = list(question.options)  # type: ignore[attr-defined]
    elif kind in ("numeric", "discrete"):
        lower = getattr(question, "nominal_lower_bound", None)
        upper = getattr(question, "nominal_upper_bound", None)
        card["unit"] = question.unit_of_measure or ""
        card["lower_bound"] = question.lower_bound if lower is None else lower  # type: ignore[attr-defined]
        card["upper_bound"] = question.upper_bound if upper is None else upper  # type: ignore[attr-defined]
        card["open_lower_bound"] = question.open_lower_bound  # type: ignore[attr-defined]
        card["open_upper_bound"] = question.open_upper_bound  # type: ignore[attr-defined]
        if kind == "discrete":
            size = question.cdf_size  # type: ignore[attr-defined]
            span = question.upper_bound - question.lower_bound  # type: ignore[attr-defined]
            step = round(span / (size - 1), 9) if size and size > 1 else None
            card["step"] = step
            # The card always shows the first and last value that can be the answer.
            # The curve itself runs half a step further out on each side.
            if step and lower is None:
                card["lower_bound"] = round(question.lower_bound + step / 2, 9)  # type: ignore[attr-defined]
            if step and upper is None:
                card["upper_bound"] = round(question.upper_bound - step / 2, 9)  # type: ignore[attr-defined]
    elif kind == "date":
        card["lower_bound"] = iso(question.lower_bound)  # type: ignore[attr-defined]
        card["upper_bound"] = iso(question.upper_bound)  # type: ignore[attr-defined]
        card["open_lower_bound"] = question.open_lower_bound  # type: ignore[attr-defined]
        card["open_upper_bound"] = question.open_upper_bound  # type: ignore[attr-defined]
    return card


def gather(
    client: MetaculusClient, mode: str, now: datetime, request_id: str | None = None
) -> dict[str, Any]:
    """Collects the questions to forecast, the ones left out (with the reason) and any errors."""
    questions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[int] = set()

    for label, tournament_id in tournaments_for(mode, client):
        try:
            found = client.get_all_open_questions_from_tournament(tournament_id)
        except Exception as error:
            errors.append(f"{label}: {type(error).__name__}: {str(error)[:300]}")
            logger.error(f"Could not load {label} ({tournament_id}): {error}")
            continue

        for question in found:
            question_id = question.id_of_question
            if question_id is None or question.id_of_post is None or question_id in seen:
                continue
            seen.add(question_id)
            kind = getattr(question, "question_type", None)

            def leave_out(reason: str) -> None:
                skipped.append(
                    {
                        "question_id": question_id,
                        "post_id": question.id_of_post,
                        "title": question.question_text,
                        "reason": reason,
                    }
                )

            # In the testing area a question can be forecast again and again.
            if question.already_forecasted and mode != "test_questions":
                continue
            if kind not in fm.FORECAST_KINDS:
                leave_out(f"question type '{kind}' is not handled yet")
                continue
            closes = fm.as_utc(question.close_time)
            if closes is not None:
                minutes_left = (closes - now).total_seconds() / 60
                if minutes_left < MIN_MINUTES_LEFT:
                    leave_out("closes too soon to research and post in time")
                    continue
            questions.append(describe(question, label))

    questions.sort(key=lambda card: card["close_time"] or "9999")
    return {
        "generated_at": iso(now),
        "mode": mode,
        "request_id": request_id,
        "questions": questions,
        "skipped": skipped,
        "errors": errors,
    }


def read_request(path: Path = REQUEST_FILE) -> dict[str, Any]:
    """The Claude session's request for a fresh list: which mode, and an id to echo back."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="List the open questions the bot has not answered")
    parser.add_argument("--mode", choices=MODES, default=None)
    parser.add_argument("--out", type=Path, default=STATE_FILE)
    args = parser.parse_args()

    request = read_request()
    mode = args.mode or request.get("mode")
    if mode not in MODES:
        mode = "tournament"
    request_id = request.get("request_id")
    if not isinstance(request_id, str):
        request_id = None

    client = MetaculusClient()
    result = gather(client, mode, datetime.now(timezone.utc), request_id)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"{len(result['questions'])} question(s) to forecast, "
        f"{len(result['skipped'])} left out, {len(result['errors'])} error(s), mode {mode}"
    )
    for error in result["errors"]:
        print(f"  error: {error}")
    wanted = len(tournaments_for(mode, client))
    if result["errors"] and len(result["errors"]) >= wanted:
        print(
            "Metaculus would not hand over any questions. The usual cause is the "
            "METACULUS_TOKEN secret: check it at https://www.metaculus.com/futureeval/participate/"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
