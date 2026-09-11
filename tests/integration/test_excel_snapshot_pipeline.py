"""The Excel path, end to end, off the archived 14 August 2026 market day.

``calibration_cache.build_snapshot`` replays the same stages as the pipeline
test, writes the JSON the UDFs read, and the three UDFs are then called the
way a cell would call them. Offline: everything comes from
``tests/fixtures/e2e_20260814``. Levels are asserted only where the pipeline
test asserts them too (the NSS fit); the forward and the risk numbers are
checked for sign and internal consistency, since both short curves are scenario
assumptions rather than sourced fixings.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tes_pricer.interface import excel_bridge
from tes_pricer.interface.calibration_cache import (
    DEFAULT_MARKET_SNAPSHOT_PATH,
    DEFAULT_PRICES_PATH,
    DEFAULT_REFERENCE_PATH,
    LATEST_FILENAME,
    CalibrationSnapshot,
    build_snapshot,
    load_snapshot,
    save_snapshot,
)
from tes_pricer.interface.excel_bridge import (
    DATA_DIR_ENV,
    DV01_TES,
    FORWARD_USDCOP,
    TASA_CERO_CUPON,
)

pytestmark = [pytest.mark.integration]

SNAPSHOT_DATE = date(2026, 8, 14)
NSS_RMSE_LIMIT_BPS = 20.0
"""Same in-sample limit as the pipeline test; see its module docstring."""


@pytest.fixture(scope="module")
def snapshot() -> CalibrationSnapshot:
    return build_snapshot(
        DEFAULT_PRICES_PATH, DEFAULT_MARKET_SNAPSHOT_PATH, DEFAULT_REFERENCE_PATH, SNAPSHOT_DATE
    )


@pytest.fixture
def excel_data_dir(
    snapshot: CalibrationSnapshot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    save_snapshot(snapshot, tmp_path)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    excel_bridge.clear_cache()
    return tmp_path


def test_build_replays_the_archived_day(snapshot: CalibrationSnapshot) -> None:
    assert snapshot.calibration_date == SNAPSHOT_DATE
    assert len(snapshot.bonds) == 16
    assert snapshot.diagnostics["n_bonds_used"] == 16
    assert snapshot.diagnostics["converged"] is True
    assert snapshot.diagnostics["rmse_bps"] < NSS_RMSE_LIMIT_BPS
    assert snapshot.fx_spot_cop_per_usd == pytest.approx(3127.51)
    assert snapshot.cop_curve.tenors_years.size == 5
    assert snapshot.usd_curve.tenors_years.size == 4
    assert snapshot.params.lambda1 > 0.0 and snapshot.params.lambda2 > 0.0


def test_the_written_file_reloads_identically(
    snapshot: CalibrationSnapshot, tmp_path: Path
) -> None:
    _, latest = save_snapshot(snapshot, tmp_path)
    reloaded = load_snapshot(latest)
    assert reloaded.params == snapshot.params
    assert reloaded.bonds == snapshot.bonds
    assert latest.name == LATEST_FILENAME


def test_zero_rates_sit_in_the_regime_of_the_day(excel_data_dir: Path) -> None:
    """A units guard, not a market view: COP benchmarks traded near 12% that day."""
    for tau in (1.0, 5.0, 10.0):
        assert 0.05 < TASA_CERO_CUPON(tau) < 0.20
    assert TASA_CERO_CUPON(10.0, "2026-08-14") == pytest.approx(TASA_CERO_CUPON(10.0))


def test_forward_points_carry_the_sign_of_the_differential(
    excel_data_dir: Path, snapshot: CalibrationSnapshot
) -> None:
    forward = FORWARD_USDCOP(100_000.0, "2026-11-12", "2026-08-14")
    assert forward > snapshot.fx_spot_cop_per_usd  # COP rates above USD rates: positive points


def test_dv01_of_a_benchmark_is_positive_and_consistent(excel_data_dir: Path) -> None:
    notional = 1_000_000_000.0
    ((dv01_cop, macaulay, modified, convexity),) = DV01_TES("TFIT16300632", notional)
    assert dv01_cop > 0.0
    assert 0.0 < macaulay < 6.0  # a 2032 bond seen from 2026
    assert modified == pytest.approx(macaulay, rel=1e-5)
    assert convexity > 0.0
    ((per_million, *_),) = DV01_TES("TFIT16300632", 1_000_000.0)
    assert dv01_cop == pytest.approx(per_million * 1000.0)
