"""User-defined functions exposed to Excel through ``xlwings``.

The workbook holds no business logic: every cell formula resolves to one of the
three functions below, which delegate straight into ``tes_pricer.math``. That
keeps a single implementation under test and stops the spreadsheet from
drifting away from the library.

Nothing here calibrates. A worksheet formula fires once per cell per
recalculation, and dragging ``=TASA_CERO_CUPON(A2)`` over eighty rows would
otherwise re-fit the curve eighty times. The UDFs read the snapshot that
:mod:`tes_pricer.interface.calibration_cache` wrote to ``data/processed`` -
``latest_calibration.json``, or ``calibration_YYYY-MM-DD.json`` when a date is
given - and keep it in memory keyed by the file's modification time, so a
whole sheet costs one JSON parse and a rewritten file is picked up on the next
call without restarting Excel.

Conventions for the Excel side:

* Dates may arrive as ISO text (``"2026-12-15"``) or as ``datetime`` objects:
  ``xlwings`` converts a date-formatted cell to the latter, so both are accepted
  and no serial-number handling is needed.
* Rates cross the boundary as **decimals**, never percentages. Formatting is the
  worksheet's job.
* Optional text arguments are pinned to the ``str`` converter with
  ``@xw.arg``: a ``str | None`` annotation is not a registered ``xlwings``
  converter and would fail inside Excel, not at import.
* The array-returning function is declared with ``@xw.ret(expand="table")`` so
  it spills in Excel 365 and works as a CSE array formula elsewhere.
* Failures raise; ``xlwings`` surfaces the message in the cell rather than
  returning a silent zero.

Where the snapshot directory is: ``$TES_PRICER_DATA_DIR`` if set, otherwise
``<repository>/data/processed``.
"""

from __future__ import annotations

import math
import os
import warnings
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Final

import xlwings as xw

from tes_pricer.interface.calibration_cache import (
    DEFAULT_OUTPUT_DIR,
    CalibrationSnapshot,
    load_snapshot,
    resolve_snapshot_path,
)
from tes_pricer.math.fx_forward import CurveConsistencyWarning, price_fx_forward
from tes_pricer.math.greeks import bond_risk_from_curve
from tes_pricer.math.nss_model import nss_zero_rate

DATA_DIR_ENV: Final = "TES_PRICER_DATA_DIR"
"""Environment variable that overrides where the calibration snapshots are read from."""

_DATE_FORMAT_HINT: Final = "YYYY-MM-DD"


# --------------------------------------------------------------------------- #
# Snapshot access
# --------------------------------------------------------------------------- #
def snapshot_directory() -> Path:
    """Directory holding ``latest_calibration.json`` and the dated snapshots."""
    override = os.environ.get(DATA_DIR_ENV)
    return Path(override) if override else DEFAULT_OUTPUT_DIR


@lru_cache(maxsize=16)
def _cached_snapshot(path: str, mtime_ns: int) -> CalibrationSnapshot:
    """Parse ``path`` once per distinct modification time."""
    del mtime_ns  # part of the cache key only
    return load_snapshot(Path(path))


def clear_cache() -> None:
    """Drop every in-memory snapshot; the next call re-reads from disk."""
    _cached_snapshot.cache_clear()


def get_snapshot(calibration_date: date | None = None) -> CalibrationSnapshot:
    """The snapshot for ``calibration_date``, or the latest one when ``None``.

    Raises:
        FileNotFoundError: If no snapshot exists for that date; the message
            names the command that creates one.
    """
    path = resolve_snapshot_path(snapshot_directory(), calibration_date)
    if not path.is_file():
        # Let load_snapshot raise its explanatory error rather than stat().
        return load_snapshot(path)
    return _cached_snapshot(str(path), path.stat().st_mtime_ns)


# --------------------------------------------------------------------------- #
# Argument coercion
# --------------------------------------------------------------------------- #
def _coerce_date(value: object, name: str) -> date | None:
    """A ``date`` from ISO text or an Excel date; ``None`` for an empty argument."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{name} must be a date in {_DATE_FORMAT_HINT} format or an Excel date; "
                f"got {value!r}"
            ) from None
    raise ValueError(
        f"{name} must be a date in {_DATE_FORMAT_HINT} format or an Excel date; got {value!r}"
    )


def _positive_float(value: object, name: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number; got {value!r}") from None
    if math.isnan(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive number; got {value!r}")
    return number


def _today() -> date:
    """Today's date; a function so tests can pin it."""
    return date.today()


