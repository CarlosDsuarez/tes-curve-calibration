"""Offline tests for the datos.gov.co (Socrata) client.

Every HTTP interaction is mocked with ``responses``: this module never touches
the network and runs in the ``unit`` tier. Mocking at the HTTP layer rather than
stubbing ``sodapy`` keeps the SoQL that ``fetch_trm`` and ``fetch_ibr`` actually
build under test - several assertions below read it straight off the recorded
request URL.

The payloads reproduce rows captured from the live portal on 2026-09-07,
including the two responses that are not data: the HTTP 403 an ``href`` stub
returns, and the ``assetType='href'`` metadata record that predicts it.
"""

from __future__ import annotations

import json
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import responses

from tes_pricer.data.socrata_client import (
    DEFAULT_IBR_SPEC,
    IBR_COLUMNS,
    IBR_DATASET_HREF_STUB,
    TRM_COLUMNS,
    TRM_DATASET,
    IbrDatasetSpec,
    SocrataClient,
    SocrataDataset,
    SocrataDataUnavailableError,
    SocrataSchemaError,
    SocrataValidationWarning,
    canonical_tenor,
    colombian_business_days,
    colombian_holidays,
    find_business_day_gaps,
    validate_ibr,
    validate_trm,
)
from tes_pricer.data.validators import ValidationStatus, read_manifest, sha256_bytes

pytestmark = pytest.mark.unit

DOMAIN = "www.datos.gov.co"
TRM_URL = f"https://{DOMAIN}/resource/{TRM_DATASET.dataset_id}.json"
TRM_METADATA_URL = f"https://{DOMAIN}/api/views/{TRM_DATASET.dataset_id}.json"
IBR_STUB_METADATA_URL = f"https://{DOMAIN}/api/views/{IBR_DATASET_HREF_STUB.dataset_id}.json"

# A synthetic tabular IBR source. datos.gov.co has none (see the module docstring
# of socrata_client), so this spec is what a replacement would look like: long
# format, one row per date and tenor, rates in percentage points.
IBR_SOURCE = SocrataDataset("test-ibr1", "Synthetic tabular IBR source")
IBR_URL = f"https://{DOMAIN}/resource/{IBR_SOURCE.dataset_id}.json"
IBR_SPEC = IbrDatasetSpec(
    dataset=IBR_SOURCE,
    date_column="fecha",
    rate_column="tasa",
    tenor_column="plazo",
    tenor_values={"overnight": "OVERNIGHT", "1m": "1 MES", "3m": "3 MESES"},
    rate_is_percent=True,
    convention="nominal ACT/360",
)

# Exactly the shape the live portal returns: every value a string, timestamps
# floating (no offset), and a validity window that can span a weekend.
TRM_ROWS: list[dict[str, str]] = [
    {
        "valor": "3144.28",
        "unidad": "COP",
        "vigenciadesde": "2026-08-28T00:00:00.000",
        "vigenciahasta": "2026-08-28T00:00:00.000",
    },
    {
        "valor": "3202.79",
        "unidad": "COP",
        "vigenciadesde": "2026-08-29T00:00:00.000",
        "vigenciahasta": "2026-08-31T00:00:00.000",
    },
    {
        "valor": "3213.97",
        "unidad": "COP",
        "vigenciadesde": "2026-09-01T00:00:00.000",
        "vigenciahasta": "2026-09-01T00:00:00.000",
    },
]

TRM_METADATA: dict[str, Any] = {
    "id": TRM_DATASET.dataset_id,
    "name": "Tasa de Cambio Representativa del Mercado- TRM",
    "attribution": "Superintendencia Financiera de Colombia - SUPERFINANCIERA, Bogotá D.C.",
    "assetType": "dataset",
    "viewType": "tabular",
    "rowsUpdatedAt": 1_772_925_920,
    "columns": [
        {"fieldName": "valor", "dataTypeName": "number"},
        {"fieldName": "unidad", "dataTypeName": "text"},
        {"fieldName": "vigenciadesde", "dataTypeName": "calendar_date"},
        {"fieldName": "vigenciahasta", "dataTypeName": "calendar_date"},
    ],
}

