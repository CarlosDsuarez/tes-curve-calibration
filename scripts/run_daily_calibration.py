#!/usr/bin/env python3
"""Operational entry point: the once-a-day calibration run.

Pipeline, in order, with the run aborting at the first failed stage:

1. **Ingest.** Pull TES prices, IBR fixings and the TRM for the settlement date.
2. **Archive.** Write raw payloads under ``data/raw`` and hash each one.
3. **Validate.** Run the schema validators; a ``FAILED`` status stops the run
   before any number reaches the calibration.
4. **Manifest.** Regenerate ``data/manifest.json`` with the provenance of every
   input, including the validation outcome.
5. **Calibrate.** Fit the NSS curve and bootstrap the COP OIS curve.
6. **Price.** Produce the USD/COP forward strip and its cross-currency basis.
7. **Persist.** Write results to ``data/processed`` and log the diagnostics.

The run is intentionally fail-closed: a partial ingestion never produces a
curve, because a curve that is silently missing its long end is more dangerous
than no curve at all.

Usage:
    python scripts/run_daily_calibration.py --date 2026-03-16
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger("tes_pricer.daily")

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 2
EXIT_CALIBRATION_FAILED = 3
EXIT_INGESTION_FAILED = 4


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the operator-facing arguments."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date",
        type=lambda value: datetime.strptime(value, "%Y-%m-%d").date(),
        default=date.today(),
        help="Settlement date to calibrate, YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "src" / "tes_pricer" / "config" / "tes_referencia.yaml",
        help="Benchmark bond reference file.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "processed",
        help="Where calibration output is written.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=PROJECT_ROOT / "data" / "manifest.json",
        help="Provenance manifest to regenerate.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run ingestion and validation, then stop before writing anything.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args(argv)


def configure_logging(verbosity: int) -> None:
    """Configure structured stderr logging for an unattended run."""
    level = logging.WARNING - min(verbosity, 2) * 10
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string, for the manifest."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def ingest(args: argparse.Namespace) -> None:
    """Stages 1-2: fetch and archive raw payloads."""
    raise NotImplementedError("Phase 2: ingestion stage")


def validate_and_write_manifest(args: argparse.Namespace) -> None:
    """Stages 3-4: validate the archived payloads and regenerate the manifest."""
    raise NotImplementedError("Phase 2: validation and manifest stage")


def calibrate(args: argparse.Namespace) -> None:
    """Stage 5: fit the NSS curve and bootstrap the COP OIS curve."""
    raise NotImplementedError("Phase 6: calibration stage")


def price_forwards(args: argparse.Namespace) -> None:
    """Stage 6: build the USD/COP forward strip and the cross-currency basis."""
    raise NotImplementedError("Phase 8: forward pricing stage")


def persist(args: argparse.Namespace) -> None:
    """Stage 7: write results and diagnostics to `data/processed`."""
    raise NotImplementedError("Phase 6: persistence stage")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daily pipeline and return a process exit code."""
    args = parse_args(argv)
    configure_logging(args.verbose)
    logger.info("daily calibration run for %s started at %s", args.date, utc_now_iso())

    ingest(args)
    validate_and_write_manifest(args)
    if args.dry_run:
        logger.info("dry run: stopping after validation")
        return EXIT_OK

    calibrate(args)
    price_forwards(args)
    persist(args)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
