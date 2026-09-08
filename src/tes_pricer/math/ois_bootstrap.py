"""Bootstrap of the COP OIS discount curve from IBR overnight index swaps.

IBR (Indicador Bancario de Referencia) is the COP overnight benchmark published
by Banco de la Republica. An IBR OIS exchanges a fixed rate against the daily
compounded overnight index:

.. math::

    \\prod_{i} \\left(1 + \\frac{r_i \\, d_i}{360}\\right) - 1

Two consequences the implementation must respect:

* The floating leg accrues **ACT/360 with daily compounding of the nominal
  rate**, so it is not interchangeable with an ACT/365 effective-rate curve.
* For a single-curve COP setup, discounting and forecasting share this curve;
  the bootstrap is therefore sequential and exact, one pillar per swap, with no
  optimisation involved.

Inputs are validated quotes. This module performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from numpy.typing import NDArray

from tes_pricer.math.day_count import DayCount


@dataclass(frozen=True, slots=True)
class OISQuote:
    """A single IBR OIS par quote."""

    tenor: str
    """Standard label, e.g. ``"1M"``, ``"3M"``, ``"1Y"``, ``"10Y"``."""
    maturity_date: date
    fixed_rate: float
    """Par fixed rate as a decimal."""
    fixed_frequency: int = 1
    day_count: DayCount = DayCount.ACT_360


@dataclass(frozen=True, slots=True)
class DiscountCurve:
    """A bootstrapped discount curve stored on its pillar dates."""

    reference_date: date
    pillar_times: NDArray[np.float64]
    """Year fractions from ``reference_date`` to each pillar, strictly increasing."""
    discount_factors: NDArray[np.float64]
    """Discount factors at ``pillar_times``, strictly decreasing and in ``(0, 1]``."""
    day_count: DayCount = DayCount.ACT_360

    def discount(self, tau: NDArray[np.float64]) -> NDArray[np.float64]:
        """Interpolate discount factors at arbitrary ``tau``.

        Interpolation is log-linear on the discount factors, which is equivalent
        to piecewise-constant instantaneous forwards and keeps the curve
        arbitrage-free between pillars.
        """
        raise NotImplementedError("Phase 7: discount curve interpolation")

    def zero_rate(self, tau: NDArray[np.float64]) -> NDArray[np.float64]:
        """Continuously compounded zero rates at ``tau``."""
        raise NotImplementedError("Phase 7: zero rates from discount factors")

    def forward_rate(
        self,
        start: NDArray[np.float64],
        end: NDArray[np.float64],
    ) -> NDArray[np.float64]:
        """Simple forward rate between ``start`` and ``end``, ACT/360."""
        raise NotImplementedError("Phase 7: forward rates from discount factors")


def bootstrap_ois_curve(
    quotes: list[OISQuote],
    reference_date: date,
    holidays: frozenset[date],
) -> DiscountCurve:
    """Bootstrap the COP OIS discount curve from par IBR OIS quotes.

    Quotes must be sorted by maturity and free of duplicate pillars.

    Raises:
        ValueError: If the quotes are unsorted, duplicated, or imply a
            non-monotone discount curve (an arbitrage in the input set).
    """
    raise NotImplementedError("Phase 7: OIS bootstrap")


def compound_overnight(
    overnight_rates: NDArray[np.float64],
    accrual_days: NDArray[np.float64],
) -> float:
    """Daily-compounded ACT/360 growth factor of a realised IBR fixing path."""
    raise NotImplementedError("Phase 7: overnight compounding")
