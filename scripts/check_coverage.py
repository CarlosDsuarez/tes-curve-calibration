#!/usr/bin/env python3
"""Enforce the per-package coverage floors that ``fail_under`` cannot express.

``coverage``'s own ``fail_under`` is a single global number. This repository
needs two, because two tiers of code carry different consequences when they go
untested:

* **85% package-wide.** The floor for everything under ``src/tes_pricer``.
  Already enforced by ``[tool.coverage.report] fail_under`` in
  ``pyproject.toml``; re-checked here so one command reports both floors, and so
  the failure message names which one broke.
* **95% for ``src/tes_pricer/math/``.** The numerical core. A silently wrong
  discount factor does not raise, it prices - and the curve, the forward and the
  greeks downstream inherit the error with no symptom. That is where an untested
  branch is most expensive, so it carries the tighter floor.

Percentages are computed the way ``coverage`` computes its own, with branch
coverage folded in:

    (covered_lines + covered_branches) / (num_statements + num_branches)

Usage:
    pytest -m "not network" --cov=src/tes_pricer --cov-report=json
    python scripts/check_coverage.py

Exit codes:
    0  every floor met
    1  a floor was missed; the failing packages are listed
    2  the report is missing, malformed, or matches no file under a required package
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REPORT = PROJECT_ROOT / "coverage.json"
DEFAULT_TOTAL_FLOOR = 85.0
DEFAULT_PACKAGE_FLOORS: dict[str, float] = {"src/tes_pricer/math/": 95.0}

EXIT_OK = 0
EXIT_BELOW_FLOOR = 1
EXIT_UNUSABLE_REPORT = 2


@dataclass(frozen=True, slots=True)
class Measured:
    """Coverage of one package, or of the whole report."""

    label: str
    covered: int
    total: int
    floor: float
    files: int

    @property
    def percent(self) -> float:
        """Coverage as a percentage; a package with nothing to cover counts as 100."""
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total

    @property
    def passed(self) -> bool:
        """Whether this package met its floor."""
        return self.percent >= self.floor


def _normalise(path: str) -> str:
    """Return a comparable, forward-slashed, repository-relative path."""
    text = path.replace("\\", "/")
    root = PROJECT_ROOT.as_posix() + "/"
    if text.startswith(root):
        return text[len(root) :]
    return text[2:] if text.startswith("./") else text


def _weights(summary: Mapping[str, Any]) -> tuple[int, int]:
    """Return ``(covered, total)`` units for one file, branches included."""
    covered = int(summary.get("covered_lines", 0)) + int(summary.get("covered_branches", 0))
    total = int(summary.get("num_statements", 0)) + int(summary.get("num_branches", 0))
    return covered, total


def measure(report: Mapping[str, Any], prefix: str, label: str, floor: float) -> Measured:
    """Aggregate every file in ``report`` whose path starts with ``prefix``."""
    covered = total = matched = 0
    for path, entry in dict(report.get("files", {})).items():
        if prefix and not _normalise(str(path)).startswith(prefix):
            continue
        file_covered, file_total = _weights(entry.get("summary", {}))
        covered += file_covered
        total += file_total
        matched += 1
    return Measured(label=label, covered=covered, total=total, floor=floor, files=matched)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the operator-facing arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="coverage JSON report to read (default: coverage.json at the repository root)",
    )
    parser.add_argument(
        "--total-floor",
        type=float,
        default=DEFAULT_TOTAL_FLOOR,
        help=f"package-wide minimum, in percent (default: {DEFAULT_TOTAL_FLOOR:g})",
    )
    parser.add_argument(
        "--package-floor",
        action="append",
        default=[],
        metavar="PREFIX=PERCENT",
        help=(
            "extra per-package floor, repeatable, e.g. src/tes_pricer/math/=95. "
            "Replaces the defaults when given at least once."
        ),
    )
    return parser.parse_args(argv)


def package_floors(raw: list[str]) -> dict[str, float]:
    """Turn ``PREFIX=PERCENT`` arguments into a mapping, or return the defaults."""
    if not raw:
        return dict(DEFAULT_PACKAGE_FLOORS)
    floors: dict[str, float] = {}
    for item in raw:
        prefix, separator, percent = item.partition("=")
        if not prefix or not separator or not percent:
            raise SystemExit(f"--package-floor expects PREFIX=PERCENT; got {item!r}")
        floors[prefix if prefix.endswith("/") else prefix + "/"] = float(percent)
    return floors


def main(argv: Sequence[str] | None = None) -> int:
    """Check every floor and return a process exit code."""
    args = parse_args(argv)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            f"coverage report not found at {args.report}. Generate it with "
            f"`pytest --cov=src/tes_pricer --cov-report=json`.",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE_REPORT
    except json.JSONDecodeError as exc:
        print(f"coverage report at {args.report} is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE_REPORT

    measurements = [measure(report, "", "src/tes_pricer (whole package)", args.total_floor)]
    measurements += [
        measure(report, prefix, prefix, floor)
        for prefix, floor in package_floors(args.package_floor).items()
    ]

    width = max(len(m.label) for m in measurements)
    print(f"{'package':<{width}}  {'files':>5}  {'covered':>8}  {'floor':>7}  result")
    for m in measurements:
        print(
            f"{m.label:<{width}}  {m.files:>5}  {m.percent:>7.2f}%  "
            f"{m.floor:>6.1f}%  {'ok' if m.passed else 'BELOW FLOOR'}"
        )

    unmatched = [m.label for m in measurements if m.files == 0]
    if unmatched:
        print(
            "\nno file in the report matched: "
            + ", ".join(unmatched)
            + ". The report was generated against a different source tree, or the prefix "
            "is wrong - either way that floor was not actually checked.",
            file=sys.stderr,
        )
        return EXIT_UNUSABLE_REPORT

    failed = [m for m in measurements if not m.passed]
    if failed:
        print(
            "\ncoverage below floor:\n"
            + "\n".join(
                f"  {m.label}: {m.percent:.2f}% < {m.floor:.1f}% "
                f"({m.total - m.covered} of {m.total} units uncovered)"
                for m in failed
            ),
            file=sys.stderr,
        )
        return EXIT_BELOW_FLOOR

    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
