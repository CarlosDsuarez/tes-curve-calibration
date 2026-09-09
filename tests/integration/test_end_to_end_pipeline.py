"""The whole pipeline, once, on one frozen day of real Colombian market data.

Every other test in this repository checks one function against a synthetic
input. This one is the only place where ingestion, yield inversion, NSS
calibration, the COP and USD short curves, the USD/COP forward and its greeks
run **in sequence, in one process, off one snapshot**, which is the only way to
catch the failures that live between the modules rather than inside them: a
day count that disagrees across two phases, a percent that should have been a
decimal, a curve read at the wrong point of its own axis.

Two arms, one pipeline
----------------------
* **Cached (default).** The pipeline replays the frozen snapshot in
  ``tests/fixtures/e2e_20260814/``. No network, no environment variables, fully
  deterministic - the run CI executes.
* **Network.** One extra test re-fetches the TRM for the snapshot date from
  datos.gov.co and asserts the cached FX spot still equals what the
  Superintendencia Financiera publishes. That is what keeps the fixture honest:
  a cached number nobody ever re-checks against its source stops being data and
  becomes folklore. It is marked ``network`` and needs
  ``TES_PRICER_ALLOW_NETWORK=1``.

Why 14 August 2026
------------------
Not "thirty days ago" computed at runtime, which would move the snapshot every
morning and make the run irreproducible. ``config/tes_referencia.yaml`` carries
per-bond clean prices and traded yields for all sixteen benchmark TES **as of
that date**, cited to one primary document (MinHacienda's *Informe Diario de
Deuda Publica*). It is the only day this repository holds a sourced per-bond
price set for, so it is the day the pipeline is replayed on. See
``tests/fixtures/e2e_20260814/PROVENANCE.md`` for the tier of every input.

Thresholds, and why they are looser than the unit tier
------------------------------------------------------
The NSS fit is asserted at **20bps RMSE**, not at the sub-basis-point level the
synthetic tests use. Real quotes carry microstructure noise - bid/ask,
staleness, liquidity premia specific to one bond - that a six-factor parametric
curve cannot and should not absorb. A fit that drove the residuals to zero on
real prices would be interpolating noise, not fitting a term structure. The
observed figure is recorded in the evidence report either way, so a regression
shows up as a moving number long before it trips the threshold.

The run writes ``reports/e2e_pipeline_report.{html,md}``: what went in, what
each stage produced, and which economic property was asserted about it.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from tes_pricer.data.suameca_client import TES_PRICE_COLUMNS, SuamecaClient
from tes_pricer.data.validators import (
    ValidationStatus,
    read_manifest,
    sha256_file,
    validate_tes_referencia_yaml,
)
from tes_pricer.math.bond_pricing import BondTerms, yield_to_maturity
from tes_pricer.math.calibration import NSSCalibrationResult, calibrate_nss
from tes_pricer.math.day_count import DayCount
from tes_pricer.math.fx_forward import (
    FORWARD_POINTS_SCALE,
    FXForwardQuote,
    check_forward_points_sign,
    price_fx_forward,
)
from tes_pricer.math.greeks import (
    BASIS_POINT,
    ForwardGreeks,
    compute_forward_greeks,
    forward_value,
)
from tes_pricer.math.nss_model import nss_zero_rate
from tes_pricer.math.ois_curve import (
    ShortRateCurve,
    build_cop_short_curve,
    build_usd_short_curve,
)
from tests.integration.e2e_report import PipelineEvidence, report_directory

pytestmark = [pytest.mark.integration]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "e2e_20260814"
PRICE_FIXTURE = FIXTURE_DIR / "tes_precios_2026-08-14.csv"
SNAPSHOT_FIXTURE = FIXTURE_DIR / "market_snapshot_2026-08-14.yaml"

SNAPSHOT_DATE = date(2026, 8, 14)
"""The frozen valuation date. See the module docstring for why this one."""

SETTLEMENT_DATE = SNAPSHOT_DATE
"""Same-day settlement, which is the basis the source report quotes yields on.

Not an assumption: repricing the sixteen published clean prices at same-day
settlement reproduces the report's own ``tasa_ref`` column to under 0.1bp, while
the T+3 lag in ``tes_referencia.yaml`` misses the front two bonds by 5-7bp. T+3
is the *settlement* convention of a SEN trade; the yield printed next to a price
in the daily report is quoted to the price's own date. Stage 2 asserts that
rather than leaving it as a comment.
"""

# -- economic bands --------------------------------------------------------- #
YTM_PLAUSIBLE_MIN = 0.05
YTM_PLAUSIBLE_MAX = 0.15
"""Plausible COP nominal yields for the regime of this snapshot.

