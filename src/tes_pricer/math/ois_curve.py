r"""Short-end discount curve interpolated in *rate* space on an effective-annual basis.

This module is the pragmatic sibling of :mod:`tes_pricer.math.ois_bootstrap`.
Where that module bootstraps an exact, arbitrage-free COP OIS curve out of par
IBR OIS swap quotes, this one takes a handful of *published* short-term index
fixings and turns them into a usable discount curve with no optimiser and no
swap conventions involved.

Interpolation convention
------------------------
Rates are interpolated linearly, and the discount factor is derived from the
interpolated rate:

.. math::

    r(\tau) = \mathrm{lerp}(\tau;\ \tau_i, r_i), \qquad
    DF(\tau) = \frac{1}{\left(1 + r(\tau)\right)^{\tau}}

Interpolating the **rates** rather than the discount factors is the market
standard, and the reason is the shape of the implied forwards. Linear
interpolation in DF space makes :math:`\log DF` piecewise *non*-linear with a
kink whose curvature flips sign at every pillar, so the instantaneous forward
:math:`f(\tau) = -\partial_\tau \log DF(\tau)` picks up discontinuities at the
pillars. Interpolating the rate keeps :math:`r` continuous and the forward
curve visibly smoother between pillars. (Log-linear interpolation *on* DFs is
the other defensible choice - it gives piecewise-constant forwards - and that
is exactly what :class:`~tes_pricer.math.ois_bootstrap.DiscountCurve` does once
a real bootstrap exists. The two modules deliberately differ, because they are
fed different things: fixings here, par swaps there.)

Compounding basis
-----------------
**Effective annual (E.A.), ACT/365** - the Colombian street basis, and the unit
Banco de la Republica publishes IBR in ("Tasa efectiva, base 365"). So
:math:`DF = (1 + r)^{-\tau}`, never :math:`e^{-r\tau}`. This differs on purpose
from :mod:`tes_pricer.math.nss_model`, which is continuously compounded; convert
with ``z = log(1 + r)`` before mixing a curve from this module with anything
built off NSS, and see the "Compounding convention" section of that module.

Extrapolation
-------------
Flat in rate space at both ends: below the shortest pillar the shortest rate is
held, above the longest pillar the longest rate is held. Linear extrapolation
is *not* offered. Extending the last slope of a short-end curve out past its
data is the standard way to manufacture negative long rates or discount factors
above 1, and the failure is silent - the curve keeps returning finite numbers
that are simply wrong. Flat extrapolation is also wrong, but it is wrong in a
bounded, obvious way, and it never changes sign.

Data availability - the COP term structure *is* public and free
--------------------------------------------------------------
This was checked rather than assumed, on 2026-09-08, because the design brief
for this module assumed only IBR overnight was publicly available and that
1M/3M would need a paid market-data subscription. **That assumption is wrong.**

Banco de la Republica publishes IBR at overnight, 1M, 3M, 6M and 12M, in both
nominal (base 360) and effective (base 365) form, on the same free,
unauthenticated SUAMECA endpoint :mod:`tes_pricer.data.suameca_client` already
uses (``consultaInformacionSerieXTipoDato?idSerie={id}&tipoDato=1``). Verified
series ids, all returning observations, all in "Tasa efectiva, base 365" -
which is the convention this module wants, so no conversion is needed:

=========  =========  =======  ==========================================
Tenor      Effective  Nominal  Observations verified on 2026-09-08
=========  =========  =======  ==========================================
Overnight  15324      241      4555, from 2008-01-02
1M         15325      242      3436, from 2012-08-01
3M         15326      243      3436, from 2012-08-01
6M         16561      16560    2509, from 2016-05-23
12M        16563      16562    1031, from 2022-06-13
=========  =========  =======  ==========================================

The single-pillar flat-curve path below is therefore a **fallback for a
degraded fetch, not a permanent data limitation**, and its warning says so.
What still needs paid data is the full OIS bootstrap: par IBR *swap* quotes are
not published by Banco de la Republica, which is why
:func:`tes_pricer.math.ois_bootstrap.bootstrap_ois_curve` remains a v2 item.

For USD the situation is genuinely worse and the flat approximation is the
realistic default. The New York Fed publishes overnight SOFR and the
30/90/180-day SOFR *Averages* free, but those are compounded **in arrears** -
realised backward-looking averages, not forward-looking term rates, so they are
not discount-curve pillars. The forward-looking CME Term SOFR fixings are
published under licence and are not freely redistributable. Pass them through
``additional_tenors`` if you hold a licence; otherwise expect the warning.

This module performs no I/O, in line with the package rule in
:mod:`tes_pricer.math`.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import date
from typing import Final

import numpy as np
from numpy.typing import NDArray

OVERNIGHT_TENOR_YEARS: Final = 1.0 / 365.0
"""One calendar day on an ACT/365 basis; the shortest pillar a fixing can carry."""

TENOR_1M_YEARS: Final = 1.0 / 12.0
TENOR_3M_YEARS: Final = 0.25
TENOR_6M_YEARS: Final = 0.5

MAX_PLAUSIBLE_RATE: Final = 1.0
"""Reject rates at or above 100% E.A. as a decimals-versus-percent mix-up.

