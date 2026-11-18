"""
Checks the forecast files waiting in forecasts/pending before they are pushed.

A Claude session runs this as the last step before it pushes (see
FORECASTING.md). It needs nothing but Python: no packages, no network, no token.
It applies the same checks the posting step applies to the file itself. Checks
that need the live question (is it still open, do the option names match, do the
percentiles fit the bounds) can only happen when the forecast is posted.

Usage: python3 check_forecast_files.py [folder]
Exit code 0 when every file passes, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forecast_math as fm  # noqa: E402

DEFAULT_FOLDER = Path(__file__).resolve().parent / "forecasts" / "pending"


def check_folder(folder: Path) -> list[tuple[str, str | None]]:
    """Returns (file name, problem) for every forecast file; problem is None when it passes."""
    results: list[tuple[str, str | None]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            forecast = fm.parse_forecast_file(data)
            expected = fm.forecast_file_name(forecast.question_id)
            if path.name != expected:
                raise ValueError(f"the file must be named {expected}")
        except (OSError, ValueError) as error:
            results.append((path.name, str(error)))
        else:
            results.append((path.name, None))
    return results


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FOLDER
    results = check_folder(folder) if folder.is_dir() else []
    if not results:
        print(f"No forecast files in {folder}")
        return
    for name, problem in results:
        print(f"{'REFUSED' if problem else 'ok':8s} {name}" + (f": {problem}" if problem else ""))
    refused = sum(1 for _, problem in results if problem)
    print(f"\n{len(results) - refused} of {len(results)} file(s) pass")
    if refused:
        sys.exit(1)


if __name__ == "__main__":
    main()