Wide on purpose. Colombia's benchmark TES traded near 12% on this date, so the
band spans roughly a 700bp move either way. It is a units-and-sanity guard - it
catches a percent that should have been a decimal, or a curve read off the wrong
currency - not a market view.
"""

PUBLISHED_YIELD_TOLERANCE_BPS = 1.0
"""How far a recomputed YTM may sit from the yield the source report printed."""

NSS_RMSE_LIMIT_BPS = 20.0
"""In-sample NSS fit limit on real quotes. See the module docstring."""

DELTA_RELATIVE_TOLERANCE = 1e-6
"""Analytic vs bumped spot delta. Both differentiate an affine payoff, so the
central difference is exact up to rounding; anything above this is a real
disagreement rather than truncation error."""

DV01_RELATIVE_TOLERANCE = 1e-3
"""Analytic vs bump-and-reprice DV01. Looser than delta because the bumped
figure carries the second-order term of a 1bp shift, which is O(1e-4) relative."""

FORWARD_TENOR_DAYS = 90
NOTIONAL_USD = 100_000.0


@dataclass(frozen=True, slots=True)
class PipelineRun:
    """Everything the pipeline produced, stage by stage, in one object."""

    snapshot: dict[str, object]
    prices: pd.DataFrame
    manifest_path: Path
    bonds: dict[str, BondTerms]
    maturities: dict[str, float]
    ytms: dict[str, float]
    published_ytms: dict[str, float]
    calibration: NSSCalibrationResult
    cop_curve: ShortRateCurve
    usd_curve: ShortRateCurve
    spot: float
    maturity_date: date
    quote: FXForwardQuote
    greeks: ForwardGreeks
    tes_reference_status: ValidationStatus


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def evidence(project_root: Path) -> Iterator[PipelineEvidence]:
    """Record the run and write the evidence report, pass or fail."""
    recorder = PipelineEvidence(snapshot_date=SNAPSHOT_DATE, mode="cached fixtures (offline)")
    yield recorder
    markdown_path, html_path = recorder.write(report_directory(project_root))
    print(f"\nend-to-end evidence report:\n  {html_path}\n  {markdown_path}")


@pytest.fixture(scope="session")
def snapshot() -> dict[str, object]:
    """The frozen non-TES inputs: FX spot, COP and USD short-curve pillars."""
    return yaml.safe_load(SNAPSHOT_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pipeline(
    evidence: PipelineEvidence,
    snapshot: dict[str, object],
    project_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> PipelineRun:
    """Run every phase in order and record what each one consumed and produced.

    The stages are *recorded* here; the properties asserted about them live in
    the tests below, so a failure points at the test that owns the property and
    the report still names the check that broke.
    """
    workspace = tmp_path_factory.mktemp("e2e")
    manifest_path = workspace / "manifest.json"

    prices, reference_status = _stage_ingest(evidence, manifest_path, workspace, project_root)
    bonds, maturities, ytms, published = _stage_yields(evidence, prices, project_root)
    calibration = _stage_calibrate(evidence, maturities, ytms)
    cop_curve, usd_curve = _stage_short_curves(evidence, snapshot)
    spot, maturity_date, quote = _stage_forward(evidence, snapshot, cop_curve, usd_curve)
    greeks = _stage_greeks(evidence, quote, cop_curve, usd_curve, maturity_date)
    _record_headline(evidence, calibration, quote, greeks)

    return PipelineRun(
        snapshot=snapshot,
        prices=prices,
        manifest_path=manifest_path,
        bonds=bonds,
        maturities=maturities,
        ytms=ytms,
        published_ytms=published,
        calibration=calibration,
        cop_curve=cop_curve,
        usd_curve=usd_curve,
        spot=spot,
        maturity_date=maturity_date,
        quote=quote,
        greeks=greeks,
        tes_reference_status=reference_status,
    )


# --------------------------------------------------------------------------- #
# Stage 1 - ingestion
# --------------------------------------------------------------------------- #
def _stage_ingest(
    evidence: PipelineEvidence,
    manifest_path: Path,
    workspace: Path,
    project_root: Path,
) -> tuple[pd.DataFrame, ValidationStatus]:
    """Phase 2: read the archived price export through the real data layer."""
    reference_path = project_root / "src" / "tes_pricer" / "config" / "tes_referencia.yaml"
    reference_report = validate_tes_referencia_yaml(str(reference_path))

    client = SuamecaClient(
        raw_cache_dir=workspace / "raw",
        manifest_path=manifest_path,
        manual_export_path=PRICE_FIXTURE,
    )
    prices = client.load_from_manual_export(PRICE_FIXTURE)
    # The per-instrument lookup is the call the daily run makes; exercise it too.
    single = client.fetch_tes_prices(str(prices["isin"].iloc[0]), SNAPSHOT_DATE, SNAPSHOT_DATE)

    stage = evidence.stage(
        "Ingestion and provenance",
        "tes_pricer.data.suameca_client, tes_pricer.data.validators",
        "Read the archived per-bond price export through the same loader the daily run "
        "uses, validate the benchmark universe against its JSON Schema, and regenerate "
        "the provenance manifest with a SHA-256 of the file actually read.",
    )
    stage.add_input("Price export", PRICE_FIXTURE.name, "MinHacienda Informe Diario, 14-ago-2026")
    stage.add_input("Benchmark universe", reference_path.name, "src/tes_pricer/config/")
    stage.add_output("Rows ingested", str(len(prices)))
    stage.add_output("Schema", ", ".join(prices.columns))
    stage.add_output("Per-instrument lookup", f"{single['isin'].iloc[0]} -> {len(single)} row")
    stage.add_output("Universe validation", reference_report.status.value)
    stage.add_output("SHA-256 of export", sha256_file(PRICE_FIXTURE)[:24] + "...")
    for message in reference_report.warnings:
        stage.note(f"Universe warning: {message}")
    stage.note(
        "precio_sucio is empty in the export and stays empty here. The source report "
        "publishes clean prices only; back-filling the dirty price with one this project "
        "computed itself would make a derived number look sourced."
    )
    return prices, reference_report.status


# --------------------------------------------------------------------------- #
# Stage 2 - yields
# --------------------------------------------------------------------------- #
def _stage_yields(
    evidence: PipelineEvidence,
    prices: pd.DataFrame,
    project_root: Path,
) -> tuple[dict[str, BondTerms], dict[str, float], dict[str, float], dict[str, float]]:
    """Phase 2/4: invert each observed clean price for the yield it implies."""
    reference_path = project_root / "src" / "tes_pricer" / "config" / "tes_referencia.yaml"
    reference = yaml.safe_load(reference_path.read_text(encoding="utf-8"))
    terms_by_id = {
        bond["nemotecnico"]: BondTerms(
            isin=bond["nemotecnico"],
            issue_date=date.fromisoformat(bond["fecha_emision"]),
            maturity_date=date.fromisoformat(bond["fecha_vencimiento"]),
            coupon_rate=float(bond["tasa_cupon_nominal"]),
            face_value=float(bond["valor_nominal"]),
            coupon_frequency=1 if bond["frecuencia_cupon"] == "anual" else 2,
            day_count=DayCount(bond["day_count_convention"]),
        )
        for bond in reference["bonos_benchmark"]
    }

    bonds: dict[str, BondTerms] = {}
    maturities: dict[str, float] = {}
    ytms: dict[str, float] = {}
    published: dict[str, float] = {}
    for row in prices.itertuples():
        identifier = str(row.isin)
        terms = terms_by_id[identifier]
        bonds[identifier] = terms
        maturities[identifier] = (terms.maturity_date - SETTLEMENT_DATE).days / 365.0
        ytms[identifier] = yield_to_maturity(terms, SETTLEMENT_DATE, float(row.precio_limpio))
        published[identifier] = float(row.tasa_negociacion)

    stage = evidence.stage(
        "Yields to maturity from observed prices",
        "tes_pricer.math.bond_pricing.yield_to_maturity",
        "Invert each observed clean price for the single yield that reprices it, by "
        "Brent's method on a bracketed, strictly decreasing price-yield function. The "
        "traded yields printed alongside the prices are held out of the pipeline and used "
        "only as the independent check below.",
    )
    stage.add_input("Bonds priced", str(len(ytms)))
    stage.add_input("Settlement", SETTLEMENT_DATE.isoformat(), "same day; asserted, not assumed")
    stage.add_input("Day count", "ACT/365", "SFC Circular Externa 100 de 1995")
    for identifier, ytm in ytms.items():
        stage.add_output(
            identifier,
            f"{ytm:.5%} @ {maturities[identifier]:.2f}y",
            f"published {published[identifier]:.5%}, "
            f"diff {(ytm - published[identifier]) * 1e4:+.2f}bp",
        )
    return bonds, maturities, ytms, published


# --------------------------------------------------------------------------- #
# Stage 3 - calibration
# --------------------------------------------------------------------------- #
def _stage_calibrate(
    evidence: PipelineEvidence,
    maturities: dict[str, float],
    ytms: dict[str, float],
) -> NSSCalibrationResult:
    """Phase 3: fit the six NSS parameters by multi-start least squares."""
    identifiers = list(ytms)
    result = calibrate_nss(
        [ytms[i] for i in identifiers],
        [maturities[i] for i in identifiers],
        identifiers,
        n_multistart=25,
        seed=0,
    )
    params = result.to_params()

    stage = evidence.stage(
        "NSS curve calibration, multi-start",
        "tes_pricer.math.calibration.calibrate_nss",
        "Fit the six Nelson-Siegel-Svensson parameters to the sixteen observed yields "
        "from 25 Latin-hypercube starting points, keeping the lowest-RMSE converged run. "
        "Multi-start is not a robustness nicety here: with the decay constants free the "
        "objective is multi-modal, so a single local solve reports whichever basin its "
        "starting point happened to fall into.",
    )
    stage.add_input("Observations", f"{result.n_bonds_used} bonds")
    stage.add_input(
        "Maturity span",
        f"{min(maturities.values()):.2f} - {max(maturities.values()):.2f} years",
    )
    stage.add_input("Starts", f"{result.n_starts_tried} Latin-hypercube points, seed 0")
    for name in ("beta0", "beta1", "beta2", "beta3"):
        stage.add_output(name, f"{getattr(params, name):+.6f}")
    stage.add_output("lambda1", f"{params.lambda1:.4f} y")
    stage.add_output("lambda2", f"{params.lambda2:.4f} y")
    stage.add_output("RMSE", f"{result.rmse_bps:.3f} bp")
    stage.add_output("Max |error|", f"{result.max_abs_error * 1e4:.3f} bp")
    stage.add_output("Starts converged", f"{result.n_starts_converged}/{result.n_starts_tried}")
    worst = sorted(result.per_bond_errors.items(), key=lambda item: -abs(item[1]))[:3]
    stage.add_output(
        "Largest residuals",
        ", ".join(f"{identifier} {error:+.2f}bp" for identifier, error in worst),
        "model minus observed",
    )
    for message in result.warnings:
        stage.note(f"Diagnostic: {message}")
    return result


# --------------------------------------------------------------------------- #
# Stage 4 - short curves
# --------------------------------------------------------------------------- #
def _pillars(block: dict[str, object]) -> dict[str, float]:
    """Read one curve's effective-annual pillars out of the snapshot."""
    return {str(k): float(v) for k, v in dict(block["pillars_effective_annual"]).items()}