SUAMECA returns IBR as ``11.985`` meaning 11.985%, so feeding a raw payload
straight in is a live failure mode rather than a hypothetical one. A COP or USD
short rate of 100% E.A. is not a curve this project needs to price, so trading
that unreachable case for a loud error on the common bug is worth it.
"""


class FlatCurveApproximationWarning(UserWarning):
    """A curve was built from a single pillar and carries no term structure.

    Its own class, rather than a bare :class:`UserWarning`, so that callers can
    filter or escalate exactly this condition (``warnings.simplefilter("error",
    FlatCurveApproximationWarning)``) without touching unrelated warnings.
    """


@dataclass(frozen=True, slots=True)
class ShortRateCurve:
    """A short-end curve stored as effective annual rates on their tenors.

    Attributes:
        tenors_years: Pillar tenors in years, strictly increasing and positive,
            e.g. ``[1/365, 1/12, 0.25, 0.5, 1.0]``.
        rates: Effective annual (ACT/365) rates as decimals - ``0.0985`` for
            9.85%, never ``9.85``.
        currency: Currency of the curve, e.g. ``"COP"`` or ``"USD"``.
        curve_date: The observation date the fixings belong to.
    """

    tenors_years: NDArray[np.float64]
    rates: NDArray[np.float64]
    currency: str
    curve_date: date

    def __post_init__(self) -> None:
        """Coerce the inputs to float64 arrays and reject unusable curves.

        Raises:
            ValueError: If the pillars are empty, mismatched in length, not
                strictly increasing, non-positive in tenor, non-finite, or
                priced at a rate that implies ``1 + r <= 0`` or a
                percent-versus-decimal mix-up.
        """
        tenors = np.asarray(self.tenors_years, dtype=np.float64).reshape(-1)
        rates = np.asarray(self.rates, dtype=np.float64).reshape(-1)
        object.__setattr__(self, "tenors_years", tenors)
        object.__setattr__(self, "rates", rates)

        if tenors.size == 0:
            raise ValueError("A ShortRateCurve needs at least one pillar; got none.")
        if tenors.size != rates.size:
            raise ValueError(
                f"tenors_years and rates must have the same length; "
                f"got {tenors.size} and {rates.size}."
            )
        if not np.all(np.isfinite(tenors)) or not np.all(np.isfinite(rates)):
            raise ValueError("tenors_years and rates must be finite.")
        if np.any(tenors <= 0.0):
            raise ValueError(f"Pillar tenors must be strictly positive; got {tenors.tolist()}.")
        if tenors.size > 1 and np.any(np.diff(tenors) <= 0.0):
            raise ValueError(
                f"Pillar tenors must be strictly increasing (no duplicates); got {tenors.tolist()}."
            )
        if np.any(rates <= -1.0):
            raise ValueError(
                f"Rates must satisfy 1 + r > 0 for (1 + r)^-tau to be defined; "
                f"got {rates.tolist()}."
            )
        if np.any(np.abs(rates) >= MAX_PLAUSIBLE_RATE):
            raise ValueError(
                f"Rates must be decimals, not percentages: 9.85% is 0.0985, not 9.85. "
                f"Got {rates.tolist()}."
            )
        if not self.currency:
            raise ValueError("currency must be a non-empty code such as 'COP' or 'USD'.")

    def rate(self, tau: float) -> float:
        """Return the interpolated effective annual rate at ``tau`` years.

        Linear between pillars, flat outside them. Exposed because the rate is
        the interpolated quantity - :meth:`discount_factor` is a function of
        this, not the other way round - so a caller checking the curve should
        be able to see the rate that produced a discount factor.

        Args:
            tau: Year fraction from :attr:`curve_date`, non-negative.

        Returns:
            The effective annual rate as a decimal.

        Raises:
            ValueError: If ``tau`` is negative or not finite.
        """
        tau = float(tau)
        if not np.isfinite(tau):
            raise ValueError(f"tau must be finite; got {tau}.")
        if tau < 0.0:
            raise ValueError(f"tau must be non-negative; got {tau}.")

        # np.interp already clamps to fp[0] / fp[-1] outside xp, which *is* flat
        # extrapolation. left/right are passed explicitly anyway: the behaviour
        # this module promises should not rest on a library default.
        return float(
            np.interp(
                tau,
                self.tenors_years,
                self.rates,
                left=float(self.rates[0]),
                right=float(self.rates[-1]),
            )
        )

    def discount_factor(self, tau: float) -> float:
        r"""Return :math:`DF(\tau) = (1 + r(\tau))^{-\tau}` on the E.A. basis.

        The rate is interpolated first and converted second (see the module
        docstring for why that order matters to the implied forwards), with
        flat extrapolation of the nearest observed rate outside the pillar
        range.

        Args:
            tau: Year fraction from :attr:`curve_date`, non-negative. ``0.0``
                returns ``1.0`` by construction.

        Returns:
            The discount factor, in ``(0, 1]`` for any non-negative rate.

        Raises:
            ValueError: If ``tau`` is negative or not finite.
        """
        return float((1.0 + self.rate(tau)) ** (-float(tau)))


def _assemble_pillars(
    fixed: dict[float, float | None],
    additional_tenors: dict[float, float] | None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Merge the named tenors with any extras into sorted pillar arrays.

    Args:
        fixed: Named tenor -> rate, where ``None`` means "not published today"
            and is dropped.
        additional_tenors: Extra tenor -> rate pillars, or ``None``.

    Returns:
        ``(tenors, rates)``, sorted by tenor.

    Raises:
        ValueError: If ``additional_tenors`` repeats a tenor already supplied.
    """
    pillars: dict[float, float] = {
        tenor: float(rate) for tenor, rate in fixed.items() if rate is not None
    }
    for raw_tenor, raw_rate in (additional_tenors or {}).items():
        tenor = float(raw_tenor)
        if tenor in pillars:
            raise ValueError(
                f"additional_tenors duplicates the tenor {tenor} years, which was "
                f"already supplied as a named argument."
            )
        pillars[tenor] = float(raw_rate)

    ordered = sorted(pillars.items())
    tenors = np.array([tenor for tenor, _ in ordered], dtype=np.float64)
    rates = np.array([rate for _, rate in ordered], dtype=np.float64)
    return tenors, rates


