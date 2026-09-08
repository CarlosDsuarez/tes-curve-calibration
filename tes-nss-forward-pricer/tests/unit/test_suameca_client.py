"""Offline tests for the SUAMECA client.

Every HTTP interaction is mocked with ``responses``: this module never touches
the network and runs in the ``unit`` tier.

The fixtures reproduce payloads captured from the live host on 2026-09-07 and
recorded in ``docs/suameca_reverse_engineering.md``, including the two responses
that arrive with HTTP 200 and are not data.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import requests
import responses

from tes_pricer.data.suameca_client import (
    DEFAULT_BASE_URL,
    TES_PRICE_COLUMNS,
    SuamecaClient,
    SuamecaDataUnavailableError,
    SuamecaSchemaError,
    SuamecaSeries,
)
from tes_pricer.data.validators import read_manifest, sha256_bytes

pytestmark = pytest.mark.unit

SERIES_URL = f"{DEFAULT_BASE_URL}/consultaInformacionSerieXTipoDato"
TEN_YEAR = SuamecaSeries(15274, "Cero Cupón TES pesos - 10 años", "Porcentaje")

# Epoch millis at midnight America/Bogota, exactly as the host returns them.
VALID_PAYLOAD = [
    {
        "id": 15274,
        "nombre": "Tasa de interés Cero Cupón, Títulos de Tesorería (TES), pesos - 10 años",
        "unidad": "Porcentaje",
        "numeroDecimales": 2,
        "fuente": "SEN y MEC, con cálculos Banco de la República.",
        "isSerie": "SI",
        "data": [
            [1041483600000, 14.69],  # 2003-01-02
            [1041570000000, 14.47],  # 2003-01-03
            [1041915600000, 15.57],  # 2003-01-07
            [1042002000000, 14.63],  # 2003-01-08
            [1042088400000, 15.54],  # 2003-01-09
        ],
    }
]

# What the host actually returns for an unknown idSerie: HTTP 200, valid JSON.
SENTINEL_PAYLOAD = [{"idPeriodicidad": 0, "valor": 0.0, "isSerie": "NO", "tieneHijos": "NO"}]

ANGULAR_SHELL = (
    '<!doctype html>\n<html dir="ltr" lang="es"><head><meta charset="utf-8">'
    "<title>Estadísticas Económicas Banco de la República</title></head>"
    "<body><app-root></app-root></body></html>"
)


@pytest.fixture
def client(tmp_path: Path) -> SuamecaClient:
    """A client writing its cache and manifest inside the test's tmp_path.

    ``backoff_seconds=0`` keeps the retry tests instant without weakening them:
    the retry *count* is what is asserted, not the wall clock.
    """
    return SuamecaClient(
        timeout=10.0,
        max_retries=3,
        backoff_seconds=0.0,
        raw_cache_dir=tmp_path / "raw" / "suameca",
        manifest_path=tmp_path / "manifest.json",
    )


def _manual_export(tmp_path: Path, *, columns: dict[str, list[str]]) -> Path:
    path = tmp_path / "export.csv"
    pd.DataFrame(columns).to_csv(path, index=False)
    return path


VALID_EXPORT_COLUMNS = {
    "Fecha": ["16/03/2026", "17/03/2026"],
    "Nemotecnico": ["COL17CT02871", "COL17CT02871"],
    "Precio Sucio": ["1.024,53", "1.026,10"],
    "Precio Limpio": ["987,41", "988,90"],
    "Tasa": ["9,12", "9,08"],
    "Cupon": ["7,25", "7,25"],
    "Fecha de vencimiento": ["18/10/2034", "18/10/2034"],
}


# --------------------------------------------------------------------------- #
# (a) successful response
# --------------------------------------------------------------------------- #


@responses.activate
def test_fetch_series_parses_valid_payload(client: SuamecaClient) -> None:
    """A well-formed payload becomes a typed, date-filtered frame."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    frame = client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert list(frame.columns) == ["fecha", "id_serie", "nombre", "valor", "unidad"]
    assert len(frame) == 5
    assert frame["valor"].tolist() == [14.69, 14.47, 15.57, 14.63, 15.54]
    assert frame["id_serie"].unique().tolist() == [15274]


