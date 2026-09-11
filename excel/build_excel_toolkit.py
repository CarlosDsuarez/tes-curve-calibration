#!/usr/bin/env python3
"""Build ``excel/tes_toolkit.xlsm``, the workbook front-end of the three UDFs.

The workbook is generated, not hand-edited, so that it holds no business logic:
every number on it comes from a formula that calls
``tes_pricer.interface.excel_bridge`` through the xlwings add-in. Re-running
this script from scratch is the way to change the workbook.

What it writes:

``xlwings.conf``
    The settings sheet the add-in reads on open: which Python interpreter to
    launch, the ``PYTHONPATH`` entry for ``src/``, and the module holding the
    UDFs. Because it lives *in the workbook*, the file is portable across
    machines as long as the paths are rewritten (``--interpreter``) for each.
``Calculadora_Forward``
    Amount, maturity and an optional valuation date as inputs;
    ``=FORWARD_USDCOP(...)`` as the output, plus the COP amount as plain
    arithmetic.
``Curva_TES``
    Maturities from 0.25 to 20 years in steps of 0.25, each row calling
    ``=TASA_CERO_CUPON(...)``, and a native Excel chart of the curve.
``Riesgo_TES``
    A bond identifier and a notional, and ``=DV01_TES(...)`` spilling the four
    risk figures.

What it cannot do, and the guide explains: enable the xlwings add-in in the
user's Excel (``xlwings addin install``, once per machine), trust macros, and -
on Windows - press *Import Functions* in the xlwings ribbon so Excel learns the
three names. Those steps touch the user's Office installation and are not
automatable from here. And UDFs run on **Windows only**: on macOS this script
builds the workbook fine, but the formulas show ``#NAME?``.

Requires a local Excel. Usage::

    python excel/build_excel_toolkit.py --seed-cache
    python excel/build_excel_toolkit.py --interpreter "C:/venv/Scripts/python.exe"
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import xlwings as xw

from tes_pricer.interface import calibration_cache

logger = logging.getLogger("tes_pricer.excel")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "excel" / "tes_toolkit.xlsm"

UDF_MODULE = "tes_pricer.interface.excel_bridge"

SHEET_CONF = "xlwings.conf"
SHEET_FORWARD = "Calculadora_Forward"
SHEET_CURVE = "Curva_TES"
SHEET_RISK = "Riesgo_TES"

# Input cells of the forward calculator, referenced by FORWARD_FORMULA.
FORWARD_FORMULA = "=FORWARD_USDCOP(B4,B5,B6)"
FORWARD_COP_FORMULA = '=IF(ISNUMBER(B8),B4*B8,"")'
RISK_FORMULA = "=DV01_TES(B4,B5)"

CURVE_START_YEARS = 0.25
CURVE_END_YEARS = 20.0
CURVE_STEP_YEARS = 0.25

DEFAULT_FORWARD_AMOUNT_USD = 100_000.0
DEFAULT_RISK_BOND = "TFIT16300632"
DEFAULT_RISK_NOTIONAL_COP = 1_000_000_000.0


# --------------------------------------------------------------------------- #
# Pure helpers (unit tested)
# --------------------------------------------------------------------------- #
def curve_tenors(
    start: float = CURVE_START_YEARS,
    end: float = CURVE_END_YEARS,
    step: float = CURVE_STEP_YEARS,
) -> list[float]:
    """Maturities of the curve sheet: ``start`` to ``end`` inclusive, every ``step``."""
    count = round((end - start) / step) + 1
    return [round(start + i * step, 10) for i in range(count)]


def curve_formula(row: int) -> str:
    """The zero-rate formula for one row of the curve sheet."""
    return f"=TASA_CERO_CUPON(A{row})"


def xlwings_conf_rows(interpreter: Path, project_root: Path) -> list[list[str]]:
    """Key/value rows of the ``xlwings.conf`` sheet.

    Both interpreter keys are written with the same path so the sheet is
    complete whichever platform opens it; rewrite with ``--interpreter`` on a
    machine where the venv lives elsewhere.
    """
    return [
        ["Interpreter_Win", str(interpreter)],
        ["Interpreter_Mac", str(interpreter)],
        ["PYTHONPATH", str(project_root / "src")],
        ["UDF Modules", UDF_MODULE],
        ["Use UDF Server", "False"],
        ["Show Console", "False"],
        ["Add workbook to PYTHONPATH", "False"],
    ]


# --------------------------------------------------------------------------- #
# Sheets
# --------------------------------------------------------------------------- #
def _sheet(book: xw.Book, name: str) -> xw.Sheet:
    """A clean sheet called ``name`` (recreated if a template already has one)."""
    if name in book.sheet_names:
        book.sheets[name].delete()
    return book.sheets.add(name, after=book.sheets[-1])


def _title(sheet: xw.Sheet, cell: str, text: str) -> None:
    rng = sheet.range(cell)
    rng.value = text
    rng.font.bold = True
    rng.font.size = 14


def _label(sheet: xw.Sheet, cell: str, text: str) -> None:
    rng = sheet.range(cell)
    rng.value = text
    rng.font.bold = True


def write_conf_sheet(book: xw.Book, interpreter: Path) -> None:
    sheet = _sheet(book, SHEET_CONF)
    sheet.range("A1").value = xlwings_conf_rows(interpreter, PROJECT_ROOT)
    sheet.range("A:A").column_width = 28
    sheet.range("B:B").column_width = 70


def write_forward_sheet(book: xw.Book, snapshot: calibration_cache.CalibrationSnapshot) -> None:
    sheet = _sheet(book, SHEET_FORWARD)
    _title(sheet, "A1", "Calculadora de forward USD/COP (paridad cubierta de tasas)")
    sheet.range("A2").value = (
        f"Curvas y TRM de la calibración del {snapshot.calibration_date.isoformat()}; "
        "cambie los inputs en azul."
    )

    _label(sheet, "A4", "Monto (USD)")
    _label(sheet, "A5", "Fecha de vencimiento")
    _label(sheet, "A6", "Fecha de valoración (opcional; vacío = hoy)")
    _label(sheet, "A8", "Tasa forward teórica (COP por USD)")
    _label(sheet, "A9", "Monto a entregar (COP)")

    default_maturity = date(snapshot.calibration_date.year + 1, 6, 15)
    sheet.range("B4").value = DEFAULT_FORWARD_AMOUNT_USD
    sheet.range("B5").value = default_maturity
    sheet.range("B5").number_format = "yyyy-mm-dd"
    sheet.range("B6").number_format = "yyyy-mm-dd"
    for cell in ("B4", "B5", "B6"):
        sheet.range(cell).font.color = (0, 0, 192)

    sheet.range("B8").formula = FORWARD_FORMULA
    sheet.range("B8").number_format = "#,##0.00"
    sheet.range("B9").formula = FORWARD_COP_FORMULA
    sheet.range("B9").number_format = "#,##0"

    sheet.range("A11").value = (
        "Fórmula: =FORWARD_USDCOP(monto_usd; fecha_vencimiento; [fecha_valoracion]). "
        "La tasa no depende del monto; el monto en COP se calcula en la hoja."
    )
    sheet.range("A:A").column_width = 44
    sheet.range("B:B").column_width = 22


def write_curve_sheet(book: xw.Book) -> None:
    sheet = _sheet(book, SHEET_CURVE)
    _label(sheet, "A1", "Vencimiento (años)")
    _label(sheet, "B1", "Tasa cero cupón")

    tenors = curve_tenors()
    sheet.range("A2").options(transpose=True).value = tenors
    last_row = len(tenors) + 1
    sheet.range(f"B2:B{last_row}").formula = [[curve_formula(r)] for r in range(2, last_row + 1)]
    sheet.range(f"B2:B{last_row}").number_format = "0.000%"
    sheet.range("A:A").column_width = 20
    sheet.range("B:B").column_width = 18

    # A scatter-with-lines chart rather than Excel's "Line" type: the latter is
    # categorical and would plot the tenor column as a second series, while the
    # scatter uses column A as a numeric x axis - which is what a curve needs.
    chart = sheet.charts.add(left=220, top=10, width=560, height=340)
    chart.set_source_data(sheet.range(f"A1:B{last_row}"))
    chart.chart_type = "xy_scatter_lines_no_markers"
    chart.name = "CurvaCeroCupon"
    _try_set_chart_title(chart, "Curva cero cupón TES (NSS)")


def _try_set_chart_title(chart: xw.Chart, title: str) -> None:
    """Best effort across platforms; the chart is complete without a title."""
    _, native = chart.api
    try:
        native.HasTitle = True  # Windows COM
        native.ChartTitle.Text = title
        return
    except Exception:  # not on Windows
        pass
    try:
        native.has_title.set(True)  # macOS appscript
        native.chart_title.caption.set(title)
    except Exception as exc:
        logger.info("chart title not set on this platform: %s", exc)


def write_risk_sheet(book: xw.Book, snapshot: calibration_cache.CalibrationSnapshot) -> None:
    sheet = _sheet(book, SHEET_RISK)
    _title(sheet, "A1", "Riesgo de tasa de un TES (curva NSS calibrada)")
    sheet.range("A2").value = (
        f"Valorado en la fecha de calibración ({snapshot.calibration_date.isoformat()}). "
        "Identificadores válidos: " + ", ".join(sorted(snapshot.bonds))
    )
    _label(sheet, "A4", "Nemotécnico / ISIN")
    _label(sheet, "A5", "Nominal (COP)")
    sheet.range("B4").value = (
        DEFAULT_RISK_BOND if DEFAULT_RISK_BOND in snapshot.bonds else next(iter(snapshot.bonds))
    )
    sheet.range("B5").value = DEFAULT_RISK_NOTIONAL_COP
    sheet.range("B5").number_format = "#,##0"
    for cell in ("B4", "B5"):
        sheet.range(cell).font.color = (0, 0, 192)

    for col, header in zip(
        ("B", "C", "D", "E"),
        ("DV01 (COP)", "Duración Macaulay", "Duración modificada", "Convexidad"),
        strict=True,
    ):
        _label(sheet, f"{col}7", header)
    sheet.range("B8").formula = RISK_FORMULA
    sheet.range("B8").number_format = "#,##0.00"
    sheet.range("C8:E8").number_format = "0.0000"
    sheet.range("A10").value = (
        "Fórmula matricial: =DV01_TES(isin; notional). En Excel 365 se derrama sola; "
        "en versiones anteriores seleccione B8:E8 y confirme con Ctrl+Shift+Enter."
    )
    sheet.range("A:A").column_width = 24
    sheet.range("B:E").column_width = 20


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def ensure_snapshot(seed: bool) -> calibration_cache.CalibrationSnapshot:
    """The latest snapshot, building it from the archived day if asked and missing."""
    latest = calibration_cache.resolve_snapshot_path(calibration_cache.DEFAULT_OUTPUT_DIR, None)
    if not latest.is_file() and seed:
        logger.info("no snapshot found; calibrating the archived market day")
        snapshot = calibration_cache.build_snapshot(
            calibration_cache.DEFAULT_PRICES_PATH,
            calibration_cache.DEFAULT_MARKET_SNAPSHOT_PATH,
            calibration_cache.DEFAULT_REFERENCE_PATH,
            calibration_cache.DEFAULT_SETTLEMENT_DATE,
        )
        calibration_cache.save_snapshot(snapshot, calibration_cache.DEFAULT_OUTPUT_DIR)
    return calibration_cache.load_snapshot(latest)


def build_workbook(
    output: Path,
    interpreter: Path,
    snapshot: calibration_cache.CalibrationSnapshot,
    *,
    template: Path | None = None,
    visible: bool = False,
) -> Path:
    """Drive Excel to write every sheet and save the ``.xlsm``."""
    app = xw.App(visible=visible, add_book=False)
    try:
        app.display_alerts = False
        book = app.books.open(template) if template is not None else app.books.add()
        default_sheets = list(book.sheet_names) if template is None else []

        write_conf_sheet(book, interpreter)
        write_forward_sheet(book, snapshot)
        write_curve_sheet(book)
        write_risk_sheet(book, snapshot)

        for name in default_sheets:
            if name in book.sheet_names:
                book.sheets[name].delete()
        book.sheets[SHEET_FORWARD].activate()

        output = Path(output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        book.save(output)
        book.close()
    finally:
        app.quit()
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--template",
        type=Path,
        default=None,
        help="Optional .xlsm/.xlsx to start from instead of a blank workbook.",
    )
    parser.add_argument(
        "--interpreter",
        type=Path,
        default=Path(sys.executable),
        help="Python the add-in should launch (default: the one running this script).",
    )
    parser.add_argument(
        "--seed-cache",
        action="store_true",
        help="If data/processed/latest_calibration.json is missing, build it from the "
        "archived 2026-08-14 market day first.",
    )
    parser.add_argument("--visible", action="store_true", help="Show Excel while building.")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - min(args.verbose, 2) * 10, format="%(message)s")

    snapshot = ensure_snapshot(args.seed_cache)
    logger.info(
        "snapshot %s: %d bonds, RMSE %.2f bp",
        snapshot.calibration_date,
        len(snapshot.bonds),
        float(snapshot.diagnostics.get("rmse_bps", float("nan"))),
    )
    written = build_workbook(
        args.output, args.interpreter, snapshot, template=args.template, visible=args.visible
    )
    print(f"wrote {written}")
    print("Next: see docs/excel_setup_guide.md (add-in, macros, Import Functions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