def _warn_if_flat(curve: ShortRateCurve, remedy: str) -> None:
    """Emit :class:`FlatCurveApproximationWarning` when the curve has one pillar.

    The warning is unconditional and not silenceable from inside this module: a
    flat curve prices every tenor off one number, so any term-structure effect
    it reports - a forward, a carry, a curve trade - is an artefact. That has to
    be visible to whoever consumes the curve, not buried in a docstring.

    ``stacklevel=3`` points the warning at the caller of the public builder
    rather than at this helper or at the builder itself.
    """
    if curve.tenors_years.size > 1:
        return
    warnings.warn(
        f"{curve.currency} short curve built from a single pillar at "
        f"{float(curve.tenors_years[0]):.6f} years "
        f"({float(curve.rates[0]):.4%} E.A.). This is a FLAT-RATE APPROXIMATION, "
        f"not a real curve: it has no term structure, every tenor discounts at the "
        f"same rate, and every forward rate implied from it is flat by construction. "
        f"{remedy}",
        FlatCurveApproximationWarning,
        stacklevel=3,
    )


def build_cop_short_curve(
    ibr_overnight: float,
    ibr_1m: float | None,
    ibr_3m: float | None,
    curve_date: date,
    *,
    additional_tenors: dict[float, float] | None = None,
) -> ShortRateCurve:
    """Build the COP short-end curve from published IBR fixings.

    ``ibr_1m`` and ``ibr_3m`` are optional so a degraded fetch still produces a
    usable object, but they are *not* expected to be missing: Banco de la
    Republica publishes both free of charge (SUAMECA effective-rate series 15325
    and 15326 - see the module docstring for the full table). A single-pillar
    curve therefore signals a data-layer problem, and warns loudly.

    Args:
        ibr_overnight: IBR overnight, effective annual, as a decimal.
        ibr_1m: IBR 1M, effective annual, or ``None`` if unavailable.
        ibr_3m: IBR 3M, effective annual, or ``None`` if unavailable.
        curve_date: Observation date of the fixings.
        additional_tenors: Further ``{tenor_in_years: rate}`` pillars, e.g.
            ``{0.5: 0.1228, 1.0: 0.1235}`` for the freely published 6M and 12M
            IBR (series 16561 and 16563).

    Returns:
        The assembled :class:`ShortRateCurve` in ``"COP"``.

    Warns:
        FlatCurveApproximationWarning: If only ``ibr_overnight`` was supplied,
            leaving a flat curve with no term structure.

    Raises:
        ValueError: If the pillars are invalid (see :class:`ShortRateCurve`) or
            ``additional_tenors`` duplicates a named tenor.
    """
    tenors, rates = _assemble_pillars(
        {
            OVERNIGHT_TENOR_YEARS: ibr_overnight,
            TENOR_1M_YEARS: ibr_1m,
            TENOR_3M_YEARS: ibr_3m,
        },
        additional_tenors,
    )
    curve = ShortRateCurve(tenors_years=tenors, rates=rates, currency="COP", curve_date=curve_date)
    _warn_if_flat(
        curve,
        "IBR at 1M/3M/6M/12M is published free by Banco de la Republica on the same "
        "SUAMECA endpoint as the overnight fixing (effective-rate series 15325, 15326, "
        "16561, 16563); if they are missing here, the fetch degraded rather than the "
        "data being unavailable.",
    )
    return curve