def _stage_short_curves(
    evidence: PipelineEvidence,
    snapshot: dict[str, object],
) -> tuple[ShortRateCurve, ShortRateCurve]:
    """Phase 5: assemble the COP and USD short-end discount curves."""
    cop_block = dict(snapshot["cop_short_curve"])
    usd_block = dict(snapshot["usd_short_curve"])
    cop_pillars = _pillars(cop_block)
    usd_pillars = _pillars(usd_block)

    cop_curve = build_cop_short_curve(
        cop_pillars["overnight"],
        cop_pillars["1M"],
        cop_pillars["3M"],
        SNAPSHOT_DATE,
        additional_tenors={0.5: cop_pillars["6M"], 1.0: cop_pillars["12M"]},
    )
    usd_curve = build_usd_short_curve(
        usd_pillars["overnight"],
        SNAPSHOT_DATE,
        {0.25: usd_pillars["3M"], 0.5: usd_pillars["6M"], 1.0: usd_pillars["12M"]},
    )

    tenor = FORWARD_TENOR_DAYS / 365.0
    stage = evidence.stage(
        "COP and USD short-end curves",
        "tes_pricer.math.ois_curve",
        "Assemble both legs of the forward as effective annual ACT/365 curves - the basis "
        "Banco de la Republica publishes IBR on, and the basis DF = (1 + r)^-tau assumes. "
        "The NSS curve of the previous stage is continuously compounded and deliberately "
        "does not feed this one: mixing the two costs about 46bp of rate at a 10% level.",
    )
    stage.add_input(
        "COP pillars",
        ", ".join(f"{name} {rate:.4%}" for name, rate in cop_pillars.items()),
        f"tier: {cop_block['nivel_verificacion']}",
    )
    stage.add_input(
        "USD pillars",
        ", ".join(f"{name} {rate:.4%}" for name, rate in usd_pillars.items()),
        f"tier: {usd_block['nivel_verificacion']}",
    )
    for label, curve in (("COP", cop_curve), ("USD", usd_curve)):
        stage.add_output(
            f"{label} curve",
            f"{curve.tenors_years.size} pillars, "
            f"{curve.tenors_years.min():.4f}-{curve.tenors_years.max():.2f}y",
            f"r(90d) = {curve.rate(tenor):.4%} E.A., DF = {curve.discount_factor(tenor):.8f}",
        )
    stage.note(
        "Both pillar sets are scenario assumptions, not sourced fixings: datos.gov.co "
        "publishes IBR as an href stub with no rows, and free forward-looking SOFR term "
        "rates do not exist. Everything asserted downstream therefore tests the sign of "
        "the differential and the internal consistency of the greeks, never the level of "
        "the forward. See tests/fixtures/e2e_20260814/PROVENANCE.md."
    )
    return cop_curve, usd_curve