# What the portal really answers for ev8i-uzwt: healthy metadata, no columns.
HREF_STUB_METADATA: dict[str, Any] = {
    "id": IBR_DATASET_HREF_STUB.dataset_id,
    "name": "IBR - Indicador Bancario de Referencia",
    "attribution": "Banco de la República, Bogotá D.C.",
    "assetType": "href",
    "viewType": "href",
    "displayType": "href",
    "rowsUpdatedAt": 1_770_661_028,
    "columns": [],
}

NON_TABULAR_403 = {"error": True, "message": "no row or column access to non-tabular tables"}


@pytest.fixture
def client(tmp_path: Path) -> SocrataClient:
    """A client writing its cache and manifest inside the test's tmp_path."""
    return SocrataClient(
        app_token=None,
        domain=DOMAIN,
        timeout=5.0,
        raw_cache_dir=tmp_path / "raw" / "socrata",
        manifest_path=tmp_path / "manifest.json",
    )


@pytest.fixture
def quiet_client() -> SocrataClient:
    """A client with provenance disabled, for tests that only care about shaping."""
    return SocrataClient(domain=DOMAIN, timeout=5.0, manifest_path=None)


def _query(call_index: int = 0) -> dict[str, list[str]]:
    """The SoQL parameters of a recorded request."""
    return parse_qs(urlparse(responses.calls[call_index].request.url).query)


def _ibr_row(day: str, rate: str, plazo: str = "OVERNIGHT") -> dict[str, str]:
    return {"fecha": f"{day}T00:00:00.000", "tasa": rate, "plazo": plazo}


# --------------------------------------------------------------------------- #
# (a) TRM: successful fetch and the validity-window contract
# --------------------------------------------------------------------------- #


@responses.activate
def test_fetch_trm_returns_declared_schema(quiet_client: SocrataClient) -> None:
    """A well-formed payload becomes exactly the ['fecha', 'trm_cop_usd'] frame."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    frame = quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))

    assert list(frame.columns) == list(TRM_COLUMNS)
    assert frame["fecha"].is_monotonic_increasing
    assert frame["trm_cop_usd"].dtype.kind == "f"
    assert frame.attrs["quote_convention"] == "COP per 1 USD"


@responses.activate
def test_fetch_trm_expands_the_validity_window(quiet_client: SocrataClient) -> None:
    """The Saturday fixing valid through Monday must land on Sunday and Monday too.

    This is the difference between a TRM series a pricer can index by settlement
    date and one with holes every weekend.
    """
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    frame = quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))

    assert [stamp.date() for stamp in frame["fecha"]] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 8, 30),
        date(2026, 8, 31),
        date(2026, 9, 1),
    ]
    # The 29 August print carries through Sunday and Monday unchanged.
    assert frame["trm_cop_usd"].tolist() == [3144.28, 3202.79, 3202.79, 3202.79, 3213.97]


@responses.activate
def test_fetch_trm_without_expansion_keeps_one_row_per_publication(
    quiet_client: SocrataClient,
) -> None:
    """`expand_validity=False` dates each row at vigenciadesde, Saturdays included."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    frame = quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1), expand_validity=False)

    assert [stamp.date() for stamp in frame["fecha"]] == [
        date(2026, 8, 28),
        date(2026, 8, 29),
        date(2026, 9, 1),
    ]


