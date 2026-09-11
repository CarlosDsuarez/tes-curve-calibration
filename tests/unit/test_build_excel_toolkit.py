"""The parts of ``excel/build_excel_toolkit.py`` that do not need Excel.

The script drives a live Excel instance through ``xlwings`` to write the
workbook, which no CI runner has. What *can* be pinned without Excel is what it
writes: the tenor grid of the curve sheet, the ``xlwings.conf`` rows that tell
the add-in which interpreter and module to use, and the formulas each sheet
carries. Those are pure functions and are tested here by loading the script as
a module.
"""

from __future__ import annotations

import importlib.util
import sys
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "excel" / "build_excel_toolkit.py"


@pytest.fixture(scope="module")
def toolkit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_excel_toolkit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_curve_grid_runs_from_a_quarter_to_twenty_years_in_quarters(toolkit: ModuleType) -> None:
    tenors = toolkit.curve_tenors()
    assert len(tenors) == 80
    assert tenors[0] == pytest.approx(0.25)
    assert tenors[-1] == pytest.approx(20.0)
    assert all(b - a == pytest.approx(0.25) for a, b in pairwise(tenors))


def test_xlwings_conf_points_the_addin_at_the_bridge(toolkit: ModuleType, tmp_path: Path) -> None:
    interpreter = tmp_path / "venv" / "bin" / "python"
    rows = dict(toolkit.xlwings_conf_rows(interpreter, tmp_path))
    assert rows["UDF Modules"] == "tes_pricer.interface.excel_bridge"
    assert rows["PYTHONPATH"] == str(tmp_path / "src")
    assert rows["Interpreter_Mac"] == str(interpreter)
    assert rows["Interpreter_Win"] == str(interpreter)


def test_forward_sheet_formula_calls_the_udf_with_the_input_cells(toolkit: ModuleType) -> None:
    assert toolkit.FORWARD_FORMULA == "=FORWARD_USDCOP(B4,B5,B6)"


def test_curve_sheet_formula_references_its_own_row(toolkit: ModuleType) -> None:
    assert toolkit.curve_formula(2) == "=TASA_CERO_CUPON(A2)"
    assert toolkit.curve_formula(81) == "=TASA_CERO_CUPON(A81)"


def test_risk_sheet_formula_spills_the_four_figures(toolkit: ModuleType) -> None:
    assert toolkit.RISK_FORMULA == "=DV01_TES(B4,B5)"
