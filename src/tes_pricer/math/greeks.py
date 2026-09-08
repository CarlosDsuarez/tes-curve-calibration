"""Risk sensitivities: bond DV01/duration/convexity and FX option greeks.

Two families live here on purpose, because they answer the same question in two
markets and share the finite-difference machinery:

* **Rates.** DV01 is reported per 100 of face for a one basis point parallel
  shift. Analytic duration is only valid for a flat-yield reparameterisation, so
  the curve-consistent variants bump the NSS parameters instead.
* **FX.** Garman-Kohlhagen greeks, with the usual market scalings noted per
  function (delta per 1 USD notional, vega per volatility point, theta per day).

All functions are pure. Curve and parameter objects are supplied by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from numpy.typing import NDArray

from tes_pricer.math.bond_pricing import BondTerms
from tes_pricer.math.fx_forward import OptionType
from tes_pricer.math.nss_model import NSSParams

BASIS_POINT = 1e-4


@dataclass(frozen=True, slots=True)
class BondRisk:
    """Rate sensitivities of a single bond position."""

    dv01: float
    """Price change per 100 of face for a 1bp parallel shift, sign-flipped so a
    long position reports a positive number."""
    macaulay_duration: float
    modified_duration: float
    convexity: float


@dataclass(frozen=True, slots=True)
class FXOptionGreeks:
    """Garman-Kohlhagen sensitivities, per 1 USD of notional."""

    price: float
    delta: float
    gamma: float
    vega: float
    """Per 1.00 of volatility; divide by 100 for a per-vol-point figure."""
    theta: float
    """Per year; divide by 365 for a per-calendar-day figure."""
    rho_domestic: float
    rho_foreign: float


def dv01_from_ytm(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
    *,
    bump: float = BASIS_POINT,
) -> float:
    """Central-difference DV01 under a flat-yield bump."""
    raise NotImplementedError("Phase 8: DV01 from YTM")


def dv01_from_curve(
    terms: BondTerms,
    settlement_date: date,
    params: NSSParams,
    *,
    bump: float = BASIS_POINT,
) -> float:
    """DV01 under a parallel shift of the whole NSS curve (``beta0`` bump).

    This is the number a curve-based book actually hedges on; the flat-yield
    variant above is kept for reconciliation against street quotes.
    """
    raise NotImplementedError("Phase 8: curve DV01")


def key_rate_durations(
    terms: BondTerms,
    settlement_date: date,
    params: NSSParams,
    key_tenors: NDArray[np.float64],
    *,
    bump: float = BASIS_POINT,
) -> NDArray[np.float64]:
    """Partial DV01s against bumps localised at each of ``key_tenors``."""
    raise NotImplementedError("Phase 8: key rate durations")


def bond_risk(
    terms: BondTerms,
    settlement_date: date,
    ytm: float,
) -> BondRisk:
    """Analytic Macaulay/modified duration, convexity and DV01 at a flat ``ytm``."""
    raise NotImplementedError("Phase 8: analytic bond risk")


def fx_option_greeks(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
    option_type: OptionType,
) -> FXOptionGreeks:
    """Analytic Garman-Kohlhagen greeks for a European USD/COP option."""
    raise NotImplementedError("Phase 8: FX option greeks")


def forward_delta(
    notional_usd: float,
    domestic_discount: float,
) -> float:
    """Spot-equivalent delta of an outright forward, in USD.

    A forward is not delta-one against spot: its spot sensitivity is discounted
    by the domestic (COP) discount factor to the settlement date.
    """
    raise NotImplementedError("Phase 8: forward delta")
