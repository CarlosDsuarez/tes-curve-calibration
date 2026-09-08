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
    """
    del config
    needs_quantlib = pytest.mark.skip(reason="QuantLib is not installed")
    needs_network = pytest.mark.skip(
        reason="network tests are opt-in; set TES_PRICER_ALLOW_NETWORK=1"
    )
    for item in items:
        if "benchmark" in item.keywords and not QUANTLIB_AVAILABLE:
            item.add_marker(needs_quantlib)
        if "integration" in item.keywords and not NETWORK_ENABLED:
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