# --------------------------------------------------------------------------- #
# Stage 5 - forward
# --------------------------------------------------------------------------- #
def _stage_forward(
    evidence: PipelineEvidence,
    snapshot: dict[str, object],
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
) -> tuple[float, date, FXForwardQuote]:
    """Phase 6: price the 90-day USD/COP outright under covered interest parity."""
    fx_block = dict(snapshot["fx_spot"])
    spot = float(fx_block["cop_per_usd"])
    maturity_date = SNAPSHOT_DATE + timedelta(days=FORWARD_TENOR_DAYS)
    quote = price_fx_forward(spot, cop_curve, usd_curve, SNAPSHOT_DATE, maturity_date)

    stage = evidence.stage(
        "USD/COP 90-day forward, covered interest parity",
        "tes_pricer.math.fx_forward.price_fx_forward",
        "Price the outright as the discount-factor ratio S * DF_usd / DF_cop, reading both "
        "legs off their own curves at the same ACT/365 tenor. Under CIP the forward points "
        "and the COP-USD rate differential are the same fact seen twice, so their signs are "
        "asserted against each other rather than assumed.",
    )
    stage.add_input("Spot", f"{spot:,.2f} COP/USD", "SFC TRM via datos.gov.co 32sa-8pi3")
    stage.add_input("Notional", f"USD {NOTIONAL_USD:,.0f}")
    stage.add_input(
        "Tenor",
        f"{FORWARD_TENOR_DAYS} days, {SNAPSHOT_DATE} -> {maturity_date}",
        f"tau = {quote.tenor_years:.6f} on ACT/365",
    )
    stage.add_output("Forward", f"{quote.forward_rate:,.4f} COP/USD")
    stage.add_output(
        "Forward points",
        f"{quote.forward_points:,.1f}",
        f"scale {FORWARD_POINTS_SCALE:g}; outright premium "
        f"{quote.forward_rate - spot:+,.4f} COP",
    )
    stage.add_output("r_cop at tenor", f"{quote.r_cop:.4%} E.A.")
    stage.add_output("r_usd at tenor", f"{quote.r_usd:.4%} E.A.")
    stage.add_output("Differential", f"{quote.r_cop - quote.r_usd:+.4%}")
    stage.add_output(
        "Premium on the notional",
        f"{NOTIONAL_USD * (quote.forward_rate - spot):+,.0f} COP",
        f"USD {NOTIONAL_USD:,.0f} bought forward",
    )
    return spot, maturity_date, quote