@responses.activate
def test_fetch_trm_clips_to_the_requested_window(quiet_client: SocrataClient) -> None:
    """Expansion must not leak dates outside [start_date, end_date]."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    frame = quiet_client.fetch_trm(date(2026, 8, 30), date(2026, 8, 31))

    assert [stamp.date() for stamp in frame["fecha"]] == [date(2026, 8, 30), date(2026, 8, 31)]


@responses.activate
def test_fetch_trm_selects_by_window_overlap(quiet_client: SocrataClient) -> None:
    """The `$where` must catch a fixing whose window opens before start_date.

    Filtering on `vigenciadesde >= start` alone would silently drop the fixing
    that actually applies on the first day requested.
    """
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    quiet_client.fetch_trm(date(2026, 8, 31), date(2026, 9, 1))

    where = _query()["$where"][0]
    assert "vigenciahasta >= '2026-08-31T00:00:00.000'" in where
    assert "vigenciadesde <= '2026-09-01T00:00:00.000'" in where
    assert _query()["$order"][0] == "vigenciadesde ASC"


# --------------------------------------------------------------------------- #
# (b) TRM: mandatory post-fetch validation
# --------------------------------------------------------------------------- #


@responses.activate
def test_implausible_trm_warns_and_keeps_the_row(quiet_client: SocrataClient) -> None:
    """A TRM outside [2000, 10000] warns explicitly and is NOT discarded."""
    rows = [
        {
            "valor": "42.00",
            "unidad": "COP",
            "vigenciadesde": "2026-08-28T00:00:00.000",
            "vigenciahasta": "2026-08-28T00:00:00.000",
        }
    ]
    responses.add(responses.GET, TRM_URL, json=rows, status=200)

    with pytest.warns(SocrataValidationWarning, match="outside the plausible band"):
        frame = quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 8, 28))

    assert frame["trm_cop_usd"].tolist() == [42.0]
    report = frame.attrs["validation_report"]
    assert report.status is ValidationStatus.PASSED
    assert any("42.00" in message for message in report.warnings)


@responses.activate
def test_plausible_trm_emits_no_warning(quiet_client: SocrataClient) -> None:
    """The happy path must be silent, or the warnings stop being read."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    with warnings.catch_warnings():
        warnings.simplefilter("error", SocrataValidationWarning)
        frame = quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))

    assert frame.attrs["validation_report"].warnings == ()


def test_trm_band_breach_names_the_offending_dates() -> None:
    """The warning has to identify which prints broke the band, not just how many."""
    frame = pd.DataFrame(
        {
            "fecha": pd.to_datetime(["2026-02-02", "2026-02-03"]),
            "trm_cop_usd": [4100.0, 12_500.0],
        }
    )

    report = validate_trm(frame, date(2026, 2, 2), date(2026, 2, 3))

    assert report.status is ValidationStatus.PASSED
    assert len(report.warnings) == 1
    assert "2026-02-03=12,500.00" in report.warnings[0]


def test_trm_validation_fails_on_a_missing_column() -> None:
    """A frame without the declared schema is an error, not a warning."""
    report = validate_trm(
        pd.DataFrame({"fecha": pd.to_datetime(["2026-02-02"])}),
        date(2026, 2, 2),
        date(2026, 2, 2),
    )

    assert report.status is ValidationStatus.FAILED
    assert "trm_cop_usd" in report.errors[0]


# --------------------------------------------------------------------------- #
# (c) Business-day gaps against the Colombian calendar
# --------------------------------------------------------------------------- #


def test_colombian_calendar_applies_the_ley_emiliani_shift() -> None:
    """Reyes Magos moves from Tue 6 Jan 2026 to Mon 12 Jan; the shifted day is the holiday."""
    january = colombian_holidays(date(2026, 1, 1), date(2026, 1, 31))

    assert date(2026, 1, 12) in january
    assert date(2026, 1, 6) not in january

    business = colombian_business_days(date(2026, 1, 5), date(2026, 1, 13))
    assert date(2026, 1, 12) not in business
    assert date(2026, 1, 6) in business


