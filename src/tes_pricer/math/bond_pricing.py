"""Clean/dirty pricing, accrued interest and yield to maturity for fixed-rate TES.

Every function is a pure transformation of an already validated cash flow
schedule. Nothing here fetches a price: the caller supplies the schedule and the
market quote.

Sign and quoting conventions used throughout:

* Prices are expressed per 100 of face value, as quoted on the SEN/MEC screens.
* ``ytm`` is an **effective annual** rate (Colombian street convention), not a
  semi-annual bond-equivalent yield.
* ``dirty = clean + accrued``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from tes_pricer.math.day_count import DayCount


@dataclass(frozen=True, slots=True)
class BondTerms:
    """Static description of a fixed-rate bullet bond."""

    isin: str
    issue_date: date
    maturity_date: date
    coupon_rate: float
    """Annual coupon rate as a decimal, e.g. ``0.0725`` for 7.25%."""
    face_value: float = 100.0
    coupon_frequency: int = 1
    """Coupons per year. TES tasa fija pay annually."""
    day_count: DayCount = DayCount.ACT_365


def generate_cashflow_schedule(
    terms: BondTerms,
    settlement_date: date,
) -> pd.DataFrame:
    """Build the remaining cash flows of a bond as of ``settlement_date``.

    Returns:
        A DataFrame with columns ``payment_date``, ``year_fraction``,
        ``coupon``, ``principal`` and ``cashflow``, restricted to flows strictly
        after ``settlement_date``.
    """
    raise NotImplementedError("Phase 4: cash flow schedule generation")


def accrued_interest(terms: BondTerms, settlement_date: date) -> float:
    """Return accrued interest per 100 of face at ``settlement_date``."""
    raise NotImplementedError("Phase 4: accrued interest")


def dirty_price_from_ytm(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
) -> float:
    """Discount the remaining cash flows at a single effective annual ``ytm``."""
    raise NotImplementedError("Phase 4: dirty price from YTM")


def clean_price_from_ytm(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
) -> float:
    """Return ``dirty_price_from_ytm`` net of accrued interest."""
    raise NotImplementedError("Phase 4: clean price from YTM")


def yield_to_maturity(
    terms: BondTerms,
    settlement_date: date,
    clean_price: float,
    *,
    guess: float = 0.08,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
) -> float:
    """Invert :func:`clean_price_from_ytm` by Newton-Raphson with bisection fallback.

    Raises:
        ValueError: If the price is not attainable by any non-negative yield or
            the solver fails to converge within ``max_iterations``.
    """
    raise NotImplementedError("Phase 4: YTM solver")


def price_from_discount_factors(
    cashflows: NDArray[np.float64],
    discount_factors: NDArray[np.float64],
) -> float:
    """Present value of ``cashflows`` under an arbitrary discount curve.

    This is the curve-consistent pricing entry point used by the NSS calibration
    objective: it never assumes a flat yield.
    """
    raise NotImplementedError("Phase 4: curve-consistent pricing")
