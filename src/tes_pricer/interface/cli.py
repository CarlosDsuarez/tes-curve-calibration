"""Command line entry point for ingestion, calibration and pricing.

Subcommands:

``ingest``
    Pull raw data from SUAMECA and datos.gov.co, archive it under ``data/raw``
    and regenerate ``data/manifest.json``.
``calibrate``
    Fit the NSS curve to a settlement date's TES prices and write the parameters
    plus diagnostics to ``data/processed``.
``price-forward``
    Price USD/COP outright forwards off the calibrated curves.
``validate``
    Re-run schema validation over an existing manifest without any network
    access; this is what CI uses to confirm an archived run is reproducible.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from tes_pricer import __version__


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argument parser and its subcommands."""
    parser = argparse.ArgumentParser(
        prog="tes-pricer",
        description="TES NSS curve calibration and USD/COP forward pricing.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity; repeat for debug output.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Fetch raw data and rebuild the manifest.")
    ingest.add_argument("--start", required=True, help="Start date, YYYY-MM-DD.")
    ingest.add_argument("--end", required=True, help="End date, YYYY-MM-DD.")
    ingest.add_argument("--source", choices=["suameca", "socrata", "all"], default="all")

    calibrate = subparsers.add_parser("calibrate", help="Fit the NSS curve.")
    calibrate.add_argument("--date", required=True, help="Settlement date, YYYY-MM-DD.")
    calibrate.add_argument("--config", default="src/tes_pricer/config/tes_referencia.yaml")
    calibrate.add_argument("--output", default="data/processed")

    forward = subparsers.add_parser("price-forward", help="Price USD/COP forwards.")
    forward.add_argument("--date", required=True, help="Settlement date, YYYY-MM-DD.")
    forward.add_argument("--tenors", nargs="+", default=["1M", "3M", "6M", "1Y"])

    validate = subparsers.add_parser("validate", help="Re-validate an archived manifest.")
    validate.add_argument("--manifest", default="data/manifest.json")

    return parser


def run_ingest(args: argparse.Namespace) -> int:
    """Execute the ``ingest`` subcommand."""
    raise NotImplementedError("Phase 2: ingestion pipeline")


def run_calibrate(args: argparse.Namespace) -> int:
    """Execute the ``calibrate`` subcommand."""
    raise NotImplementedError("Phase 6: calibration pipeline")


def run_price_forward(args: argparse.Namespace) -> int:
    """Execute the ``price-forward`` subcommand."""
    raise NotImplementedError("Phase 8: forward pricing pipeline")


def run_validate(args: argparse.Namespace) -> int:
    """Execute the ``validate`` subcommand."""
    raise NotImplementedError("Phase 2: offline manifest revalidation")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected subcommand.

    Returns:
        A process exit code: ``0`` on success, non-zero on failure.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "ingest": run_ingest,
        "calibrate": run_calibrate,
        "price-forward": run_price_forward,
        "validate": run_validate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