def test_weekends_are_never_business_days() -> None:
    """February 2026 carries no CO holiday, so the count is pure weekday arithmetic."""
    business = colombian_business_days(date(2026, 2, 2), date(2026, 2, 8))

    assert business == [date(2026, 2, day) for day in (2, 3, 4, 5, 6)]


def test_gap_of_exactly_five_business_days_is_tolerated() -> None:
    """The threshold is *more than* five, so five consecutive absences pass."""
    observed = {date(2026, 2, 2), date(2026, 2, 10)}  # missing 3, 4, 5, 6, 9

    assert find_business_day_gaps(observed, date(2026, 2, 2), date(2026, 2, 10)) == []


def test_gap_longer_than_five_business_days_is_reported() -> None:
    """Seven consecutive absences are a real hole in the series."""
    observed = {date(2026, 2, 2), date(2026, 2, 12)}  # missing 3, 4, 5, 6, 9, 10, 11

    gaps = find_business_day_gaps(observed, date(2026, 2, 2), date(2026, 2, 12))

    assert gaps == [(date(2026, 2, 3), date(2026, 2, 11), 7)]


def test_holidays_and_weekends_never_count_towards_a_gap() -> None:
    """Semana Santa 2026 closes Thu 2 and Fri 3 April; that must not read as a gap.

    Without the CO calendar this window looks like four consecutive missing days
    (2 and 3 April plus the weekend) sitting on top of any real absence.
    """
    observed = {
        date(2026, 3, 30),
        date(2026, 3, 31),
        date(2026, 4, 1),
        date(2026, 4, 6),
        date(2026, 4, 7),
        date(2026, 4, 8),
        date(2026, 4, 9),
        date(2026, 4, 10),
    }

    assert find_business_day_gaps(observed, date(2026, 3, 30), date(2026, 4, 10)) == []


@responses.activate
def test_fetch_trm_warns_on_a_long_gap(quiet_client: SocrataClient) -> None:
    """A fortnight carrying two prints must surface as an explicit gap warning."""
    rows = [
        {
            "valor": "4100.00",
            "unidad": "COP",
            "vigenciadesde": "2026-02-02T00:00:00.000",
            "vigenciahasta": "2026-02-02T00:00:00.000",
        },
        {
            "valor": "4150.00",
            "unidad": "COP",
            "vigenciadesde": "2026-02-12T00:00:00.000",
            "vigenciahasta": "2026-02-12T00:00:00.000",
        },
    ]
    responses.add(responses.GET, TRM_URL, json=rows, status=200)

    with pytest.warns(SocrataValidationWarning, match="7 consecutive Colombian business days"):
        frame = quiet_client.fetch_trm(date(2026, 2, 2), date(2026, 2, 12))

    assert len(frame) == 2


# --------------------------------------------------------------------------- #
# (d) IBR
# --------------------------------------------------------------------------- #


def test_default_ibr_fetch_names_the_suameca_alternative(quiet_client: SocrataClient) -> None:
    """The portal's IBR asset is an href stub, so the default call must say so.

    Returning an empty frame here would let a bootstrap run on nothing. The
    message has to carry the replacement series ids, or the failure is a dead end
    for whoever reads the traceback.
    """
    with pytest.raises(SocrataDataUnavailableError) as excinfo:
        quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 27))

    message = str(excinfo.value)
    assert "ev8i-uzwt" in message
    assert "href" in message
    assert "suameca_client" in message
    assert "15324" in message and "241" in message
    assert DEFAULT_IBR_SPEC.is_tabular is False


@responses.activate
def test_fetch_ibr_returns_declared_schema_as_decimals(quiet_client: SocrataClient) -> None:
    """A percentage source becomes a decimal frame, per the project convention."""
    rows = [_ibr_row("2026-02-02", "9.55"), _ibr_row("2026-02-03", "9.60")]
    responses.add(responses.GET, IBR_URL, json=rows, status=200)

    frame = quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 3), spec=IBR_SPEC)

    assert list(frame.columns) == list(IBR_COLUMNS)
    assert frame["ibr_tasa"].tolist() == [0.0955, 0.096]
    assert frame["tenor"].unique().tolist() == ["overnight"]
    assert frame.attrs["convention"] == "nominal ACT/360"