@responses.activate
def test_epoch_millis_convert_in_bogota_time(client: SuamecaClient) -> None:
    """1041483600000 is 2003-01-02 in Bogota; UTC parsing would give 2003-01-01."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    frame = client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert frame["fecha"].iloc[0] == pd.Timestamp("2003-01-02")
    assert frame["fecha"].iloc[-1] == pd.Timestamp("2003-01-09")


@responses.activate
def test_request_sends_explicit_timeout(client: SuamecaClient) -> None:
    """Every request carries the configured timeout; none is left unbounded."""
    captured: list[float | None] = []
    original = requests.Session.get

    def spy(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        captured.append(kwargs.get("timeout"))  # type: ignore[arg-type]
        return original(self, url, **kwargs)  # type: ignore[arg-type]

    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)
    requests.Session.get = spy  # type: ignore[method-assign]
    try:
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))
    finally:
        requests.Session.get = original  # type: ignore[method-assign]

    assert captured == [10.0]


@responses.activate
def test_raw_payload_is_cached_before_parsing(client: SuamecaClient, tmp_path: Path) -> None:
    """The raw bytes hit disk so a parse failure never forces a re-fetch."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    cached = list((tmp_path / "raw" / "suameca").glob("*.json"))
    assert len(cached) == 1
    assert cached[0].name == "2003-01-31_serie-15274.json"
    assert json.loads(cached[0].read_text())[0]["id"] == 15274


@responses.activate
def test_cached_file_is_registered_in_manifest_with_sha256(
    client: SuamecaClient, tmp_path: Path
) -> None:
    """Provenance: the manifest records the archived file and its digest."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    manifest = read_manifest(tmp_path / "manifest.json")
    assert len(manifest.data_sources) == 1
    record = manifest.data_sources[0]
    assert record.endpoint_type == "SUAMECA"
    assert record.row_count == 5
    assert record.sha256 == sha256_bytes(Path(record.file_path).read_bytes())


# --------------------------------------------------------------------------- #
# (b) HTTP 200 carrying HTML - the dangerous silent failure
# --------------------------------------------------------------------------- #


@responses.activate
def test_html_body_with_status_200_raises_immediately(client: SuamecaClient) -> None:
    """A 200 with text/html is the SPA shell, not data. It must never parse."""
    responses.add(
        responses.GET,
        SERIES_URL,
        body=ANGULAR_SHELL,
        status=200,
        content_type="text/html;charset=UTF-8",
    )

    with pytest.raises(SuamecaDataUnavailableError, match="Angular viewer page"):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))


@responses.activate
def test_html_response_is_not_retried(client: SuamecaClient) -> None:
    """Repeating a request that reached the SPA cannot help, so it is not retried."""
    responses.add(
        responses.GET,
        SERIES_URL,
        body=ANGULAR_SHELL,
        status=200,
        content_type="text/html;charset=UTF-8",
    )

    with pytest.raises(SuamecaDataUnavailableError):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert len(responses.calls) == 1


@responses.activate
def test_html_response_leaves_nothing_in_the_cache(client: SuamecaClient, tmp_path: Path) -> None:
    """A rejected transport must not archive a viewer page as if it were data."""
    responses.add(
        responses.GET,
        SERIES_URL,
        body=ANGULAR_SHELL,
        status=200,
        content_type="text/html;charset=UTF-8",
    )

    with pytest.raises(SuamecaDataUnavailableError):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert not list((tmp_path / "raw" / "suameca").glob("*"))


@responses.activate
def test_sentinel_record_for_unknown_series_raises(client: SuamecaClient) -> None:
    """HTTP 200 + valid JSON + isSerie="NO" is an error, not an empty series."""
    responses.add(responses.GET, SERIES_URL, json=SENTINEL_PAYLOAD, status=200)

    with pytest.raises(SuamecaDataUnavailableError, match=r'isSerie="NO"'):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))


# --------------------------------------------------------------------------- #
# (c) timeouts: retry with backoff, then fail loudly
# --------------------------------------------------------------------------- #


@responses.activate
def test_timeout_retries_then_raises_with_clear_message(client: SuamecaClient) -> None:
    """Three attempts, then a message naming the timeout and the attempt count."""
    for _ in range(3):
        responses.add(responses.GET, SERIES_URL, body=requests.exceptions.ConnectTimeout())

    with pytest.raises(SuamecaDataUnavailableError) as excinfo:
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert len(responses.calls) == 3
    message = str(excinfo.value)
    assert "failed after 3 attempt(s)" in message
    assert "timeout=10.0s" in message


@responses.activate
def test_backoff_is_exponential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delays double each retry: 1s then 2s for a base of 1s."""
    slept: list[float] = []
    monkeypatch.setattr(
        "tes_pricer.data.suameca_client.time.sleep", lambda seconds: slept.append(seconds)
    )
    client = SuamecaClient(
        max_retries=3,
        backoff_seconds=1.0,
        raw_cache_dir=tmp_path / "raw",
        manifest_path=None,
    )
    for _ in range(3):
        responses.add(responses.GET, SERIES_URL, body=requests.exceptions.ConnectTimeout())

    with pytest.raises(SuamecaDataUnavailableError):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert slept == [1.0, 2.0]


