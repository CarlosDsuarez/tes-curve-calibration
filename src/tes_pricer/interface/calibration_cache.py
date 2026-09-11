"""Persist a finished calibration so the Excel bridge never calibrates in a cell.

A worksheet formula fires once per cell, per recalculation. Fitting the NSS
curve takes seconds; dragging ``=TASA_CERO_CUPON(A2)`` down eighty rows would
take minutes. So the run happens once, here, and lands in ``data/processed`` as
JSON that every UDF reads back:

* ``calibration_YYYY-MM-DD.json`` - one file per settlement date, kept.
* ``latest_calibration.json`` - a copy of the most recent run, which is what a
  formula without an explicit date resolves to.

What goes in the file is everything the three UDFs need and nothing they must
recompute: the six NSS parameters, the fit diagnostics, both short-end curves
as their pillars, the FX spot, and the static terms of every bond in the
calibration universe. Compounding bases are written next to each curve because
the two are different (NSS is continuous, the short curves are effective
annual) and a reader that has to guess will guess wrong.

:func:`build_snapshot` replays the same stages the end-to-end test does - the
data layer's manual-export loader, the yield inversion, ``calibrate_nss`` and
the short-curve builders - so the number a cell shows is the number the test
tier asserted on. The ``--prices`` / ``--market`` defaults point at the one
sourced market day this repository holds (14 August 2026); pass a different
export and snapshot to calibrate another day.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import numpy as np
import yaml

from tes_pricer.data.suameca_client import SuamecaClient
from tes_pricer.math.bond_pricing import BondTerms, yield_to_maturity
from tes_pricer.math.calibration import NSSCalibrationResult, calibrate_nss
from tes_pricer.math.day_count import DayCount
from tes_pricer.math.nss_model import NSSParams
from tes_pricer.math.ois_curve import (
    ShortRateCurve,
    build_cop_short_curve,
    build_usd_short_curve,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Final = "1.0.0"
LATEST_FILENAME: Final = "latest_calibration.json"

PROJECT_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR: Final = PROJECT_ROOT / "data" / "processed"
DEFAULT_REFERENCE_PATH: Final = (
    PROJECT_ROOT / "src" / "tes_pricer" / "config" / "tes_referencia.yaml"
)

# The only day this repository holds a sourced per-bond price set for. See
# tests/fixtures/e2e_20260814/PROVENANCE.md for the tier of every input.
_FIXTURE_DIR: Final = PROJECT_ROOT / "tests" / "fixtures" / "e2e_20260814"
DEFAULT_PRICES_PATH: Final = _FIXTURE_DIR / "tes_precios_2026-08-14.csv"
DEFAULT_MARKET_SNAPSHOT_PATH: Final = _FIXTURE_DIR / "market_snapshot_2026-08-14.yaml"
DEFAULT_SETTLEMENT_DATE: Final = date(2026, 8, 14)

DAYS_PER_YEAR: Final = 365.0
"""ACT/365: the axis the NSS maturities are measured on, matching the pipeline."""

# Named short-curve pillars in the market snapshot, in years.
_COP_NAMED_TENORS: Final = {"6M": 0.5, "12M": 1.0}
_USD_NAMED_TENORS: Final = {"3M": 0.25, "6M": 0.5, "12M": 1.0}


def dated_filename(calibration_date: date) -> str:
    """File name of the snapshot for one settlement date."""
    return f"calibration_{calibration_date.isoformat()}.json"


def resolve_snapshot_path(directory: Path, calibration_date: date | None) -> Path:
    """Where the snapshot for ``calibration_date`` lives; ``None`` means the latest."""
    if calibration_date is None:
        return Path(directory) / LATEST_FILENAME
    return Path(directory) / dated_filename(calibration_date)


# --------------------------------------------------------------------------- #
# The snapshot
# --------------------------------------------------------------------------- #
def _curve_to_dict(curve: ShortRateCurve) -> dict[str, Any]:
    return {
        "currency": curve.currency,
        "curve_date": curve.curve_date.isoformat(),
        "compounding": "effective_annual_act365",
        "tenors_years": [float(t) for t in curve.tenors_years],
        "rates": [float(r) for r in curve.rates],
    }


def _curve_from_dict(payload: Mapping[str, Any]) -> ShortRateCurve:
    return ShortRateCurve(
        tenors_years=np.asarray(payload["tenors_years"], dtype=np.float64),
        rates=np.asarray(payload["rates"], dtype=np.float64),
        currency=str(payload["currency"]),
        curve_date=date.fromisoformat(str(payload["curve_date"])),
    )


def _terms_to_dict(terms: BondTerms) -> dict[str, Any]:
    return {
        "isin": terms.isin,
        "issue_date": terms.issue_date.isoformat(),
        "maturity_date": terms.maturity_date.isoformat(),
        "coupon_rate": terms.coupon_rate,
        "face_value": terms.face_value,
        "coupon_frequency": terms.coupon_frequency,
        "day_count": str(terms.day_count),
    }


def _terms_from_dict(payload: Mapping[str, Any]) -> BondTerms:
    return BondTerms(
        isin=str(payload["isin"]),
        issue_date=date.fromisoformat(str(payload["issue_date"])),
        maturity_date=date.fromisoformat(str(payload["maturity_date"])),
        coupon_rate=float(payload["coupon_rate"]),
        face_value=float(payload["face_value"]),
        coupon_frequency=int(payload["coupon_frequency"]),
        day_count=DayCount(str(payload["day_count"])),
    )


@dataclass(frozen=True, slots=True)
class CalibrationSnapshot:
    """One calibration run, complete enough to price from without recomputing.

    Attributes:
        calibration_date: Settlement date the curve was fitted for. It is also
            the observation date of both short curves and of the FX spot.
        generated_at: ISO 8601 UTC timestamp of the run.
        params: The fitted NSS parameters, continuously compounded.
        diagnostics: Fit diagnostics as plain JSON values (``rmse_bps``,
            ``max_abs_error_bps``, ``n_bonds_used``, ``converged``, ...).
        cop_curve: COP short-end curve, effective annual ACT/365.
        usd_curve: USD short-end curve, effective annual ACT/365.
        fx_spot_cop_per_usd: TRM, COP per 1 USD.
        bonds: The calibration universe keyed by its authoritative identifier
            (the ``nemotecnico``; see ``tes_referencia.yaml``).
        aliases: Alternative identifiers, e.g. a real ISIN, mapped onto the key
            used in ``bonds``. Empty unless the reference file carries them.
        sources: Free-form provenance: which files produced this run.
    """

    calibration_date: date
    generated_at: str
    params: NSSParams
    diagnostics: dict[str, Any]
    cop_curve: ShortRateCurve
    usd_curve: ShortRateCurve
    fx_spot_cop_per_usd: float
    bonds: dict[str, BondTerms]
    aliases: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The JSON document, as plain Python containers."""
        return {
            "schema_version": SCHEMA_VERSION,
            "calibration_date": self.calibration_date.isoformat(),
            "generated_at": self.generated_at,
            "nss_params": {
                "beta0": self.params.beta0,
                "beta1": self.params.beta1,
                "beta2": self.params.beta2,
                "beta3": self.params.beta3,
                "lambda1": self.params.lambda1,
                "lambda2": self.params.lambda2,
                "compounding": "continuous",
            },
            "diagnostics": dict(self.diagnostics),
            "cop_short_curve": _curve_to_dict(self.cop_curve),
            "usd_short_curve": _curve_to_dict(self.usd_curve),
            "fx_spot_cop_per_usd": self.fx_spot_cop_per_usd,
            "bonds": {key: _terms_to_dict(terms) for key, terms in self.bonds.items()},
            "aliases": dict(self.aliases),
            "sources": dict(self.sources),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CalibrationSnapshot:
        """Rebuild a snapshot from :meth:`to_dict` output.

        Raises:
            ValueError: If ``schema_version`` is not one this reader understands.
        """
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported calibration snapshot schema_version {version!r}; "
                f"this reader understands {SCHEMA_VERSION!r}. Re-run "
                "`python -m tes_pricer.interface.calibration_cache` to regenerate it."
            )
        nss = payload["nss_params"]
        return cls(
            calibration_date=date.fromisoformat(str(payload["calibration_date"])),
            generated_at=str(payload["generated_at"]),
            params=NSSParams(
                beta0=float(nss["beta0"]),
                beta1=float(nss["beta1"]),
                beta2=float(nss["beta2"]),
                beta3=float(nss["beta3"]),
                lambda1=float(nss["lambda1"]),
                lambda2=float(nss["lambda2"]),
            ),
            diagnostics=dict(payload["diagnostics"]),
            cop_curve=_curve_from_dict(payload["cop_short_curve"]),
            usd_curve=_curve_from_dict(payload["usd_short_curve"]),
            fx_spot_cop_per_usd=float(payload["fx_spot_cop_per_usd"]),
            bonds={str(k): _terms_from_dict(v) for k, v in payload["bonds"].items()},
            aliases={str(k): str(v) for k, v in payload.get("aliases", {}).items()},
            sources={str(k): str(v) for k, v in payload.get("sources", {}).items()},
        )

    def resolve_bond(self, identifier: str) -> BondTerms:
        """Look a bond up by its ``nemotecnico`` or by any registered alias.

        Raises:
            KeyError: If the identifier is unknown; the message lists the universe.
        """
        key = identifier.strip().upper()
        key = self.aliases.get(key, key)
        try:
            return self.bonds[key]
        except KeyError:
            known = ", ".join(sorted(self.bonds))
            raise KeyError(
                f"{identifier!r} is not in the calibration universe of "
                f"{self.calibration_date.isoformat()}; known identifiers: {known}"
            ) from None


