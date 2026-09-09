"""Live datos.gov.co (Socrata) calls. Deselected by default; see the SUAMECA module.

Run with ``pytest -m integration`` and ``TES_PRICER_ALLOW_NETWORK=1``.

The point of this tier is narrow and specific: **confirm the dataset identifiers
hardcoded in** :mod:`tes_pricer.data.socrata_client` **are still the right ones.**
An open data portal retires and renumbers assets without notice, and a stale
four-by-four fails in the worst possible way - HTTP 200 on metadata, no rows, and
a calibration that quietly runs on nothing. Parsing and validation are covered
offline in ``tests/unit/test_socrata_client.py``.

Two assertions below encode findings rather than requirements:

* ``ev8i-uzwt`` is an ``href`` stub with no rows. If that test ever fails, the
  portal has published real IBR data and ``fetch_ibr`` should be pointed at it
  with an :class:`~tes_pricer.data.socrata_client.IbrDatasetSpec`.
* ``mcec-87by`` mirrors ``32sa-8pi3`` exactly. If they diverge, the module
  docstring's claim that either can be used is no longer true.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tes_pricer.data.socrata_client import (
    IBR_DATASET_HREF_STUB,
    TRM_DATASET,
    TRM_DATASET_MIRROR,
    TRM_MAX_COP_USD,
    TRM_MIN_COP_USD,
    SocrataClient,
    SocrataDataUnavailableError,
)

pytestmark = [pytest.mark.integration, pytest.mark.network]

# The portal's first published TRM is 1991-12-02, so the history cannot shrink
# below this without something having gone badly wrong upstream.
MINIMUM_EXPECTED_TRM_ROWS = 8_000


@pytest.fixture
def client() -> SocrataClient:
    """An anonymous client that writes nothing: provenance is off in this tier."""
    return SocrataClient(app_token=None, timeout=30.0, manifest_path=None)


@pytest.fixture
def recent_window() -> tuple[date, date]:
    """A 45-day window ending today, wide enough to survive a long holiday bridge."""
    today = date.today()
    return today - timedelta(days=45), today


# --------------------------------------------------------------------------- #
# Dataset identifiers still resolve
# --------------------------------------------------------------------------- #


def test_trm_dataset_id_is_still_live(client: SocrataClient) -> None:
    """32sa-8pi3 must still be a tabular, recently refreshed SFC dataset."""
    report = client.dataset_health_check(TRM_DATASET.dataset_id)

    assert report["reachable"], report["error"]
    assert report["is_tabular"], report["error"]
    assert report["is_stale"] is False, (
        f"{TRM_DATASET.dataset_id} last refreshed {report['rows_updated_at']} "
        f"({report['days_since_update']} days ago); it may have been discontinued."
    )
    assert report["row_count"] is not None
    assert report["row_count"] > MINIMUM_EXPECTED_TRM_ROWS
    assert set(report["columns"]) >= {"valor", "vigenciadesde", "vigenciahasta"}


def test_trm_dataset_is_still_discoverable_by_name(client: SocrataClient) -> None:
    """The identifier must also be reachable from the catalog, not just by id.

    Searching is how the id was resolved in the first place; if the dataset stops
    surfacing under its own name, it has been renamed or unpublished.
    """
    hits = client.discover_datasets("Tasa Representativa del Mercado TRM", limit=10)

    assert TRM_DATASET.dataset_id in {hit["dataset_id"] for hit in hits}


def test_ibr_dataset_is_still_a_non_tabular_stub(client: SocrataClient) -> None:
    """ev8i-uzwt has no rows to query. This records that, and watches for change.

    A failure here is good news, not a regression: it means datos.gov.co has
    started publishing IBR as a real table, and ``fetch_ibr`` should be given an
    ``IbrDatasetSpec`` for it instead of raising.
    """
    report = client.dataset_health_check(IBR_DATASET_HREF_STUB.dataset_id)

    assert report["reachable"], report["error"]
    assert report["asset_type"] == "href"
    assert report["is_tabular"] is False
    assert report["row_count"] is None


def test_default_ibr_fetch_still_raises_against_the_live_portal(client: SocrataClient) -> None:
    """The documented failure must hold end to end, not just against a mock."""
    with pytest.raises(SocrataDataUnavailableError, match="suameca_client"):
        client.fetch_ibr(date(2026, 1, 1), date(2026, 1, 31))


# --------------------------------------------------------------------------- #
# The data itself
# --------------------------------------------------------------------------- #


def test_fetch_trm_returns_plausible_recent_data(
    client: SocrataClient, recent_window: tuple[date, date]
) -> None:
    """A live pull must satisfy the same schema and band the unit tests assert."""
    start, end = recent_window

    frame = client.fetch_trm(start, end)

    assert list(frame.columns) == ["fecha", "trm_cop_usd"]
    assert not frame.empty
    assert frame["fecha"].is_monotonic_increasing
    assert not frame["trm_cop_usd"].isna().any()
    assert frame["trm_cop_usd"].between(TRM_MIN_COP_USD, TRM_MAX_COP_USD).all(), (
        f"live TRM outside [{TRM_MIN_COP_USD}, {TRM_MAX_COP_USD}]: "
        f"min={frame['trm_cop_usd'].min()}, max={frame['trm_cop_usd'].max()}"
    )


def test_validity_expansion_covers_every_calendar_day(
    client: SocrataClient, recent_window: tuple[date, date]
) -> None:
    """Expanded, the TRM has no holes at all: every weekend day is covered too.

    This is the property the whole ``expand_validity`` design exists for, and it
    can only be checked against real validity windows.
    """
    start, end = recent_window

    frame = client.fetch_trm(start, end)

    covered = {stamp.date() for stamp in frame["fecha"]}
    first, last = min(covered), max(covered)
    expected = {first + timedelta(days=n) for n in range((last - first).days + 1)}
    assert covered == expected


def test_trm_mirror_agrees_with_the_primary_dataset(
    client: SocrataClient, recent_window: tuple[date, date]
) -> None:
    """mcec-87by is documented as a mirror of 32sa-8pi3; verify it still is."""
    start, end = recent_window

    primary = client.fetch_trm(start, end, expand_validity=False)
    mirror = client.fetch_trm(start, end, dataset=TRM_DATASET_MIRROR, expand_validity=False)

    assert primary.equals(mirror)


# --------------------------------------------------------------------------- #
# Transport contract
# --------------------------------------------------------------------------- #


def test_fetch_respects_page_size() -> None:
    """A single `fetch` must not exceed the requested page size."""
    with SocrataClient(page_size=7, manifest_path=None) as small:
        frame = small.fetch(TRM_DATASET, order="vigenciadesde DESC")

    assert len(frame) == 7


def test_fetch_all_pages_without_duplicates() -> None:
    """Paged retrieval must be duplicate-free under a stable ordering."""
    with SocrataClient(page_size=25, manifest_path=None) as small:
        frame = small.fetch_all(TRM_DATASET, max_rows=100)

    assert len(frame) == 100
    assert not frame["vigenciadesde"].duplicated().any()


def test_works_without_app_token(recent_window: tuple[date, date]) -> None:
    """The token is optional: anonymous access must still succeed, if throttled."""
    start, end = recent_window

    with SocrataClient(app_token=None, manifest_path=None) as anonymous:
        assert anonymous.has_app_token is False
        assert not anonymous.fetch_trm(start, end).empty
