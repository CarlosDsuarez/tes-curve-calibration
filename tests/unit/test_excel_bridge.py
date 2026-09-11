"""The three Excel UDFs, exercised without Excel.

``xlwings`` decorators only attach metadata, so the functions are plain Python
here and every number is checked against the ``tes_pricer.math`` call it must
delegate to. What is specific to the bridge - and therefore what these tests
are really about - is the cache: a formula dragged over fifty cells must read
the snapshot once, and must notice when the file on disk changes.
"""

from __future__ import annotations

import ast
import os
import warnings
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from tes_pricer.interface import excel_bridge
from tes_pricer.interface.calibration_cache import (
    LATEST_FILENAME,
    CalibrationSnapshot,
    save_snapshot,
)
from tes_pricer.interface.excel_bridge import (
    DATA_DIR_ENV,
    DV01_TES,
    FORWARD_USDCOP,
    TASA_CERO_CUPON,
)
from tes_pricer.math.fx_forward import price_fx_forward
from tes_pricer.math.greeks import bond_risk_from_curve
from tes_pricer.math.nss_model import nss_zero_rate

pytestmark = pytest.mark.unit

BOND_ID = "TEST00000001"
ALIAS = "COL17CT00001"


@pytest.fixture
def snapshot_dir(
    tmp_path: Path,
    sample_calibration_snapshot: CalibrationSnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A ``data/processed`` with the synthetic snapshot, wired in through the env var."""
    save_snapshot(sample_calibration_snapshot, tmp_path)
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    excel_bridge.clear_cache()
    return tmp_path


@pytest.fixture
def snapshot(sample_calibration_snapshot: CalibrationSnapshot) -> CalibrationSnapshot:
    return sample_calibration_snapshot


# --------------------------------------------------------------------------- #
# The surface
#
# Off Windows, ``xw.func`` / ``xw.arg`` / ``xw.ret`` are pass-through stubs
# (the real ones need pywin32), so the registration metadata cannot be read
# back at runtime here. The contract is pinned structurally instead, off the
# module's AST, the same way test_architecture.py pins the I/O boundary.
# --------------------------------------------------------------------------- #
def _decorated_udfs() -> dict[str, list[ast.expr]]:
    tree = ast.parse(Path(excel_bridge.__file__).read_text(encoding="utf-8"))
    return {
        node.name: node.decorator_list
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(_is_xw_call(d, "func") for d in node.decorator_list)
    }


def _is_xw_call(node: ast.expr, attribute: str) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "xw"
        and target.attr == attribute
    )


def _keyword(call: ast.expr, name: str) -> ast.expr | None:
    assert isinstance(call, ast.Call)
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def test_module_exposes_exactly_the_three_udfs() -> None:
    assert sorted(_decorated_udfs()) == ["DV01_TES", "FORWARD_USDCOP", "TASA_CERO_CUPON"]


def test_dv01_tes_spills_as_a_table() -> None:
    rets = [d for d in _decorated_udfs()["DV01_TES"] if _is_xw_call(d, "ret")]
    assert len(rets) == 1
    expand = _keyword(rets[0], "expand")
    assert isinstance(expand, ast.Constant) and expand.value == "table"


@pytest.mark.parametrize(
    ("udf", "arg"),
    [("TASA_CERO_CUPON", "fecha_calibracion"), ("FORWARD_USDCOP", "fecha_valoracion")],
)
def test_optional_string_arguments_pin_the_str_converter(udf: str, arg: str) -> None:
    """``str | None`` is not an xlwings converter; ``@xw.arg`` must pin ``str``."""
    pinned = [
        d
        for d in _decorated_udfs()[udf]
        if _is_xw_call(d, "arg")
        and isinstance(d, ast.Call)
        and isinstance(d.args[0], ast.Constant)
        and d.args[0].value == arg
    ]
    assert len(pinned) == 1
    convert = _keyword(pinned[0], "convert")
    assert isinstance(convert, ast.Name) and convert.id == "str"


# --------------------------------------------------------------------------- #
# TASA_CERO_CUPON
# --------------------------------------------------------------------------- #
def test_zero_rate_delegates_to_the_nss_curve(
    snapshot_dir: Path, snapshot: CalibrationSnapshot
) -> None:
    assert TASA_CERO_CUPON(5.0) == pytest.approx(nss_zero_rate(5.0, snapshot.params))


@pytest.mark.parametrize("when", ["2026-03-16", datetime(2026, 3, 16), date(2026, 3, 16)])
def test_zero_rate_accepts_the_calibration_date_as_text_or_date(
    snapshot_dir: Path, snapshot: CalibrationSnapshot, when: object
) -> None:
    assert TASA_CERO_CUPON(5.0, when) == pytest.approx(nss_zero_rate(5.0, snapshot.params))  # type: ignore[arg-type]


def test_zero_rate_for_an_uncalibrated_date_says_how_to_calibrate(snapshot_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="calibration_2025-01-01.json"):
        TASA_CERO_CUPON(5.0, "2025-01-01")


@pytest.mark.parametrize("tau", [0.0, -1.0, float("nan")])
def test_zero_rate_rejects_a_non_positive_maturity(snapshot_dir: Path, tau: float) -> None:
    with pytest.raises(ValueError, match="vencimiento"):
        TASA_CERO_CUPON(tau)


def test_zero_rate_reads_the_snapshot_once_for_many_cells(
    snapshot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_load = excel_bridge.load_snapshot

    def counting_load(path: Path) -> CalibrationSnapshot:
        nonlocal calls
        calls += 1
        return real_load(path)

    monkeypatch.setattr(excel_bridge, "load_snapshot", counting_load)
    for tau in np.arange(0.25, 12.75, 0.25):
        TASA_CERO_CUPON(float(tau))
    assert calls == 1


def test_zero_rate_picks_up_a_rewritten_snapshot(
    snapshot_dir: Path, snapshot: CalibrationSnapshot
) -> None:
    before = TASA_CERO_CUPON(5.0)
    steeper = replace(snapshot, params=replace(snapshot.params, beta0=snapshot.params.beta0 + 0.01))
    _, latest = save_snapshot(steeper, snapshot_dir)
    # Force a visibly newer mtime even on coarse filesystems.
    stamp = latest.stat().st_mtime + 5.0
    os.utime(latest, (stamp, stamp))
    assert TASA_CERO_CUPON(5.0) == pytest.approx(before + 0.01)


# --------------------------------------------------------------------------- #
# FORWARD_USDCOP
# --------------------------------------------------------------------------- #
def test_forward_delegates_to_covered_interest_parity(
    snapshot_dir: Path, snapshot: CalibrationSnapshot
) -> None:
    expected = price_fx_forward(
        snapshot.fx_spot_cop_per_usd,
        snapshot.cop_curve,
        snapshot.usd_curve,
        date(2026, 3, 16),
        date(2026, 9, 16),
    ).forward_rate
    assert FORWARD_USDCOP(100_000.0, "2026-09-16", "2026-03-16") == pytest.approx(expected)


def test_forward_rate_does_not_depend_on_the_amount(snapshot_dir: Path) -> None:
    small = FORWARD_USDCOP(1.0, "2026-09-16", "2026-03-16")
    large = FORWARD_USDCOP(1e9, "2026-09-16", "2026-03-16")
    assert small == pytest.approx(large)


def test_forward_defaults_the_valuation_date_to_today(
    snapshot_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(excel_bridge, "_today", lambda: date(2026, 4, 1))
    assert FORWARD_USDCOP(100_000.0, "2026-09-16") == pytest.approx(
        FORWARD_USDCOP(100_000.0, "2026-09-16", "2026-04-01")
    )


def test_forward_accepts_excel_dates(snapshot_dir: Path) -> None:
    as_text = FORWARD_USDCOP(100_000.0, "2026-09-16", "2026-03-16")
    as_dates = FORWARD_USDCOP(100_000.0, datetime(2026, 9, 16), datetime(2026, 3, 16))  # type: ignore[arg-type]
    assert as_dates == pytest.approx(as_text)


def test_forward_off_the_curve_date_is_quiet(snapshot_dir: Path) -> None:
    """A cell cannot show a warning; the stale-curve caveat lives in the docs instead."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        FORWARD_USDCOP(100_000.0, "2026-09-16", "2026-04-01")


@pytest.mark.parametrize("amount", [0.0, -5.0, float("nan")])
def test_forward_rejects_a_non_positive_amount(snapshot_dir: Path, amount: float) -> None:
    with pytest.raises(ValueError, match="monto_usd"):
        FORWARD_USDCOP(amount, "2026-09-16", "2026-03-16")


def test_forward_rejects_maturity_before_valuation(snapshot_dir: Path) -> None:
    with pytest.raises(ValueError, match="precedes"):
        FORWARD_USDCOP(100_000.0, "2026-01-01", "2026-03-16")


def test_forward_rejects_an_unparseable_date(snapshot_dir: Path) -> None:
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        FORWARD_USDCOP(100_000.0, "16/09/2026", "2026-03-16")


# --------------------------------------------------------------------------- #
# DV01_TES
# --------------------------------------------------------------------------- #
def test_dv01_tes_returns_one_row_scaled_to_the_notional(
    snapshot_dir: Path, snapshot: CalibrationSnapshot
) -> None:
    notional = 1_000_000.0
    risk = bond_risk_from_curve(snapshot.bonds[BOND_ID], snapshot.calibration_date, snapshot.params)
    result = DV01_TES(BOND_ID, notional)
    assert len(result) == 1 and len(result[0]) == 4
    dv01_cop, macaulay, modified, convexity = result[0]
    assert dv01_cop == pytest.approx(risk.dv01 * notional / 100.0)
    assert macaulay == pytest.approx(risk.macaulay_duration)
    assert modified == pytest.approx(risk.modified_duration)
    assert convexity == pytest.approx(risk.convexity)


def test_dv01_tes_accepts_an_alias_and_ignores_case(snapshot_dir: Path) -> None:
    assert DV01_TES(ALIAS, 100.0) == DV01_TES(f"  {BOND_ID.lower()} ", 100.0)


def test_dv01_tes_names_the_universe_for_an_unknown_bond(snapshot_dir: Path) -> None:
    with pytest.raises(KeyError, match=BOND_ID):
        DV01_TES("TFIT99999999", 100.0)


@pytest.mark.parametrize("notional", [0.0, -1.0, float("nan")])
def test_dv01_tes_rejects_a_non_positive_notional(snapshot_dir: Path, notional: float) -> None:
    with pytest.raises(ValueError, match="notional"):
        DV01_TES(BOND_ID, notional)


# --------------------------------------------------------------------------- #
# Where the snapshot comes from
# --------------------------------------------------------------------------- #
def test_missing_latest_snapshot_names_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    excel_bridge.clear_cache()
    with pytest.raises(FileNotFoundError, match=LATEST_FILENAME):
        TASA_CERO_CUPON(5.0)


def test_snapshot_directory_defaults_to_the_project_data_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    assert excel_bridge.snapshot_directory().parts[-2:] == ("data", "processed")