# --------------------------------------------------------------------------- #
# Disk
# --------------------------------------------------------------------------- #
def save_snapshot(snapshot: CalibrationSnapshot, directory: Path) -> tuple[Path, Path]:
    """Write the dated file and refresh ``latest``; returns both paths."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False) + "\n"
    dated = target / dated_filename(snapshot.calibration_date)
    latest = target / LATEST_FILENAME
    dated.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    return dated, latest


def load_snapshot(path: Path) -> CalibrationSnapshot:
    """Read one snapshot file.

    Raises:
        FileNotFoundError: With the command that produces the file, since the
            usual reason it is missing is that nobody has run a calibration yet.
        ValueError: If the file's schema version is unsupported.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"no calibration snapshot at {path}. Generate one with "
            "`python -m tes_pricer.interface.calibration_cache --date YYYY-MM-DD` "
            "(or run excel/build_excel_toolkit.py with --seed-cache)."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    return CalibrationSnapshot.from_dict(payload)


# --------------------------------------------------------------------------- #
# Building one from the data files
# --------------------------------------------------------------------------- #
def _terms_from_reference(reference_path: Path) -> tuple[dict[str, BondTerms], dict[str, str]]:
    """The calibration universe from ``tes_referencia.yaml``, keyed by nemotecnico."""
    reference = yaml.safe_load(Path(reference_path).read_text(encoding="utf-8"))
    bonds: dict[str, BondTerms] = {}
    aliases: dict[str, str] = {}
    for bond in reference["bonos_benchmark"]:
        key = str(bond["nemotecnico"])
        bonds[key] = BondTerms(
            isin=key,
            issue_date=date.fromisoformat(str(bond["fecha_emision"])),
            maturity_date=date.fromisoformat(str(bond["fecha_vencimiento"])),
            coupon_rate=float(bond["tasa_cupon_nominal"]),
            face_value=float(bond["valor_nominal"]),
            coupon_frequency=1 if bond["frecuencia_cupon"] == "anual" else 2,
            day_count=DayCount(str(bond["day_count_convention"])),
        )
        if bond.get("isin"):
            aliases[str(bond["isin"]).upper()] = key
    return bonds, aliases