@responses.activate
def test_fetch_ibr_passes_a_decimal_source_through(quiet_client: SocrataClient) -> None:
    """`rate_is_percent=False` must not divide by 100 a second time."""
    spec = IbrDatasetSpec(
        dataset=IBR_SOURCE,
        date_column="fecha",
        rate_column="tasa",
        rate_is_percent=False,
        convention="efectiva anual base 365",
    )
    responses.add(responses.GET, IBR_URL, json=[_ibr_row("2026-02-02", "0.0955")], status=200)

    frame = quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 2), spec=spec)

    assert frame["ibr_tasa"].tolist() == [0.0955]
    assert frame.attrs["convention"] == "efectiva anual base 365"


@responses.activate
def test_fetch_ibr_filters_by_tenor_in_soql(quiet_client: SocrataClient) -> None:
    """The tenor must be pushed into `$where`, not filtered after the fact."""
    responses.add(
        responses.GET, IBR_URL, json=[_ibr_row("2026-02-02", "9.80", "3 MESES")], status=200
    )

    frame = quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 2), "3 meses", spec=IBR_SPEC)

    assert "plazo = '3 MESES'" in _query()["$where"][0]
    assert frame["tenor"].tolist() == ["3m"]


@responses.activate
def test_implausible_ibr_warns_and_keeps_the_row(quiet_client: SocrataClient) -> None:
    """A rate above 30% per annum warns explicitly and is NOT discarded."""
    responses.add(responses.GET, IBR_URL, json=[_ibr_row("2026-02-02", "45.0")], status=200)

    with pytest.warns(SocrataValidationWarning, match="outside the plausible band"):
        frame = quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 2), spec=IBR_SPEC)

    assert frame["ibr_tasa"].tolist() == [0.45]


@responses.activate
def test_negative_ibr_warns(quiet_client: SocrataClient) -> None:
    """The lower bound is strict: a zero or negative fixing is not plausible for COP."""
    responses.add(responses.GET, IBR_URL, json=[_ibr_row("2026-02-02", "-0.5")], status=200)

    with pytest.warns(SocrataValidationWarning, match="outside the plausible band"):
        quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 2), spec=IBR_SPEC)


def test_ibr_validation_flags_a_percentage_scale_mistake() -> None:
    """A series sitting near 9.5 rather than 0.095 is a unit bug, and is called out."""
    frame = pd.DataFrame(
        {
            "fecha": pd.to_datetime(["2026-02-02", "2026-02-03"]),
            "ibr_tasa": [9.55, 9.60],
            "tenor": "overnight",
        }
    )

    report = validate_ibr(frame, date(2026, 2, 2), date(2026, 2, 3))

    assert any("percentage points" in message for message in report.warnings)


def test_ibr_tenor_aliases_normalise() -> None:
    """Spanish and shorthand spellings must land on the same canonical label."""
    assert canonical_tenor("overnight") == "overnight"
    assert canonical_tenor(" O/N ") == "overnight"
    assert canonical_tenor("1 Mes") == "1m"
    assert canonical_tenor("3 meses") == "3m"
    assert canonical_tenor("1y") == "12m"


def test_unknown_ibr_tenor_is_rejected(quiet_client: SocrataClient) -> None:
    """Guessing a tenor would put a three-month rate in an overnight column."""
    with pytest.raises(ValueError, match="Unknown IBR tenor"):
        quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 3), "18m", spec=IBR_SPEC)


