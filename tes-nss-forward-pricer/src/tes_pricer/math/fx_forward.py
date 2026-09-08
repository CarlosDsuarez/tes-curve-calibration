"""USD/COP forward pricing under covered interest parity and Garman-Kohlhagen.

Quoting convention throughout: **COP per USD**, so USD is the *foreign* (base,
asset) currency and COP the *domestic* (quote, numeraire) currency. Under CIP,

.. math::

    F(T) = S \\, \\frac{D_{USD}(T)}{D_{COP}(T)}

so a COP rate above the USD rate implies ``F > S`` (forward points positive),
which is the normal USD/COP regime.

Garman-Kohlhagen is Black-Scholes with a continuous foreign dividend yield equal
to the foreign interest rate; it prices European FX options off the same two
discount curves. Both curves arrive already bootstrapped: this module never
fetches a rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray


class OptionType(StrEnum):
    """European option payoff direction, on the USD/COP (COP per USD) rate."""

    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True, slots=True)
class FXForwardQuote:
    """A single outright forward and its decomposition."""

    tenor: str
    spot: float
    forward: float
    forward_points: float
    """``forward - spot``, in COP. Market convention quotes these times 1000."""
    implied_cop_rate: float
    implied_usd_rate: float


def forward_rate_cip(
    spot: float,
    domestic_discount: NDArray[np.float64],
    foreign_discount: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Outright USD/COP forwards implied by two discount curves.

    Args:
        spot: Spot rate in COP per USD.
        domestic_discount: COP discount factors at each tenor.
        foreign_discount: USD discount factors at the same tenors.
    """
    raise NotImplementedError("Phase 8: CIP forward rate")


def forward_points(
    spot: float,
    forward: NDArray[np.float64],
    *,
    scale: float = 1.0,
) -> NDArray[np.float64]:
    """Forward points ``(forward - spot) * scale``.

    ``scale`` exists because desks quote USD/COP points in different multiples;
    keep it at ``1.0`` for raw COP.
    """
    raise NotImplementedError("Phase 8: forward points")


def implied_forward_rate_from_points(spot: float, points: float) -> float:
    """Rebuild an outright forward from a points quote."""
    raise NotImplementedError("Phase 8: outright from points")


def basis_spread(
    spot: float,
    market_forward: NDArray[np.float64],
    domestic_discount: NDArray[np.float64],
    foreign_discount: NDArray[np.float64],
    tau: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Cross-currency basis: the spread that reconciles CIP with the traded forward.

    A persistently non-zero basis is not a bug in the curves; it is the price of
    USD funding, and it is exactly the quantity this project is meant to measure.
    """
    raise NotImplementedError("Phase 8: cross-currency basis")


def garman_kohlhagen_price(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
    option_type: OptionType,
) -> float:
    """European FX option price in COP, per 1 USD of notional.

    Rates are continuously compounded; ``volatility`` is the annualised
    lognormal vol of the COP-per-USD rate.
    """
    raise NotImplementedError("Phase 8: Garman-Kohlhagen pricing")


def gk_d1_d2(
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    volatility: float,
) -> tuple[float, float]:
    """Return the Garman-Kohlhagen ``(d1, d2)`` terms shared by price and greeks."""
    raise NotImplementedError("Phase 8: Garman-Kohlhagen d1/d2")


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    tau: float,
    domestic_rate: float,
    foreign_rate: float,
    option_type: OptionType,
    *,
    tolerance: float = 1e-8,
    max_iterations: int = 100,
) -> float:
    """Invert :func:`garman_kohlhagen_price` for the lognormal volatility."""
    raise NotImplementedError("Phase 8: FX implied volatility")


def _standard_normal_cdf(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """Vectorised standard normal CDF used by the pricing formulas."""
    raise NotImplementedError("Phase 8: normal CDF helper")