def build_usd_short_curve(
    sofr_overnight: float,
    curve_date: date,
    additional_tenors: dict[float, float] | None = None,
) -> ShortRateCurve:
    """Build the USD short-end curve from SOFR.

    Only the overnight fixing is taken as given because only the overnight
    fixing is reliably free: the New York Fed's 30/90/180-day SOFR Averages are
    compounded in arrears and are therefore realised averages rather than
    forward-looking term pillars, and the forward-looking CME Term SOFR rates
    are licensed. ``additional_tenors`` is the hook for a licensed Term SOFR
    feed, or for OIS-implied points from any other source.

    Args:
        sofr_overnight: SOFR overnight, effective annual, as a decimal.
        curve_date: Observation date of the fixing.
        additional_tenors: ``{tenor_in_years: rate}`` pillars to add, e.g.
            ``{0.25: 0.0431, 0.5: 0.0425}`` from licensed CME Term SOFR.

    Returns:
        The assembled :class:`ShortRateCurve` in ``"USD"``.

    Warns:
        FlatCurveApproximationWarning: If no ``additional_tenors`` were given,
            leaving a flat curve with no term structure.

    Raises:
        ValueError: If the pillars are invalid (see :class:`ShortRateCurve`) or
            ``additional_tenors`` duplicates the overnight tenor.
    """
    tenors, rates = _assemble_pillars(
        {OVERNIGHT_TENOR_YEARS: sofr_overnight},
        additional_tenors,
    )
    curve = ShortRateCurve(tenors_years=tenors, rates=rates, currency="USD", curve_date=curve_date)
    _warn_if_flat(
        curve,
        "Free public SOFR term pillars do not exist: the New York Fed's 30/90/180-day "
        "SOFR Averages are compounded in arrears, not forward-looking term rates, and "
        "CME Term SOFR is licensed. Pass licensed or OIS-implied pillars through "
        "additional_tenors to give this curve a term structure.",
    )
    return curve
