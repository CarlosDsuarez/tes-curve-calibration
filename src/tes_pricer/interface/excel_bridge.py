"""User-defined functions exposed to Excel through ``xlwings``.

The workbook holds no business logic: every cell formula resolves to one of the
functions below, which delegate straight into ``tes_pricer.math``. That keeps a
single implementation under test and stops the spreadsheet from drifting away
from the library.

Conventions for the Excel side:

* Dates arrive as ``datetime`` objects, not serial numbers; ``xlwings`` converts
  them, so no manual 1900/1904 epoch handling is needed.
* Rates cross the boundary as **decimals**, never percentages. Formatting is the
  worksheet's job.
* Array-returning functions are declared with ``@xw.ret(expand="table")`` so they
  spill correctly in modern Excel.
* Failures raise; ``xlwings`` surfaces the message in the cell rather than
  returning a silent zero.
"""

from __future__ import annotations

from datetime import datetime

import xlwings as xw


@xw.func
@xw.arg("settlement_date", doc="Settlement date.")
@xw.arg("maturity_date", doc="Bond maturity date.")
@xw.arg("coupon_rate", doc="Annual coupon rate as a decimal, e.g. 0.0725.")
@xw.arg("ytm", doc="Effective annual yield to maturity as a decimal.")
def TES_CLEAN_PRICE(
    settlement_date: datetime,
    maturity_date: datetime,
    coupon_rate: float,
    ytm: float,
) -> float:
    """Clean price per 100 of face of a fixed-rate TES."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
def TES_YTM(
    settlement_date: datetime,
    maturity_date: datetime,
    coupon_rate: float,
    clean_price: float,
) -> float:
    """Effective annual yield implied by a clean price."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
def TES_DV01(
    settlement_date: datetime,
    maturity_date: datetime,
    coupon_rate: float,
    ytm: float,
) -> float:
    """DV01 per 100 of face for a 1bp parallel shift."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
def NSS_ZERO_RATE(
    tau: float,
    beta0: float,
    beta1: float,
    beta2: float,
    beta3: float,
    lambda1: float,
    lambda2: float,
) -> float:
    """Continuously compounded NSS zero rate at maturity ``tau`` in years."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
@xw.ret(expand="table")
def NSS_CALIBRATE(settlement_date: datetime) -> list[list[object]]:
    """Calibrate the curve for a date and spill the parameters and diagnostics."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
def FX_FORWARD_CIP(
    spot: float,
    tau: float,
    cop_rate: float,
    usd_rate: float,
) -> float:
    """USD/COP outright forward (COP per USD) implied by covered interest parity."""
    raise NotImplementedError("Phase 9: Excel bridge")


@xw.func
def FX_FORWARD_POINTS(spot: float, forward: float) -> float:
    """Forward points, ``forward - spot``, in COP."""
    raise NotImplementedError("Phase 9: Excel bridge")


def main() -> None:
    """Entry point used by ``xlwings`` when the workbook calls ``RunPython``."""
    raise NotImplementedError("Phase 9: workbook macro entry point")