# --------------------------------------------------------------------------- #
# Stage 6 - greeks
# --------------------------------------------------------------------------- #
def _stage_greeks(
    evidence: PipelineEvidence,
    quote: FXForwardQuote,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    maturity_date: date,
) -> ForwardGreeks:
    """Phase 7: risk of the long forward, every greek cross-checked two ways."""
    greeks = compute_forward_greeks(
        NOTIONAL_USD,
        quote.forward_rate,
        quote.spot_rate,
        cop_curve,
        usd_curve,
        SNAPSHOT_DATE,
        maturity_date,
    )
    stage = evidence.stage(
        "Forward greeks, analytic against bump-and-reprice",
        "tes_pricer.math.greeks.compute_forward_greeks",
        "Differentiate the position value V = N (S DF_usd - K DF_cop) in closed form and, "
        "independently, by repricing it under a bumped input. Reporting a closed form that "
        "nothing has checked is the failure this stage exists to prevent: an unverified "
        "risk number does not sit idle, it gets hedged on.",
    )
    stage.add_input("Position", f"long USD {NOTIONAL_USD:,.0f} against COP")
    stage.add_input("Struck at", f"{quote.forward_rate:,.4f} COP/USD", "at market, so V = 0 today")
    stage.add_output("delta_spot", f"{greeks.delta_spot:,.2f} COP per 1.00 COP of spot")
    stage.add_output("gamma_spot", f"{greeks.gamma_spot:.1f}", "exactly zero: V is affine in S")
    stage.add_output("dv01_cop", f"{greeks.dv01_cop:+,.2f} COP", "+1bp on every COP pillar")
    stage.add_output("dv01_usd", f"{greeks.dv01_usd:+,.2f} COP", "+1bp on every USD pillar")
    stage.add_output("theta", f"{greeks.theta:+,.2f} COP/day", "carry plus roll-down")
    for note in greeks.notes:
        stage.note(note)
    return greeks


def _record_headline(
    evidence: PipelineEvidence,
    calibration: NSSCalibrationResult,
    quote: FXForwardQuote,
    greeks: ForwardGreeks,
) -> None:
    """Fill the summary strip and the limitations section of the report."""
    evidence.add_headline("NSS fit", f"{calibration.rmse_bps:.2f} bp", "RMSE on 16 real quotes")
    evidence.add_headline(
        "USD/COP 90d", f"{quote.forward_rate:,.2f}", f"spot {quote.spot_rate:,.2f}"
    )
    evidence.add_headline(
        "Forward premium",
        f"{quote.forward_rate - quote.spot_rate:+,.2f} COP",
        f"differential {quote.r_cop - quote.r_usd:+.2%}",
    )
    evidence.add_headline(
        "Position DV01",
        f"{greeks.dv01_cop + greeks.dv01_usd:+,.0f} COP",
        f"COP leg {greeks.dv01_cop:+,.0f}, USD leg {greeks.dv01_usd:+,.0f}",
    )
    evidence.limitation(
        "The COP and USD short-curve pillars are scenario assumptions. No open primary "
        "source publishes IBR by tenor - the datos.gov.co asset is an empty href stub - and "
        "free forward-looking SOFR term rates do not exist. The TES prices and the FX spot "
        "are sourced; the curve pillars are stated."
    )
    evidence.limitation(
        "The NSS fit is calibrated on yields with equal weights, which is the pedagogical "
        "standard and not what a desk does. The consistent objective is price error weighted "
        "by inverse modified duration; the calibration module documents the change, and the "
        "multi-start machinery carries over to it unchanged."
    )
    evidence.limitation(
        "Bond-level greeks - DV01, key-rate durations, convexity - are still Phase 8 stubs, "
        "so this run covers the forward's risk only. The curve that would feed them is "
        "calibrated and asserted here."
    )