def test_tenor_absent_from_the_spec_is_rejected(quiet_client: SocrataClient) -> None:
    """A recognised tenor the source cannot serve must fail before the request."""
    with pytest.raises(ValueError, match="no source value for tenor"):
        quiet_client.fetch_ibr(date(2026, 2, 2), date(2026, 2, 3), "6m", spec=IBR_SPEC)


# --------------------------------------------------------------------------- #
# (e) Failure modes
# --------------------------------------------------------------------------- #


def test_inverted_window_is_rejected_before_any_request(quiet_client: SocrataClient) -> None:
    """An inverted window is a caller bug; it must not become a SoQL query."""
    with pytest.raises(ValueError, match="is after end_date"):
        quiet_client.fetch_trm(date(2026, 9, 1), date(2026, 8, 1))


@responses.activate
def test_empty_window_raises_rather_than_returning_an_empty_frame(
    quiet_client: SocrataClient,
) -> None:
    """An empty frame would be indistinguishable from a market with no prints."""
    responses.add(responses.GET, TRM_URL, json=[], status=200)

    with pytest.raises(SocrataDataUnavailableError, match="returned no rows"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))


@responses.activate
def test_renamed_columns_raise_a_schema_error(quiet_client: SocrataClient) -> None:
    """If the portal renames vigenciadesde, fail loudly instead of yielding NaNs."""
    responses.add(
        responses.GET,
        TRM_URL,
        json=[{"valor": "4100.00", "fecha_inicio": "2026-08-28T00:00:00.000"}],
        status=200,
    )

    with pytest.raises(SocrataSchemaError, match="missing column"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 8, 28))


@responses.activate
def test_non_tabular_403_is_explained_not_retried(quiet_client: SocrataClient) -> None:
    """The href stub's 403 is not a permission problem and no token fixes it."""
    responses.add(responses.GET, TRM_URL, json=NON_TABULAR_403, status=403)

    with pytest.raises(SocrataDataUnavailableError, match="not a table"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))


@responses.activate
def test_missing_dataset_404_points_back_at_discovery(quiet_client: SocrataClient) -> None:
    """A retired four-by-four must send the reader to discover_datasets()."""
    responses.add(responses.GET, TRM_URL, json={"error": True}, status=404)

    with pytest.raises(SocrataDataUnavailableError, match="discover_datasets"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))


@responses.activate
def test_rate_limit_mentions_the_app_token_when_anonymous(quiet_client: SocrataClient) -> None:
    """A 429 without a token should say the token is what raises the limit."""
    responses.add(responses.GET, TRM_URL, json={"error": True}, status=429)

    with pytest.raises(SocrataDataUnavailableError, match="SOCRATA_APP_TOKEN"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))


@responses.activate
def test_html_body_is_reported_as_an_unusable_response(quiet_client: SocrataClient) -> None:
    """A proxy or captive-portal page must never be mistaken for data."""
    responses.add(
        responses.GET,
        TRM_URL,
        body="<!doctype html><html><body>Proxy</body></html>",
        status=200,
        content_type="text/html",
    )

    with pytest.raises(SocrataSchemaError, match="unusable response"):
        quiet_client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))


def test_page_size_must_be_within_the_soda_limits() -> None:
    """SODA caps `$limit` at 50000; asking for more silently truncates."""
    with pytest.raises(ValueError, match="must not exceed"):
        SocrataClient(page_size=60_000)
    with pytest.raises(ValueError, match="at least 1"):
        SocrataClient(page_size=0)


# --------------------------------------------------------------------------- #
# (f) Paging
# --------------------------------------------------------------------------- #


@responses.activate
def test_fetch_trm_pages_until_a_short_page() -> None:
    """A full page means there may be more; only a short page ends the pull."""
    small = SocrataClient(domain=DOMAIN, page_size=2, manifest_path=None)
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS[:2], status=200)
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS[2:], status=200)

    frame = small.fetch_trm(date(2026, 8, 28), date(2026, 9, 1), expand_validity=False)

    assert len(responses.calls) == 2
    assert _query(1)["$offset"] == ["2"]
    assert len(frame) == 3
    assert not frame["fecha"].duplicated().any()