@responses.activate
def test_transient_failure_then_success(client: SuamecaClient) -> None:
    """A recovered request returns data; the retry is not fatal by itself."""
    responses.add(responses.GET, SERIES_URL, body=requests.exceptions.ConnectTimeout())
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    frame = client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert len(responses.calls) == 2
    assert len(frame) == 5


@responses.activate
def test_server_error_is_retried(client: SuamecaClient) -> None:
    """A 500 is transient and retried; the last one surfaces as an exception."""
    for _ in range(3):
        responses.add(responses.GET, SERIES_URL, body="boom", status=500)

    with pytest.raises(SuamecaDataUnavailableError):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))

    assert len(responses.calls) == 3


# --------------------------------------------------------------------------- #
# (d) schema failures name the offending column
# --------------------------------------------------------------------------- #


def test_manual_export_missing_column_names_it(client: SuamecaClient, tmp_path: Path) -> None:
    """A missing column is reported by name, with what the file did contain."""
    columns = dict(VALID_EXPORT_COLUMNS)
    del columns["Precio Limpio"]
    del columns["Cupon"]
    path = _manual_export(tmp_path, columns=columns)

    with pytest.raises(SuamecaSchemaError) as excinfo:
        client.load_from_manual_export(path)

    message = str(excinfo.value)
    assert "'precio_limpio'" in message
    assert "'cupon'" in message
    assert "Nemotecnico" in message  # the columns actually present


@responses.activate
def test_payload_without_data_key_raises_schema_error(client: SuamecaClient) -> None:
    """A metadata-only record is a schema failure, not an empty result."""
    payload = [{"id": 15274, "nombre": "x", "isSerie": "SI"}]
    responses.add(responses.GET, SERIES_URL, json=payload, status=200)

    with pytest.raises(SuamecaSchemaError, match="no 'data' key"):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))


@responses.activate
def test_malformed_observation_pairs_raise_schema_error(client: SuamecaClient) -> None:
    """`data` must hold [epoch_millis, value] pairs."""
    payload = [{"id": 15274, "isSerie": "SI", "data": [{"fecha": 1, "valor": 2}]}]
    responses.add(responses.GET, SERIES_URL, json=payload, status=200)

    with pytest.raises(SuamecaSchemaError, match="epoch_millis"):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))


@responses.activate
def test_empty_data_array_raises_rather_than_returning_empty_frame(
    client: SuamecaClient,
) -> None:
    """The client never signals absence with an empty DataFrame."""
    payload = [{"id": 15274, "isSerie": "SI", "data": []}]
    responses.add(responses.GET, SERIES_URL, json=payload, status=200)

    with pytest.raises(SuamecaDataUnavailableError, match="empty 'data' array"):
        client.fetch_series(TEN_YEAR, date(2003, 1, 1), date(2003, 1, 31))


