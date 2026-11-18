"""
Checks the forecast files waiting in forecasts/pending before they are pushed.

A Claude session runs this before every push (see FORECASTING.md). It needs
nothing but Python: no packages, no network, no token. It applies the same
checks the posting step applies to the file itself, and then holds each
forecast against its question in state/open-questions.json: the type, the
option names, the bounds and the scale. Whether the question is still open can
only be known when the forecast is posted.

Usage: python3 check_forecast_files.py [folder]
Exit code 0 when every file passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forecast_math as fm  # noqa: E402

ROOT = Path(__file__).resolve().parent
DEFAULT_FOLDER = ROOT / "forecasts" / "pending"
QUESTION_LIST = ROOT / "state" / "open-questions.json"


def load_cards(path: Path = QUESTION_LIST) -> dict[int, dict[str, Any]] | None:
    """The questions in the list by question id, or None when there is no readable list."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(card["question_id"]): card for card in data["questions"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def deadline_passed(card: dict[str, Any], now: datetime) -> bool:
    """Whether the moment this question's forecast had to be in has gone by."""
    try:
        return fm.parse_when(card.get("deadline") or card.get("close_time")) <= now
    except ValueError:
        return False


def check_folder(
    folder: Path,
    cards: dict[int, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str | None]]:
    """
    Returns (file name, problem) for every forecast file; problem is None when it
    passes. With the question cards, each forecast is also held against its question.
    """
    now = datetime.now(timezone.utc) if now is None else now
    results: list[tuple[str, str | None]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            forecast = fm.parse_forecast_file(data)
            expected = fm.forecast_file_name(forecast.question_id)
            if path.name != expected:
                raise ValueError(f"the file must be named {expected}")
            if cards is not None:
                card = cards.get(forecast.question_id)
                if card is None:
                    raise ValueError(
                        "this question is not in state/open-questions.json (it has closed "
                        "or been forecast already): delete this file"
                    )
                if deadline_passed(card, now):
                    raise ValueError("this question's deadline has passed: delete this file")
                problem = fm.check_against_card(forecast, card)
                if problem:
                    raise ValueError(problem)
        except (OSError, ValueError) as error:
            results.append((path.name, str(error)))
        else:
            results.append((path.name, None))
    return results


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FOLDER
    cards = load_cards()
    results = check_folder(folder, cards) if folder.is_dir() else []
    if not results:
        print(f"No forecast files in {folder}")
        return
    for name, problem in results:
        print(f"{'REFUSED' if problem else 'ok':8s} {name}" + (f": {problem}" if problem else ""))
    refused = sum(1 for _, problem in results if problem)
    print(f"\n{len(results) - refused} of {len(results)} file(s) pass")
    if cards is None:
        print("(state/open-questions.json could not be read, so the files were not held against their questions)")
    if refused:
        sys.exit(1)


if __name__ == "__main__":
    main()