@responses.activate
def test_fetch_all_orders_by_row_id_when_unordered(quiet_client: SocrataClient) -> None:
    """Paging without a stable order can skip or repeat rows, so `:id` is forced."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    quiet_client.fetch_all(TRM_DATASET, max_rows=3)

    assert _query()["$order"] == [":id"]


@responses.activate
def test_fetch_all_honours_max_rows(quiet_client: SocrataClient) -> None:
    """`max_rows` must cap the `$limit`, not just trim the result afterwards."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS[:2], status=200)

    frame = quiet_client.fetch_all(TRM_DATASET, max_rows=2)

    assert _query()["$limit"] == ["2"]
    assert len(frame) == 2
    assert len(responses.calls) == 1


# --------------------------------------------------------------------------- #
# (g) Health check
# --------------------------------------------------------------------------- #


@responses.activate
def test_health_check_reports_a_live_tabular_dataset(quiet_client: SocrataClient) -> None:
    """The happy path carries the row count and the columns actually present."""
    fresh = dict(TRM_METADATA, rowsUpdatedAt=int(datetime.now(UTC).timestamp()))
    responses.add(responses.GET, TRM_METADATA_URL, json=fresh, status=200)
    responses.add(responses.GET, TRM_URL, json=[{"n": "8341"}], status=200)

    report = quiet_client.dataset_health_check(TRM_DATASET.dataset_id)

    assert report["reachable"] is True
    assert report["is_tabular"] is True
    assert report["row_count"] == 8341
    assert report["is_stale"] is False
    assert report["columns"] == ["valor", "unidad", "vigenciadesde", "vigenciahasta"]
    assert report["error"] is None
    assert _query(1)["$select"] == ["count(1) as n"]


@responses.activate
def test_health_check_flags_the_href_stub_without_querying_it(
    quiet_client: SocrataClient,
) -> None:
    """An href asset must be caught from metadata, before a doomed SoQL query."""
    responses.add(responses.GET, IBR_STUB_METADATA_URL, json=HREF_STUB_METADATA, status=200)

    report = quiet_client.dataset_health_check(IBR_DATASET_HREF_STUB.dataset_id)

    assert report["reachable"] is True
    assert report["is_tabular"] is False
    assert report["asset_type"] == "href"
    assert report["row_count"] is None
    assert "not a queryable table" in report["error"]
    assert len(responses.calls) == 1


@responses.activate
def test_health_check_flags_a_discontinued_dataset(quiet_client: SocrataClient) -> None:
    """A dataset untouched for two years is the quiet way an identifier goes bad."""
    two_years_ago = datetime.now(UTC) - timedelta(days=730)
    stale = dict(TRM_METADATA, rowsUpdatedAt=int(two_years_ago.timestamp()))
    responses.add(responses.GET, TRM_METADATA_URL, json=stale, status=200)
    responses.add(responses.GET, TRM_URL, json=[{"n": "8341"}], status=200)

    report = quiet_client.dataset_health_check(TRM_DATASET.dataset_id)

    assert report["is_stale"] is True
    assert report["days_since_update"] >= 729
    assert report["rows_updated_at"].startswith(two_years_ago.strftime("%Y-%m-%d"))


@responses.activate
def test_health_check_never_raises(quiet_client: SocrataClient) -> None:
    """Callers use it to decide whether to start a run, so it reports, never throws."""
    responses.add(responses.GET, TRM_METADATA_URL, json={"error": True}, status=500)

    report = quiet_client.dataset_health_check(TRM_DATASET.dataset_id)

    assert report["reachable"] is False
    assert report["error"]
    assert report["row_count"] is None


# --------------------------------------------------------------------------- #
# (h) Discovery
# --------------------------------------------------------------------------- #