@responses.activate
def test_date_window_with_no_observations_raises(client: SuamecaClient) -> None:
    """An out-of-range window is an error with the available span in the message."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    with pytest.raises(SuamecaDataUnavailableError, match="2003-01-02 to 2003-01-09"):
        client.fetch_series(TEN_YEAR, date(2020, 1, 1), date(2020, 12, 31))


# --------------------------------------------------------------------------- #
# fetch_tes_prices and the documented manual-export fallback
# --------------------------------------------------------------------------- #


def test_fetch_tes_prices_without_export_explains_the_limitation(
    client: SuamecaClient,
) -> None:
    """No silent empty frame: the message states why SUAMECA cannot serve this."""
    with pytest.raises(SuamecaDataUnavailableError) as excinfo:
        client.fetch_tes_prices("COL17CT02871", date(2026, 3, 1), date(2026, 3, 31))

    message = str(excinfo.value)
    assert "does not publish per-ISIN TES prices" in message
    assert "docs/suameca_reverse_engineering.md" in message


def test_fetch_tes_prices_from_manual_export_returns_exact_schema(tmp_path: Path) -> None:
    """The fallback yields the contracted columns, in order, correctly typed."""
    path = _manual_export(tmp_path, columns=dict(VALID_EXPORT_COLUMNS))
    client = SuamecaClient(
        manual_export_path=path,
        raw_cache_dir=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.json",
    )

    frame = client.fetch_tes_prices("COL17CT02871", date(2026, 3, 1), date(2026, 3, 31))

    assert list(frame.columns) == list(TES_PRICE_COLUMNS)
    assert len(frame) == 2
    # Colombian locale: "1.024,53" is one thousand and change, not 102453.
    assert frame["precio_sucio"].iloc[0] == pytest.approx(1024.53)
    assert frame["precio_limpio"].iloc[0] == pytest.approx(987.41)
    assert frame["tasa_negociacion"].iloc[0] == pytest.approx(9.12)
    assert frame["fecha"].iloc[0] == pd.Timestamp("2026-03-16")
    assert frame["fecha_vencimiento"].iloc[0] == pd.Timestamp("2034-10-18")


def test_fetch_tes_prices_unknown_isin_raises(tmp_path: Path) -> None:
    """An ISIN absent from the export is an error listing what is present."""
    path = _manual_export(tmp_path, columns=dict(VALID_EXPORT_COLUMNS))
    client = SuamecaClient(
        manual_export_path=path,
        raw_cache_dir=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.json",
    )

    with pytest.raises(SuamecaDataUnavailableError, match="COL17CT02871"):
        client.fetch_tes_prices("COL17CT99999", date(2026, 3, 1), date(2026, 3, 31))


def test_manual_export_is_registered_in_manifest(tmp_path: Path) -> None:
    """A manual export is provenance-tracked exactly like a downloaded payload."""
    path = _manual_export(tmp_path, columns=dict(VALID_EXPORT_COLUMNS))
    client = SuamecaClient(
        manual_export_path=path,
        raw_cache_dir=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.json",
    )

    client.fetch_tes_prices("COL17CT02871", date(2026, 3, 1), date(2026, 3, 31))

    record = read_manifest(tmp_path / "manifest.json").data_sources[0]
    assert record.endpoint_type == "manual"
    assert record.sha256 == sha256_bytes(path.read_bytes())
    assert record.row_count == 2
    assert record.date_range.start == date(2026, 3, 16)


def test_manual_export_missing_file_raises(client: SuamecaClient, tmp_path: Path) -> None:
    """A missing export is reported as unavailable data, not an OSError."""
    with pytest.raises(SuamecaDataUnavailableError, match="not found"):
        client.load_from_manual_export(tmp_path / "nope.csv")


def test_reversed_date_range_is_rejected(client: SuamecaClient) -> None:
    """A caller error is caught before any request goes out."""
    with pytest.raises(ValueError, match="is after"):
        client.fetch_tes_prices("COL17CT02871", date(2026, 3, 31), date(2026, 3, 1))


# --------------------------------------------------------------------------- #
# health_check
# --------------------------------------------------------------------------- #


@responses.activate
def test_health_check_true_when_endpoint_answers(client: SuamecaClient) -> None:
    """A well-formed series payload means the endpoint is usable."""
    responses.add(responses.GET, SERIES_URL, json=VALID_PAYLOAD, status=200)

    assert client.health_check() is True


@responses.activate
def test_health_check_false_when_endpoint_serves_html(client: SuamecaClient) -> None:
    """The viewer page is not a healthy endpoint."""
    responses.add(
        responses.GET,
        SERIES_URL,
        body=ANGULAR_SHELL,
        status=200,
        content_type="text/html;charset=UTF-8",
    )

    assert client.health_check() is False


@responses.activate
def test_health_check_true_via_manual_export_fallback(tmp_path: Path) -> None:
    """A dead endpoint still counts as healthy when the fallback is in place."""
    path = _manual_export(tmp_path, columns=dict(VALID_EXPORT_COLUMNS))
    client = SuamecaClient(
        backoff_seconds=0.0,
        manual_export_path=path,
        raw_cache_dir=tmp_path / "raw",
        manifest_path=tmp_path / "manifest.json",
    )
    for _ in range(3):
        responses.add(responses.GET, SERIES_URL, body=requests.exceptions.ConnectTimeout())

    assert client.health_check() is True


@responses.activate
def test_health_check_never_raises(client: SuamecaClient) -> None:
    """Callers use health_check to decide whether to start; it must not throw."""
    for _ in range(3):
        responses.add(responses.GET, SERIES_URL, body=requests.exceptions.ConnectionError())

    assert client.health_check() is False
