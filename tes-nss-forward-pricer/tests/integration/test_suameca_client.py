"""Live SUAMECA calls.

Deselected by default. Run with:

    TES_PRICER_ALLOW_NETWORK=1 pytest -m integration

These assert on the *shape* of the response and on the endpoint contract, never
on specific market values: a test that pins a rate fails every morning.

They are also the canary for ``docs/suameca_reverse_engineering.md``. The
endpoints are undocumented SPA internals; when these start failing, the service
has been redeployed and the investigation must be re-run.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tes_pricer.data.suameca_client import (
    TES_NELSON_SIEGEL_BETAS_COP,
    TES_ZERO_COUPON_COP,
    SuamecaClient,
    SuamecaDataUnavailableError,
    SuamecaSeries,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture
def client(tmp_path: object) -> SuamecaClient:
    """A client archiving into the test's tmp_path, so a run leaves no residue."""
    return SuamecaClient(
        raw_cache_dir=tmp_path / "raw",  # type: ignore[operator]
        manifest_path=tmp_path / "manifest.json",  # type: ignore[operator]
    )


@pytest.fixture
def recent_window() -> tuple[date, date]:
    """A window wide enough to survive a publication lag or a long weekend."""
    today = date.today()
    return today - timedelta(days=45), today


def test_health_check_passes_against_live_endpoint(client: SuamecaClient) -> None:
    """The reverse-engineered endpoint still answers with a usable payload."""
    assert client.health_check() is True


def test_zero_coupon_curve_has_expected_shape(
    client: SuamecaClient, recent_window: tuple[date, date]
) -> None:
    """Three COP tenors come back with plausible percentage rates."""
    frame = client.fetch_zero_coupon_curve(*recent_window, denomination="pesos")

    assert set(frame["id_serie"]) == {s.series_id for s in TES_ZERO_COUPON_COP}
    assert (frame["unidad"] == "Porcentaje").all()
    # Bounds are deliberately loose: this checks units, not the market level.
    assert frame["valor"].between(0.0, 60.0).all()


def test_nelson_siegel_betas_are_published_as_decimals(
    client: SuamecaClient, recent_window: tuple[date, date]
) -> None:
    """The betas are decimals rounded to 2dp, not percentages.

    Guards the units trap in section 3 of the investigation: if B0 ever comes
    back near 12 rather than near 0.12, the upstream scale changed and every
    downstream comparison against our NSS fit is silently wrong.
    """
    frame = client.fetch_nelson_siegel_betas(*recent_window)

    assert set(frame["id_serie"]) == {s.series_id for s in TES_NELSON_SIEGEL_BETAS_COP}
    b0 = frame[frame["id_serie"] == 15278]["valor"]
    assert not b0.empty
    assert b0.between(-1.0, 1.0).all()


def test_unknown_series_id_raises_rather_than_returning_empty(
    client: SuamecaClient, recent_window: tuple[date, date]
) -> None:
    """The live sentinel record (HTTP 200, isSerie="NO") is rejected."""
    bogus = SuamecaSeries(99999999, "does not exist", "n/a")

    with pytest.raises(SuamecaDataUnavailableError):
        client.fetch_series(bogus, *recent_window)


def test_fetch_writes_raw_payload_and_manifest_entry(
    client: SuamecaClient, recent_window: tuple[date, date], tmp_path: object
) -> None:
    """A live fetch archives its payload and records the SHA-256."""
    from tes_pricer.data.validators import read_manifest, sha256_bytes

    client.fetch_series(TES_ZERO_COUPON_COP[2], *recent_window)

    manifest = read_manifest(tmp_path / "manifest.json")  # type: ignore[operator]
    assert manifest.data_sources
    record = manifest.data_sources[0]
    assert record.row_count > 0
    from pathlib import Path

    assert record.sha256 == sha256_bytes(Path(record.file_path).read_bytes())