@responses.activate
def test_discover_datasets_normalises_catalog_hits(quiet_client: SocrataClient) -> None:
    """Identifiers are re-resolved through the catalog rather than trusted."""
    catalog_payload = {
        "results": [
            {
                "resource": {
                    "id": "32sa-8pi3",
                    "name": "Tasa de Cambio Representativa del Mercado- TRM",
                    "type": "dataset",
                    "attribution": "Superintendencia Financiera de Colombia",
                    "updatedAt": "2026-09-04T23:05:20.000Z",
                    "description": "TRM diaria.",
                }
            }
        ]
    }
    responses.add(
        responses.GET,
        "https://api.us.socrata.com/api/catalog/v1",
        json=catalog_payload,
        status=200,
    )

    hits = quiet_client.discover_datasets("Tasa Representativa del Mercado", only="dataset")

    assert hits == [
        {
            "dataset_id": "32sa-8pi3",
            "name": "Tasa de Cambio Representativa del Mercado- TRM",
            "type": "dataset",
            "attribution": "Superintendencia Financiera de Colombia",
            "updated_at": "2026-09-04T23:05:20.000Z",
            "description": "TRM diaria.",
        }
    ]
    query = _query()
    assert query["domains"] == [DOMAIN]
    assert query["only"] == ["dataset"]


# --------------------------------------------------------------------------- #
# (i) Provenance
# --------------------------------------------------------------------------- #


@responses.activate
def test_raw_payload_is_archived_and_registered(client: SocrataClient, tmp_path: Path) -> None:
    """The raw rows hit disk before parsing, and the manifest records their hash."""
    responses.add(responses.GET, TRM_URL, json=TRM_ROWS, status=200)

    client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))

    archived = list((tmp_path / "raw" / "socrata").glob("*.json"))
    assert len(archived) == 1
    assert archived[0].name == f"2026-09-01_trm-{TRM_DATASET.dataset_id}.json"
    assert json.loads(archived[0].read_text(encoding="utf-8")) == TRM_ROWS

    manifest = read_manifest(tmp_path / "manifest.json")
    assert len(manifest.data_sources) == 1
    record = manifest.data_sources[0]
    assert record.endpoint_type.value == "Socrata"
    assert record.url.endswith(f"/resource/{TRM_DATASET.dataset_id}.json")
    assert record.row_count == len(TRM_ROWS)
    assert record.sha256 == sha256_bytes(archived[0].read_bytes())
    assert (record.date_range.start, record.date_range.end) == (
        date(2026, 8, 28),
        date(2026, 9, 1),
    )


@responses.activate
def test_archive_happens_before_the_schema_check(client: SocrataClient, tmp_path: Path) -> None:
    """A schema failure must leave the evidence on disk, not force a re-fetch."""
    responses.add(responses.GET, TRM_URL, json=[{"valor": "4100.00"}], status=200)

    with pytest.raises(SocrataSchemaError):
        client.fetch_trm(date(2026, 8, 28), date(2026, 9, 1))

    assert list((tmp_path / "raw" / "socrata").glob("*.json"))


def test_save_raw_returns_the_digest_it_records(client: SocrataClient, tmp_path: Path) -> None:
    """The digest in the manifest must be the digest of the bytes written."""
    frame = pd.DataFrame(
        {
            "fecha": pd.to_datetime(["2026-08-28", "2026-08-29"]),
            "trm_cop_usd": [3144.28, 3202.79],
        }
    )
    destination = tmp_path / "processed" / "trm.csv"

    digest = client.save_raw(frame, destination)

    assert digest == sha256_bytes(destination.read_bytes())
    record = read_manifest(tmp_path / "manifest.json").data_sources[0]
    assert record.sha256 == digest
    assert record.row_count == 2
    assert (record.date_range.start, record.date_range.end) == (
        date(2026, 8, 28),
        date(2026, 8, 29),
    )