def _pillars(block: Mapping[str, Any]) -> dict[str, float]:
    return {str(k): float(v) for k, v in dict(block["pillars_effective_annual"]).items()}


def _diagnostics(result: NSSCalibrationResult) -> dict[str, Any]:
    return {
        "rmse_bps": result.rmse_bps,
        "max_abs_error_bps": result.max_abs_error * 1e4,
        "n_bonds_used": result.n_bonds_used,
        "converged": result.converged,
        "n_starts_tried": result.n_starts_tried,
        "n_starts_converged": result.n_starts_converged,
        "optimization_message": result.optimization_message,
        "warnings": list(result.warnings),
        "per_bond_errors_bps": dict(result.per_bond_errors),
    }


def build_snapshot(
    prices_path: Path,
    market_snapshot_path: Path,
    reference_path: Path,
    settlement_date: date,
    *,
    n_multistart: int = 25,
    seed: int = 0,
) -> CalibrationSnapshot:
    """Run ingestion, yield inversion, calibration and curve assembly, once.

    Args:
        prices_path: Per-bond clean price export (CSV/XLSX) in the format
            :meth:`~tes_pricer.data.suameca_client.SuamecaClient.load_from_manual_export`
            accepts.
        market_snapshot_path: YAML with ``fx_spot``, ``cop_short_curve`` and
            ``usd_short_curve`` blocks (see ``tests/fixtures/e2e_20260814``).
        reference_path: ``tes_referencia.yaml`` with the bond terms.
        settlement_date: Date the prices are quoted for and the curve is fitted at.
        n_multistart: Latin-hypercube starts for the NSS fit.
        seed: Seed of those starts, so a re-run reproduces the file.

    Raises:
        KeyError: If a priced bond is missing from the reference file.
        NSSCalibrationError: If the fit does not converge.
    """
    bonds_in_reference, aliases = _terms_from_reference(reference_path)

    client = SuamecaClient(raw_cache_dir=Path(prices_path).parent, manifest_path=None)
    prices = client.load_from_manual_export(prices_path)

    universe: dict[str, BondTerms] = {}
    maturities: list[float] = []
    ytms: list[float] = []
    identifiers: list[str] = []
    clean_prices = prices["precio_limpio"].astype(float).tolist()
    for identifier, clean in zip(prices["isin"].astype(str), clean_prices, strict=True):
        terms = bonds_in_reference[identifier]
        universe[identifier] = terms
        identifiers.append(identifier)
        maturities.append((terms.maturity_date - settlement_date).days / DAYS_PER_YEAR)
        ytms.append(yield_to_maturity(terms, settlement_date, clean))

    result = calibrate_nss(ytms, maturities, identifiers, n_multistart=n_multistart, seed=seed)
    logger.info(
        "NSS fit for %s: %d bonds, RMSE %.3f bp, %d/%d starts converged",
        settlement_date,
        result.n_bonds_used,
        result.rmse_bps,
        result.n_starts_converged,
        result.n_starts_tried,
    )

    market = yaml.safe_load(Path(market_snapshot_path).read_text(encoding="utf-8"))
    cop = _pillars(market["cop_short_curve"])
    usd = _pillars(market["usd_short_curve"])
    cop_curve = build_cop_short_curve(
        cop["overnight"],
        cop.get("1M"),
        cop.get("3M"),
        settlement_date,
        additional_tenors={
            years: cop[name] for name, years in _COP_NAMED_TENORS.items() if name in cop
        },
    )
    usd_curve = build_usd_short_curve(
        usd["overnight"],
        settlement_date,
        {years: usd[name] for name, years in _USD_NAMED_TENORS.items() if name in usd},
    )
    spot = float(market["fx_spot"]["cop_per_usd"])

    return CalibrationSnapshot(
        calibration_date=settlement_date,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        params=result.to_params(),
        diagnostics=_diagnostics(result),
        cop_curve=cop_curve,
        usd_curve=usd_curve,
        fx_spot_cop_per_usd=spot,
        bonds=universe,
        aliases={alias: key for alias, key in aliases.items() if key in universe},
        sources={
            "prices": str(prices_path),
            "market_snapshot": str(market_snapshot_path),
            "reference": str(reference_path),
        },
    )


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """Arguments of ``python -m tes_pricer.interface.calibration_cache``."""
    parser = argparse.ArgumentParser(
        prog="python -m tes_pricer.interface.calibration_cache",
        description="Calibrate once and write the snapshot the Excel UDFs read.",
    )
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=DEFAULT_SETTLEMENT_DATE,
        help="Settlement date, YYYY-MM-DD (default: %(default)s, the archived market day).",
    )
    parser.add_argument("--prices", type=Path, default=DEFAULT_PRICES_PATH)
    parser.add_argument("--market", type=Path, default=DEFAULT_MARKET_SNAPSHOT_PATH)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--n-multistart", type=int, default=25)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build one snapshot and write it; returns the process exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - min(args.verbose, 2) * 10)
    snapshot = build_snapshot(
        args.prices,
        args.market,
        args.reference,
        args.date,
        n_multistart=args.n_multistart,
        seed=args.seed,
    )
    dated, latest = save_snapshot(snapshot, args.output)
    print(f"wrote {dated}\nwrote {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