# --------------------------------------------------------------------------- #
# Stage 1 - assertions
# --------------------------------------------------------------------------- #
def test_ingestion_reads_every_benchmark_bond_and_records_its_provenance(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """The loader returns the declared schema, and the manifest matches the bytes read."""
    stage = evidence.stages[0]
    prices = pipeline.prices

    assert stage.check(
        "Loader returns the declared TES price schema",
        ", ".join(TES_PRICE_COLUMNS),
        ", ".join(prices.columns),
        list(prices.columns) == list(TES_PRICE_COLUMNS),
    )
    assert stage.check(
        "Every benchmark bond present, none duplicated",
        "16 unique identifiers in 16 rows",
        f"{prices['isin'].nunique()} unique in {len(prices)} rows",
        prices["isin"].nunique() == len(prices) == 16,
    )
    assert stage.check(
        "Every row is dated on the snapshot",
        SNAPSHOT_DATE.isoformat(),
        ", ".join(day.isoformat() for day in sorted(set(prices["fecha"].dt.date))),
        set(prices["fecha"].dt.date) == {SNAPSHOT_DATE},
    )
    assert stage.check(
        "Clean prices and maturities parsed out of the Colombian numeric locale",
        "0 nulls",
        f"{int(prices[['precio_limpio', 'fecha_vencimiento']].isna().sum().sum())} nulls",
        not prices[["precio_limpio", "fecha_vencimiento"]].isna().to_numpy().any(),
    )
    assert stage.check(
        "Benchmark universe passes its JSON Schema",
        ValidationStatus.PASSED.value,
        pipeline.tes_reference_status.value,
        pipeline.tes_reference_status is ValidationStatus.PASSED,
    )

    manifest = read_manifest(pipeline.manifest_path)
    record = next(s for s in manifest.data_sources if s.file_path == str(PRICE_FIXTURE))
    assert stage.check(
        "Manifest hash matches the file actually read",
        sha256_file(PRICE_FIXTURE)[:24] + "...",
        record.sha256[:24] + "...",
        record.sha256 == sha256_file(PRICE_FIXTURE),
    )


# --------------------------------------------------------------------------- #
# Stage 2 - assertions
# --------------------------------------------------------------------------- #
def test_recomputed_yields_are_plausible_and_reproduce_the_published_ones(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """Inverting the price must land in a plausible band and on the printed yield."""
    stage = evidence.stages[1]
    ytms = np.array([pipeline.ytms[i] for i in pipeline.ytms])
    published = np.array([pipeline.published_ytms[i] for i in pipeline.ytms])
    errors_bps = (ytms - published) * 1e4

    assert stage.check(
        "Every recomputed YTM is a plausible COP nominal yield",
        f"{YTM_PLAUSIBLE_MIN:.0%} - {YTM_PLAUSIBLE_MAX:.0%}",
        f"{ytms.min():.4%} - {ytms.max():.4%}",
        bool(np.all((ytms >= YTM_PLAUSIBLE_MIN) & (ytms <= YTM_PLAUSIBLE_MAX))),
    )
    assert stage.check(
        "Recomputed YTMs reproduce the yields the source report printed",
        f"max |error| < {PUBLISHED_YIELD_TOLERANCE_BPS:.1f} bp",
        f"max {np.abs(errors_bps).max():.3f} bp, mean {errors_bps.mean():+.3f} bp",
        bool(np.abs(errors_bps).max() < PUBLISHED_YIELD_TOLERANCE_BPS),
    )
    assert stage.check(
        "The universe spans the short, medium and long tramos",
        "shortest < 3y, longest > 10y",
        f"{min(pipeline.maturities.values()):.2f}y - {max(pipeline.maturities.values()):.2f}y",
        min(pipeline.maturities.values()) < 3.0 < 10.0 < max(pipeline.maturities.values()),
    )
    stage.note(
        "The published yields are an independent check, not an input: the pipeline computes "
        "its yields from the clean price alone. Reproducing the source report's own column "
        "to a fraction of a basis point is what pins the coupon schedule, the ACT/365 "
        "accrual and the settlement convention all at once."
    )


# --------------------------------------------------------------------------- #
# Stage 3 - assertions
# --------------------------------------------------------------------------- #
def test_nss_calibration_fits_the_real_curve_within_twenty_basis_points(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """The fit must converge, land inside the threshold, and not depend on one seed."""
    stage = evidence.stages[2]
    result = pipeline.calibration
    params = result.to_params()

    assert stage.check(
        "Calibration converged",
        "a converged trust-region solve",
        result.optimization_message,
        result.converged,
    )
    assert stage.check(
        "In-sample RMSE within the real-data threshold",
        f"< {NSS_RMSE_LIMIT_BPS:.0f} bp",
        f"{result.rmse_bps:.3f} bp (max residual {result.max_abs_error * 1e4:.3f} bp)",
        result.rmse_bps < NSS_RMSE_LIMIT_BPS,
    )
    assert stage.check(
        "Every multi-start converged, so the reported basin is not one lucky seed",
        f"{result.n_starts_tried}/{result.n_starts_tried}",
        f"{result.n_starts_converged}/{result.n_starts_tried}",
        result.n_starts_converged == result.n_starts_tried,
    )

    observed = np.array(sorted(pipeline.maturities.values()))
    in_sample = np.linspace(observed.min(), observed.max(), 200)
    effective_annual = np.exp(np.asarray(nss_zero_rate(in_sample, params))) - 1.0
    assert stage.check(
        "Fitted curve is a plausible COP curve across the observed maturities",
        f"{YTM_PLAUSIBLE_MIN:.0%} - {YTM_PLAUSIBLE_MAX:.0%} E.A. on "
        f"[{observed.min():.2f}y, {observed.max():.2f}y]",
        f"{effective_annual.min():.4%} - {effective_annual.max():.4%} E.A.",
        bool(
            np.all(
                (effective_annual >= YTM_PLAUSIBLE_MIN)
                & (effective_annual <= YTM_PLAUSIBLE_MAX)
            )
        ),
    )

    short_end = float(np.exp(float(nss_zero_rate(0.25, params))) - 1.0)
    stage.check(
        "Extrapolation below the shortest bond: recorded, deliberately not asserted",
        f"no instrument below {observed.min():.2f}y constrains it",
        f"z(3M) = {short_end:.4%} E.A., instantaneous short rate "
        f"{params.beta0 + params.beta1:+.4%}",
        True,
    )
    stage.note(
        f"The universe starts at {observed.min():.2f} years, so nothing in the objective "
        "constrains the curve below that. Selection is by lowest in-sample RMSE only, and "
        "the winning basin buys its fit with a short-end extrapolation that is not a usable "
        "discount curve - the module's own diagnostics flag it above. That is a property of "
        "the instrument set, not a solver failure: the fix is a money-market or OIS pillar "
        "at the front, which is exactly what the COP short curve in the next stage supplies "
        "for the forward. Assertions here are therefore confined to the maturities the data "
        "actually covers."
    )


# --------------------------------------------------------------------------- #
# Stage 4 - assertions
# --------------------------------------------------------------------------- #
def test_short_curves_are_well_formed_and_ordered_as_the_differential_requires(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """Both legs must be valid discount curves, with COP discounting harder than USD."""
    stage = evidence.stages[3]
    tenor = FORWARD_TENOR_DAYS / 365.0
    cop, usd = pipeline.cop_curve, pipeline.usd_curve

    assert stage.check(
        "Both curves carry term structure rather than a single flat pillar",
        ">= 2 pillars each",
        f"COP {cop.tenors_years.size}, USD {usd.tenors_years.size}",
        cop.tenors_years.size > 1 and usd.tenors_years.size > 1,
    )
    assert stage.check(
        "Currency labels match the legs they are passed as",
        "COP, USD",
        f"{cop.currency}, {usd.currency}",
        (cop.currency, usd.currency) == ("COP", "USD"),
    )
    assert stage.check(
        "Discount factors lie in (0, 1] at the forward tenor",
        "0 < DF <= 1",
        f"COP {cop.discount_factor(tenor):.8f}, USD {usd.discount_factor(tenor):.8f}",
        0.0 < cop.discount_factor(tenor) <= 1.0 and 0.0 < usd.discount_factor(tenor) <= 1.0,
    )
    assert stage.check(
        "COP discounts harder than USD at the forward tenor",
        "DF_cop < DF_usd",
        f"{cop.discount_factor(tenor):.8f} < {usd.discount_factor(tenor):.8f}",
        cop.discount_factor(tenor) < usd.discount_factor(tenor),
    )


# --------------------------------------------------------------------------- #
# Stage 5 - assertions
# --------------------------------------------------------------------------- #
def test_forward_points_carry_the_sign_of_the_cop_usd_differential(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """Under CIP the points and the differential are one fact; assert they agree."""
    stage = evidence.stages[4]
    quote = pipeline.quote
    differential = quote.r_cop - quote.r_usd

    with warnings.catch_warnings():
        # ForwardPointsSignWarning must not be raised here; escalate it so a
        # warning-only failure cannot pass as a green check.
        warnings.simplefilter("error")
        signs_agree = check_forward_points_sign(
            quote.forward_points, quote.r_cop, quote.r_usd, tenor_years=quote.tenor_years
        )
    assert stage.check(
        "sign(forward points) equals sign(r_cop - r_usd)",
        f"both {'positive' if differential > 0 else 'negative'}",
        f"differential {differential:+.4%}, points {quote.forward_points:+,.1f}",
        signs_agree,
    )
    assert stage.check(
        "COP trades at a forward discount, as the positive differential requires",
        "F > S",
        f"{quote.forward_rate:,.4f} > {quote.spot_rate:,.4f}",
        quote.forward_rate > quote.spot_rate,
    )

    expected = quote.spot_rate * (
        pipeline.usd_curve.discount_factor(quote.tenor_years)
        / pipeline.cop_curve.discount_factor(quote.tenor_years)
    )
    assert stage.check(
        "Priced forward equals the discount-factor ratio S * DF_usd / DF_cop",
        "relative gap < 1e-9",
        f"{abs(quote.forward_rate - expected) / expected:.3e}",
        abs(quote.forward_rate - expected) / expected < 1e-9,
    )

    annualised = (quote.forward_rate / quote.spot_rate) ** (1.0 / quote.tenor_years) - 1.0
    compounded = (1 + quote.r_cop) / (1 + quote.r_usd) - 1
    continuous = float(np.exp(quote.r_cop - quote.r_usd) - 1)
    assert stage.check(
        "Annualised carry reproduces the differential compounded, not exponentiated",
        f"(1+r_cop)/(1+r_usd) - 1 = {compounded:+.6%}",
        f"{annualised:+.6%}",
        abs(annualised - compounded) < 1e-9,
    )
    stage.note(
        "The last check is the one that catches a continuous-compounding leak. On this "
        f"curve the effective annual carry and exp(r_cop - r_usd) - 1 differ by "
        f"{abs(compounded - continuous) * 1e4:.1f}bp of annualised rate, which is a real "
        "mispricing rather than a rounding difference."
    )


# --------------------------------------------------------------------------- #
# Stage 6 - assertions
# --------------------------------------------------------------------------- #
def test_forward_greeks_reconcile_analytic_against_numeric(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """Every greek is checked against an independently derived second estimate."""
    stage = evidence.stages[5]
    greeks = pipeline.greeks
    quote = pipeline.quote
    tenor = quote.tenor_years
    cop, usd = pipeline.cop_curve, pipeline.usd_curve
    strike = quote.forward_rate

    def value(spot: float) -> float:
        return forward_value(
            NOTIONAL_USD, strike, spot, cop, usd, SNAPSHOT_DATE, pipeline.maturity_date
        )

    at_market = value(quote.spot_rate)
    assert stage.check(
        "A forward struck at the CIP rate is worth nothing on day one",
        "|V| < 1e-6 COP",
        f"{at_market:.3e} COP",
        abs(at_market) < 1e-6,
    )

    step = quote.spot_rate * 1e-4
    delta_numeric = (value(quote.spot_rate + step) - value(quote.spot_rate - step)) / (2 * step)
    delta_gap = abs(greeks.delta_spot - delta_numeric) / abs(delta_numeric)
    assert stage.check(
        "delta_spot: closed form against a central difference on the pricing function",
        f"relative gap < {DELTA_RELATIVE_TOLERANCE:.0e}",
        f"{delta_gap:.3e}; analytic {greeks.delta_spot:,.2f}, numeric {delta_numeric:,.2f}",
        delta_gap < DELTA_RELATIVE_TOLERANCE,
    )
    assert stage.check(
        "delta_spot equals N * DF_usd, the discounted USD leg",
        f"{NOTIONAL_USD * usd.discount_factor(tenor):,.6f}",
        f"{greeks.delta_spot:,.6f}",
        abs(greeks.delta_spot - NOTIONAL_USD * usd.discount_factor(tenor)) < 1e-6,
    )

    gamma_numeric = (
        value(quote.spot_rate + step) - 2 * at_market + value(quote.spot_rate - step)
    ) / (step * step)
    assert stage.check(
        "gamma_spot vanishes identically, because V is affine in S",
        "analytic exactly 0, numeric within float noise",
        f"analytic {greeks.gamma_spot:.1f}, numeric {gamma_numeric:.3e}",
        greeks.gamma_spot == 0.0 and abs(gamma_numeric) < 1e-6 * abs(greeks.delta_spot),
    )

    dv01_cop_analytic = (
        NOTIONAL_USD * strike * tenor * (1 + quote.r_cop) ** (-tenor - 1) * BASIS_POINT
    )
    dv01_usd_analytic = (
        -NOTIONAL_USD * quote.spot_rate * tenor * (1 + quote.r_usd) ** (-tenor - 1) * BASIS_POINT
    )
    cop_gap = abs(greeks.dv01_cop - dv01_cop_analytic) / abs(dv01_cop_analytic)
    usd_gap = abs(greeks.dv01_usd - dv01_usd_analytic) / abs(dv01_usd_analytic)
    assert stage.check(
        "dv01_cop: bump-and-reprice against +N K tau (1+r_cop)^(-tau-1)",
        f"relative gap < {DV01_RELATIVE_TOLERANCE:.0e}",
        f"{cop_gap:.3e}; bumped {greeks.dv01_cop:+,.2f}, analytic {dv01_cop_analytic:+,.2f}",
        cop_gap < DV01_RELATIVE_TOLERANCE,
    )
    assert stage.check(
        "dv01_usd: bump-and-reprice against -N S tau (1+r_usd)^(-tau-1)",
        f"relative gap < {DV01_RELATIVE_TOLERANCE:.0e}",
        f"{usd_gap:.3e}; bumped {greeks.dv01_usd:+,.2f}, analytic {dv01_usd_analytic:+,.2f}",
        usd_gap < DV01_RELATIVE_TOLERANCE,
    )
    assert stage.check(
        "A long forward is short the COP zero and long the USD zero",
        "dv01_cop > 0 > dv01_usd",
        f"{greeks.dv01_cop:+,.2f} / {greeks.dv01_usd:+,.2f}",
        greeks.dv01_cop > 0.0 > greeks.dv01_usd,
    )

    premium = NOTIONAL_USD * (quote.forward_rate - quote.spot_rate)
    decay_ratio = abs(greeks.theta) * FORWARD_TENOR_DAYS / premium
    assert stage.check(
        "theta decays the forward premium over the life of the trade",
        "negative for a long, and |theta| * 90d within 25% of the premium",
        f"theta {greeks.theta:+,.2f} COP/day, ratio {decay_ratio:.3f}, "
        f"premium {premium:+,.0f} COP",
        greeks.theta < 0.0 and 0.75 < decay_ratio < 1.25,
    )
    stage.note(
        "theta is not a flat 1/90th of the premium because the curve is re-read at the "
        "shorter tenor each day: the number is carry plus roll-down, which is what shows up "
        "in overnight P&L, so the check above is a band rather than an equality."
    )


# --------------------------------------------------------------------------- #
# The network arm: keep the cached snapshot honest
# --------------------------------------------------------------------------- #
@pytest.mark.network
def test_cached_fx_spot_still_matches_what_the_regulator_publishes(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
) -> None:
    """Re-fetch the TRM for the snapshot date and compare it against the fixture.

    The one input of this pipeline that *can* be re-verified from an open primary
    source is the FX spot. A cached number nobody ever re-checks stops being data
    and becomes folklore, so this arm exists to catch the fixture drifting away
    from datos.gov.co - not to price anything.
    """
    from tes_pricer.data.socrata_client import SocrataClient

    with SocrataClient(app_token=None, timeout=30.0, manifest_path=None) as client:
        frame = client.fetch_trm(SNAPSHOT_DATE, SNAPSHOT_DATE)

    published = float(frame.loc[frame["fecha"].dt.date == SNAPSHOT_DATE, "trm_cop_usd"].iloc[0])
    stage = evidence.stages[4]
    assert stage.check(
        "Cached FX spot still equals the TRM datos.gov.co publishes for that date",
        f"{published:,.2f} COP/USD (dataset 32sa-8pi3, fetched live)",
        f"{pipeline.spot:,.2f} COP/USD (fixture)",
        abs(published - pipeline.spot) < 0.005,
    )


def test_the_run_produced_a_complete_evidence_report(
    pipeline: PipelineRun,
    evidence: PipelineEvidence,
    tmp_path: Path,
) -> None:
    """The report is a deliverable of this test, so its rendering is asserted too."""
    del pipeline
    markdown_path, html_path = evidence.write(tmp_path)
    markdown = markdown_path.read_text(encoding="utf-8")
    page = html_path.read_text(encoding="utf-8")

    assert len(evidence.stages) == 6
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")
    for stage in evidence.stages:
        assert stage.title in markdown
        assert stage.title in page
        assert stage.checks, f"stage {stage.index} recorded no checks"
    assert SNAPSHOT_DATE.isoformat() in markdown
