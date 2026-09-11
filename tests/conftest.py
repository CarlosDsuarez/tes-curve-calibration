"""Shared fixtures and marker policy.

Deliberately imports nothing heavier than the standard library at module level,
so ``pytest --collect-only`` works on a bare checkout before the numerical
dependencies are installed.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import date
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _module_available(name: str) -> bool:
    """Return whether ``name`` can be imported without importing it."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


QUANTLIB_AVAILABLE = _module_available("QuantLib")
NETWORK_ENABLED = os.environ.get("TES_PRICER_ALLOW_NETWORK") == "1"


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip tiers whose prerequisites are missing, rather than failing them.

    The marker expression in ``addopts`` already deselects these by default; this
    hook is what keeps an explicit ``-m benchmark`` run honest on a machine
    without QuantLib, or ``-m integration`` without network opt-in.

    The network gate keys off ``network``, not ``integration``. The two are not
    the same thing: an integration test is one that runs several modules
    together, and the end-to-end pipeline does exactly that against an archived
    snapshot - offline and deterministically, which is what lets CI run it.
    Only the tests that actually reach a live endpoint carry ``network``, and
    only those are skipped without the opt-in.
    """
    del config
    needs_quantlib = pytest.mark.skip(reason="QuantLib is not installed")
    needs_network = pytest.mark.skip(
        reason="network tests are opt-in; set TES_PRICER_ALLOW_NETWORK=1"
    )
    for item in items:
        if "benchmark" in item.keywords and not QUANTLIB_AVAILABLE:
            item.add_marker(needs_quantlib)
        if "network" in item.keywords and not NETWORK_ENABLED:
            item.add_marker(needs_network)


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the repository root."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def math_package_dir(project_root: Path) -> Path:
    """Directory of the numerical core, used by the architecture tests."""
    return project_root / "src" / "tes_pricer" / "math"


@pytest.fixture(scope="session")
def settlement_date() -> date:
    """A fixed valuation date, so golden numbers never move with the clock."""
    return date(2026, 3, 16)


@pytest.fixture
def sample_bond_terms(settlement_date: date) -> dict[str, object]:
    """Terms of a synthetic annual-coupon bond used across the unit tests.

    Synthetic on purpose: unit tests must not depend on an archived market
    snapshot. Real instruments live in the benchmark tier.
    """
    del settlement_date
    return {
        "isin": "TEST00000001",
        "issue_date": date(2020, 3, 16),
        "maturity_date": date(2030, 3, 16),
        "coupon_rate": 0.07,
        "face_value": 100.0,
        "coupon_frequency": 1,
    }


@pytest.fixture
def sample_nss_params() -> dict[str, float]:
    """A plausible COP curve shape: ~9.5% long, upward sloping with one hump."""
    return {
        "beta0": 0.095,
        "beta1": -0.015,
        "beta2": 0.020,
        "beta3": -0.010,
        "lambda1": 1.5,
        "lambda2": 8.0,
    }


@pytest.fixture
def sample_calibration_snapshot(
    sample_bond_terms: dict[str, object],
    sample_nss_params: dict[str, float],
    settlement_date: date,
) -> object:
    """A complete, synthetic calibration snapshot for the interface-layer tests.

    Everything the Excel bridge reads from ``latest_calibration.json`` - the
    fitted NSS curve, both short-end curves, the FX spot and the bond universe -
    built from the shared synthetic fixtures so no test depends on an archived
    market day. Imports are local so collection stays dependency-light.
    """
    import numpy as np

    from tes_pricer.interface.calibration_cache import CalibrationSnapshot
    from tes_pricer.math.bond_pricing import BondTerms
    from tes_pricer.math.nss_model import NSSParams
    from tes_pricer.math.ois_curve import ShortRateCurve

    terms = BondTerms(**sample_bond_terms)  # type: ignore[arg-type]
    return CalibrationSnapshot(
        calibration_date=settlement_date,
        generated_at="2026-09-10T12:00:00+00:00",
        params=NSSParams(**sample_nss_params),
        diagnostics={
            "rmse_bps": 3.25,
            "max_abs_error_bps": 7.5,
            "n_bonds_used": 1,
            "converged": True,
            "n_starts_tried": 25,
            "n_starts_converged": 25,
            "warnings": [],
            "per_bond_errors_bps": {terms.isin: 0.5},
        },
        cop_curve=ShortRateCurve(
            tenors_years=np.array([1.0 / 365.0, 1.0 / 12.0, 0.25, 0.5, 1.0]),
            rates=np.array([0.0925, 0.0930, 0.0945, 0.0960, 0.0985]),
            currency="COP",
            curve_date=settlement_date,
        ),
        usd_curve=ShortRateCurve(
            tenors_years=np.array([1.0 / 365.0, 0.25, 0.5, 1.0]),
            rates=np.array([0.0410, 0.0405, 0.0398, 0.0390]),
            currency="USD",
            curve_date=settlement_date,
        ),
        fx_spot_cop_per_usd=4000.0,
        bonds={terms.isin: terms},
        aliases={"COL17CT00001": terms.isin},
        sources={"prices": "synthetic", "market_snapshot": "synthetic"},
    )