# --------------------------------------------------------------------------- #
# UDFs
# --------------------------------------------------------------------------- #
@xw.func
@xw.arg("vencimiento_anos", doc="Plazo en años, p. ej. 5 para cinco años.")
@xw.arg(
    "fecha_calibracion",
    convert=str,
    doc="Opcional. Fecha de calibración YYYY-MM-DD; vacío usa la última disponible.",
)
def TASA_CERO_CUPON(vencimiento_anos: float, fecha_calibracion: str | None = None) -> float:
    """Tasa spot cero cupón NSS (decimal) para un vencimiento en años.

    ``=TASA_CERO_CUPON(5)`` lee la última calibración disponible;
    ``=TASA_CERO_CUPON(5; "2026-08-14")`` lee ``calibration_2026-08-14.json``.
    La tasa está en la base de composición del modelo
    (:mod:`tes_pricer.math.nss_model`: continua) y es un decimal: 0.1123 es
    11.23%.
    """
    tau = _positive_float(vencimiento_anos, "vencimiento_anos")
    snapshot = get_snapshot(_coerce_date(fecha_calibracion, "fecha_calibracion"))
    return float(nss_zero_rate(tau, snapshot.params))


@xw.func
@xw.arg("monto_usd", doc="Monto nominal en USD; debe ser positivo.")
@xw.arg("fecha_vencimiento", convert=str, doc="Fecha de entrega, YYYY-MM-DD o celda de fecha.")
@xw.arg(
    "fecha_valoracion",
    convert=str,
    doc="Opcional. Fecha de valoración YYYY-MM-DD o celda de fecha; vacío usa hoy.",
)
def FORWARD_USDCOP(
    monto_usd: float,
    fecha_vencimiento: str,
    fecha_valoracion: str | None = None,
) -> float:
    """Tasa forward USD/COP teórica (COP por USD) por paridad cubierta de tasas.

    ``=FORWARD_USDCOP(100000; "2026-12-15")`` valora hoy sobre las curvas COP
    (IBR) y USD (SOFR) de la última calibración. El monto se valida pero no
    cambia la tasa: la paridad es lineal en el nominal, así que el monto en COP
    es ``monto_usd * tasa`` y se calcula en la hoja.

    Las curvas cortas están observadas en la fecha de calibración; valorar en
    otra fecha las lee sin desplazarlas, que es lo que un usuario de Excel
    espera de "la última curva", y por eso no se emite advertencia.
    """
    amount = _positive_float(monto_usd, "monto_usd")
    del amount  # validated; CIP is linear in the notional, the rate does not depend on it
    maturity = _coerce_date(fecha_vencimiento, "fecha_vencimiento")
    if maturity is None:
        raise ValueError("fecha_vencimiento is required")
    valuation = _coerce_date(fecha_valoracion, "fecha_valoracion") or _today()

    snapshot = get_snapshot()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CurveConsistencyWarning)
        quote = price_fx_forward(
            snapshot.fx_spot_cop_per_usd,
            snapshot.cop_curve,
            snapshot.usd_curve,
            valuation,
            maturity,
        )
    return float(quote.forward_rate)


@xw.func
@xw.arg("isin", doc="Nemotécnico del TES (p. ej. TFIT16300632) o su ISIN si está registrado.")
@xw.arg("notional", doc="Valor nominal de la posición en COP; debe ser positivo.")
@xw.ret(expand="table")
def DV01_TES(isin: str, notional: float) -> list[list[float]]:
    """``[DV01_COP, duración Macaulay, duración modificada, convexidad]`` del TES.

    Fórmula matricial (Ctrl+Shift+Enter) o matriz dinámica en Excel 365; se
    derrama en una fila de cuatro celdas. La curva es la de la última
    calibración, valorada en su propia fecha de calibración.

    DV01 es por bump-and-reprice: el bono se reprecia con la curva NSS
    desplazada +1pb en paralelo y la caída de precio, por 100 de nominal, se
    escala al ``notional``. Las duraciones y la convexidad son las de
    :func:`tes_pricer.math.greeks.bond_risk_from_curve`; bajo la composición
    continua de la curva la duración modificada coincide con la de Macaulay.
    """
    face = _positive_float(notional, "notional")
    snapshot = get_snapshot()
    terms = snapshot.resolve_bond(str(isin))
    risk = bond_risk_from_curve(terms, snapshot.calibration_date, snapshot.params)
    return [
        [
            risk.dv01 * face / terms.face_value,
            risk.macaulay_duration,
            risk.modified_duration,
            risk.convexity,
        ]
    ]
