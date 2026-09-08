"""Live SUAMECA calls.

Deselected by default. Run with:

    TES_PRICER_ALLOW_NETWORK=1 pytest -m integration

These tests assert on the *shape* of the response, never on specific values: a
test that pins a market number is a test that fails every morning.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_fetch_ibr_returns_expected_columns() -> None:
    """A live IBR pull must yield the columns the validator requires."""
    from tes_pricer.data.suameca_client import SuamecaClient

    raise NotImplementedError(f"Phase 2: exercise {SuamecaClient.__name__}")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_fetch_trm_covers_requested_range() -> None:
    """The returned window must cover the requested date range."""
    raise NotImplementedError("Phase 2: TRM range coverage")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_invalid_series_raises_suameca_error() -> None:
    """An unknown series identifier must surface as `SuamecaError`, not a KeyError."""
    raise NotImplementedError("Phase 2: SUAMECA error handling")
