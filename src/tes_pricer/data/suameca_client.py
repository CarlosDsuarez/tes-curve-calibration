"""Client for the Banco de la República statistics service behind SUAMECA.

The endpoints used here are **undocumented internals of an Angular SPA**,
reverse-engineered on 2026-09-07. Full findings, sample payloads and the
verification steps live in ``docs/suameca_reverse_engineering.md``. Read that
before changing anything in this module.

Three facts drive the whole design:

1. **SUAMECA publishes no per-ISIN TES prices.** The complete TES inventory is a
   fitted zero-coupon curve at 1/5/10 years, the four fitted Nelson-Siegel
   parameters, and two bid-ask spread series. Banco de la República computes
   these from the SEN and MEC tapes and publishes only the fit; the underlying
   quotes are distributed commercially. :meth:`SuamecaClient.fetch_tes_prices`
   therefore cannot be served over the network and is backed by
   :meth:`SuamecaClient.load_from_manual_export`. This limitation is surfaced
   loudly, never papered over.
2. **A 200 response is not evidence of data.** The host returns HTTP 200 with an
   HTML SPA shell for any mistyped path, and HTTP 200 with a JSON sentinel
   record (``isSerie: "NO"``) for an unknown series id. Both are checked before
   any parsing happens.
3. **The values endpoint ignores date ranges.** It returns the full history, so
   filtering is done client-side after the raw payload has been archived.

Raw payloads are written to disk and hashed *before* being parsed, so a parsing
failure leaves the evidence on disk instead of forcing a re-fetch.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pandas as pd
import requests

from tes_pricer.data.validators import (
    DateRange,
    EndpointType,
    register_data_source,
    sha256_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL: Final = (
    "https://suameca.banrep.gov.co/estadisticas-economicas-back/rest"
    "/estadisticaEconomicaRestService"
)

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_MAX_RETRIES: Final = 3
DEFAULT_BACKOFF_SECONDS: Final = 1.0

BOGOTA_TZ: Final = timezone(timedelta(hours=-5), "America/Bogota")
"""Colombia does not observe DST, so a fixed -05:00 offset is exact."""

RETRYABLE_STATUS_CODES: Final = frozenset({429, 500, 502, 503, 504})

TES_PRICE_COLUMNS: Final = (
    "fecha",
    "isin",
    "precio_sucio",
    "precio_limpio",
    "tasa_negociacion",
    "cupon",
    "fecha_vencimiento",
)
"""The exact, ordered schema returned by :meth:`SuamecaClient.fetch_tes_prices`."""

SERIES_COLUMNS: Final = ("fecha", "id_serie", "nombre", "valor", "unidad")


class SuamecaError(RuntimeError):
    """Base class for every failure raised by this module."""


class SuamecaDataUnavailableError(SuamecaError):
    """The requested data could not be obtained, for any reason.

    Raised on transport failure, on an HTML response, on the JSON sentinel
    record, and on a payload that does not match the expected schema. This
    module never returns an empty DataFrame to signal failure.
    """


class SuamecaSchemaError(SuamecaDataUnavailableError):
    """A payload was retrieved but does not match the expected schema."""


@dataclass(frozen=True, slots=True)
class SuamecaSeries:
    """Identifier of a single SUAMECA statistical series."""

    series_id: int
    name: str
    unit: str
    tipo_dato: int = 1

    @property
    def slug(self) -> str:
        """Filesystem-safe label used in raw cache filenames."""
        return f"serie-{self.series_id}"


# Verified against `listarSeriesXCategoria` on 2026-09-07. See the docs table.
TES_ZERO_COUPON_COP: Final = (
    SuamecaSeries(15272, "Cero Cupón TES pesos - 1 año", "Porcentaje"),
    SuamecaSeries(15273, "Cero Cupón TES pesos - 5 años", "Porcentaje"),
    SuamecaSeries(15274, "Cero Cupón TES pesos - 10 años", "Porcentaje"),
)
TES_ZERO_COUPON_UVR: Final = (
    SuamecaSeries(15275, "Cero Cupón TES UVR - 1 año", "Porcentaje"),
    SuamecaSeries(15276, "Cero Cupón TES UVR - 5 años", "Porcentaje"),
    SuamecaSeries(15277, "Cero Cupón TES UVR - 10 años", "Porcentaje"),
)
TES_NELSON_SIEGEL_BETAS_COP: Final = (
    SuamecaSeries(15278, "Beta TES pesos - B0", "Unidades"),
    SuamecaSeries(15279, "Beta TES pesos - B1", "Unidades"),
    SuamecaSeries(15280, "Beta TES pesos - B2", "Unidades"),
    SuamecaSeries(15281, "Beta TES pesos - Tau", "Unidades"),
)

HEALTH_CHECK_SERIES: Final = TES_ZERO_COUPON_COP[2]

_NO_PRICES_MESSAGE: Final = (
    "SUAMECA does not publish per-ISIN TES prices. Its entire TES inventory is "
    "the fitted zero-coupon curve (series 15272-15277), the Nelson-Siegel "
    "parameters (15278-15285) and two bid-ask spread series (16720, 16721); "
    "none of them carries precio_sucio, precio_limpio or tasa_negociacion for "
    "an individual bond. Banco de la Republica computes these from the SEN and "
    "MEC tapes and publishes only the fit. Supply a manual export via "
    "manual_export_path=... or call load_from_manual_export(). See "
    "docs/suameca_reverse_engineering.md section 0."
)

# Accepted source spellings for a manual export, mapped to the canonical schema.
_MANUAL_EXPORT_ALIASES: Final = {
    "fecha": "fecha",
    "fecha_operacion": "fecha",
    "fecha de operacion": "fecha",
    "fecha_negociacion": "fecha",
    "isin": "isin",
    "nemotecnico": "isin",
    "nemotécnico": "isin",
    "codigo_isin": "isin",
    "precio_sucio": "precio_sucio",
    "precio sucio": "precio_sucio",
    "preciosucio": "precio_sucio",
    "precio_limpio": "precio_limpio",
    "precio limpio": "precio_limpio",
    "preciolimpio": "precio_limpio",
    "tasa_negociacion": "tasa_negociacion",
    "tasa de negociacion": "tasa_negociacion",
    "tasa": "tasa_negociacion",
    "cupon": "cupon",
    "cupón": "cupon",
    "tasa_cupon": "cupon",
    "fecha_vencimiento": "fecha_vencimiento",
    "fecha de vencimiento": "fecha_vencimiento",
    "vencimiento": "fecha_vencimiento",
}


class SuamecaClient:
    """Retrying HTTP client for the Banco de la República statistics service.

    Args:
        base_url: Service root. Defaults to :data:`DEFAULT_BASE_URL`.
        timeout: Per-request timeout in seconds. Always explicit; never ``None``.
        max_retries: Total attempts per request, including the first.
        backoff_seconds: Base of the exponential backoff, ``backoff * 2**n``.
        raw_cache_dir: Where raw payloads are archived before parsing.
        manifest_path: Manifest to register each archived file in.
        manual_export_path: Optional CSV/XLSX standing in for the per-ISIN price
            feed SUAMECA does not provide.
        session: Injected for testing; a fresh session is created otherwise.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        raw_cache_dir: Path | str = Path("data/raw/suameca"),
        manifest_path: Path | str | None = Path("data/manifest.json"),
        manual_export_path: Path | str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.raw_cache_dir = Path(raw_cache_dir)
        self.manifest_path = Path(manifest_path) if manifest_path is not None else None
        self.manual_export_path = (
            Path(manual_export_path) if manual_export_path is not None else None
        )
        self._session = session if session is not None else requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "tes-curve-calibration/0.1 (+research; contact repo owner)",
            }
        )

    # ---------------------------------------------------------------- public

    def fetch_tes_prices(
        self,
        bond_isin: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Return per-ISIN TES prices over ``[start_date, end_date]``.

        Columns, in order: ``fecha``, ``isin``, ``precio_sucio``,
        ``precio_limpio``, ``tasa_negociacion``, ``cupon``,
        ``fecha_vencimiento``.

        **This method never reaches the network.** SUAMECA publishes no per-ISIN
        price series (see the module docstring and
        ``docs/suameca_reverse_engineering.md`` section 0), so the data comes
        from the manual export configured as ``manual_export_path``.

        Raises:
            SuamecaDataUnavailableError: If no manual export is configured, if
                the export cannot be read, if its schema does not match, or if
                it contains no row for ``bond_isin`` in the requested window.
                Never returns an empty DataFrame to signal absence.
        """
        if start_date > end_date:
            raise ValueError(f"start_date {start_date} is after end_date {end_date}")
        if self.manual_export_path is None:
            raise SuamecaDataUnavailableError(
                f"No price source available for ISIN {bond_isin}. {_NO_PRICES_MESSAGE}"
            )

        frame = self.load_from_manual_export(self.manual_export_path)
        selected = frame[
            (frame["isin"] == bond_isin)
            & (frame["fecha"] >= pd.Timestamp(start_date))
            & (frame["fecha"] <= pd.Timestamp(end_date))
        ].reset_index(drop=True)

        if selected.empty:
            available = sorted(frame["isin"].dropna().unique().tolist())
            raise SuamecaDataUnavailableError(
                f"Manual export {self.manual_export_path} has no rows for ISIN "
                f"{bond_isin} between {start_date} and {end_date}. "
                f"ISINs present in the file: {available[:20]}"
            )
        return selected

    def fetch_series(
        self,
        series: SuamecaSeries,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch one statistical series and clip it to ``[start_date, end_date]``.

        The upstream endpoint has no date parameters and always returns the full
        history; the range is applied here, after archiving the raw payload.

        Returns:
            A DataFrame with columns ``fecha``, ``id_serie``, ``nombre``,
            ``valor``, ``unidad``, sorted by ``fecha``.

        Raises:
            SuamecaDataUnavailableError: On transport failure, an HTML response,
                the unknown-series sentinel, or an empty window.
            SuamecaSchemaError: If the payload shape is not the expected one.
        """
        if start_date > end_date:
            raise ValueError(f"start_date {start_date} is after end_date {end_date}")

        payload_bytes = self._get_raw(
            "consultaInformacionSerieXTipoDato",
            {"idSerie": str(series.series_id), "tipoDato": str(series.tipo_dato)},
        )
        # Archive and hash BEFORE parsing, so a parse failure is debuggable.
        self._archive(payload_bytes, label=series.slug, reference_date=end_date)

        record = self._parse_series_record(payload_bytes, series)
        frame = self._to_frame(record, series)
        window = frame[
            (frame["fecha"] >= pd.Timestamp(start_date))
            & (frame["fecha"] <= pd.Timestamp(end_date))
        ].reset_index(drop=True)

        if window.empty:
            raise SuamecaDataUnavailableError(
                f"Series {series.series_id} ({series.name}) returned "
                f"{len(frame)} observations but none between {start_date} and "
                f"{end_date}. Available range: "
                f"{frame['fecha'].min().date()} to {frame['fecha'].max().date()}."
            )
        return window

    def fetch_zero_coupon_curve(
        self,
        start_date: date,
        end_date: date,
        *,
        denomination: str = "pesos",
    ) -> pd.DataFrame:
        """Fetch the published TES zero-coupon rates at 1, 5 and 10 years.

        Args:
            denomination: ``"pesos"`` or ``"uvr"``.

        Returns:
            A long-format DataFrame with the :data:`SERIES_COLUMNS` schema.
            Rates are percentages, as published.
        """
        catalogue = {"pesos": TES_ZERO_COUPON_COP, "uvr": TES_ZERO_COUPON_UVR}
        try:
            wanted = catalogue[denomination.lower()]
        except KeyError:
            raise ValueError(
                f"denomination must be one of {sorted(catalogue)}, got {denomination!r}"
            ) from None
        frames = [self.fetch_series(s, start_date, end_date) for s in wanted]
        return pd.concat(frames, ignore_index=True)

    def fetch_nelson_siegel_betas(
        self,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch the four published COP Nelson-Siegel parameters (B0, B1, B2, Tau).

        These are **Nelson-Siegel (1987)**, not Svensson, and they are published
        rounded to two decimals as decimals rather than percentages. Rebuilding
        a curve from them reproduces the published zero rates only to about
        ±20bp, so treat them as an indicative cross-check and never as
        calibration input. See ``docs/suameca_reverse_engineering.md`` section 3.
        """
        frames = [self.fetch_series(s, start_date, end_date) for s in TES_NELSON_SIEGEL_BETAS_COP]
        return pd.concat(frames, ignore_index=True)

    def load_from_manual_export(self, filepath: str | Path) -> pd.DataFrame:
        """Ingest a per-ISIN TES price file exported by hand from a venue terminal.

        This is the documented fallback for the data SUAMECA does not publish,
        not a convenience path. Accepts ``.csv``, ``.xlsx`` and ``.xls``.

        Column names are matched case-insensitively against a small alias table,
        so the usual Spanish spellings (``nemotecnico``, ``precio sucio``,
        ``fecha de vencimiento``, ...) are accepted. Colombian numeric locale is
        handled: ``.`` as thousands separator and ``,`` as decimal separator.

        Returns:
            A DataFrame with exactly the :data:`TES_PRICE_COLUMNS` schema.

        Raises:
            SuamecaDataUnavailableError: If the file is missing or unreadable.
            SuamecaSchemaError: If a required column is absent. The message names
                every missing column and lists what the file did contain.
        """
        path = Path(filepath)
        if not path.is_file():
            raise SuamecaDataUnavailableError(f"Manual export not found: {path}")

        suffix = path.suffix.lower()
        try:
            if suffix == ".csv":
                raw = pd.read_csv(path, dtype=str, sep=None, engine="python")
            elif suffix in {".xlsx", ".xls"}:
                raw = pd.read_excel(path, dtype=str)
            else:
                raise SuamecaDataUnavailableError(
                    f"Unsupported manual export format {suffix!r} for {path}; "
                    "expected .csv, .xlsx or .xls"
                )
        except SuamecaError:
            raise
        except Exception as exc:  # pragma: no cover - passthrough of reader errors
            raise SuamecaDataUnavailableError(f"Could not read {path}: {exc}") from exc

        renamed = {
            column: _MANUAL_EXPORT_ALIASES[str(column).strip().lower()]
            for column in raw.columns
            if str(column).strip().lower() in _MANUAL_EXPORT_ALIASES
        }
        frame = raw.rename(columns=renamed)

        missing = [c for c in TES_PRICE_COLUMNS if c not in frame.columns]
        if missing:
            raise SuamecaSchemaError(
                f"Manual export {path} is missing required column(s): "
                f"{missing}. Columns found: {list(raw.columns)}. Expected schema: "
                f"{list(TES_PRICE_COLUMNS)}"
            )

        frame = frame.loc[:, list(TES_PRICE_COLUMNS)].copy()
        for column in ("fecha", "fecha_vencimiento"):
            frame[column] = pd.to_datetime(frame[column], dayfirst=True, errors="coerce")
        for column in ("precio_sucio", "precio_limpio", "tasa_negociacion", "cupon"):
            frame[column] = _to_numeric_co(frame[column])
        frame["isin"] = frame["isin"].astype("string").str.strip()

        self._register_file(path, EndpointType.MANUAL, str(path), len(frame), frame["fecha"])
        return frame.reset_index(drop=True)

    def health_check(self) -> bool:
        """Report whether a usable price source is reachable right now.

        Returns ``True`` if the live endpoint answers with a well-formed series
        payload, or if a readable manual export is configured. Never raises:
        callers use it to decide whether to start a run.
        """
        try:
            payload = self._get_raw(
                "consultaInformacionSerieXTipoDato",
                {
                    "idSerie": str(HEALTH_CHECK_SERIES.series_id),
                    "tipoDato": str(HEALTH_CHECK_SERIES.tipo_dato),
                },
            )
            self._parse_series_record(payload, HEALTH_CHECK_SERIES)
        except (SuamecaError, ValueError) as exc:
            logger.warning("SUAMECA endpoint health check failed: %s", exc)
            if self.manual_export_path is not None and self.manual_export_path.is_file():
                logger.info("Falling back to manual export %s", self.manual_export_path)
                return True
            return False
        return True

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> SuamecaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --------------------------------------------------------------- private

    def _get_raw(self, endpoint: str, params: dict[str, str]) -> bytes:
        """GET ``endpoint`` with retries, returning the unparsed body.

        Retries transport errors and the retryable status codes with exponential
        backoff. An HTML body is **not** retried: it means the request reached
        the SPA rather than the service, and repeating it changes nothing.
        """
        url = f"{self.base_url}/{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            if attempt:
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                logger.info(
                    "Retry %d/%d for %s in %.1fs", attempt, self.max_retries - 1, url, delay
                )
                time.sleep(delay)
            try:
                response = self._session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.RequestException as exc:
                last_error = exc
                logger.warning("Transport failure on %s (attempt %d): %s", url, attempt + 1, exc)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = SuamecaDataUnavailableError(
                    f"{url} returned retryable HTTP {response.status_code}"
                )
                logger.warning(
                    "HTTP %s from %s (attempt %d)", response.status_code, url, attempt + 1
                )
                continue

            self._reject_html(response, url)

            if response.status_code >= 400:
                raise SuamecaDataUnavailableError(
                    f"{url} returned HTTP {response.status_code}: {response.text[:200]!r}"
                )
            return response.content

        raise SuamecaDataUnavailableError(
            f"{url} failed after {self.max_retries} attempt(s) "
            f"(timeout={self.timeout}s). Last error: {last_error}"
        ) from last_error

    @staticmethod
    def _reject_html(response: requests.Response, url: str) -> None:
        """Fail fast when the SPA shell comes back instead of the service.

        The host answers **HTTP 200 with ``text/html``** for any mistyped path
        under the Angular app. Parsing that as data is the most dangerous silent
        failure in this project, so it is refused before anything else looks at
        the body.
        """
        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type.lower():
            raise SuamecaDataUnavailableError(
                f"{url} returned HTTP {response.status_code} with Content-Type "
                f"{content_type!r}. This is the Angular viewer page, not data. "
                "The endpoint path or parameters are wrong, or the service has "
                "been redeployed; re-run the investigation in "
                "docs/suameca_reverse_engineering.md."
            )

    @staticmethod
    def _parse_series_record(payload: bytes, series: SuamecaSeries) -> dict[str, Any]:
        """Decode a series payload, rejecting the unknown-series sentinel.

        An unknown ``idSerie`` returns HTTP 200 and valid JSON, but the record is
        ``{"idPeriodicidad": 0, "valor": 0.0, "isSerie": "NO", ...}`` with no
        ``data`` key. Checking the content type alone does not catch it.
        """
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SuamecaSchemaError(
                f"Series {series.series_id} returned a body that is not JSON: {payload[:200]!r}"
            ) from exc

        if not isinstance(decoded, list) or not decoded:
            raise SuamecaSchemaError(
                f"Series {series.series_id} expected a non-empty JSON array, got "
                f"{type(decoded).__name__} with content {str(decoded)[:200]!r}"
            )
        record = decoded[0]
        if not isinstance(record, dict):
            raise SuamecaSchemaError(
                f"Series {series.series_id} expected an object inside the array, "
                f"got {type(record).__name__}"
            )
        if str(record.get("isSerie", "")).upper() == "NO":
            raise SuamecaDataUnavailableError(
                f"SUAMECA does not recognise series id {series.series_id}: it "
                'answered HTTP 200 with the sentinel record isSerie="NO". The '
                "series id is wrong or has been retired upstream."
            )
        if "data" not in record:
            raise SuamecaSchemaError(
                f"Series {series.series_id} payload has no 'data' key. Keys "
                f"present: {sorted(record)[:20]}"
            )
        observations = record["data"]
        if not isinstance(observations, list):
            raise SuamecaSchemaError(
                f"Series {series.series_id} 'data' should be a list of "
                f"[epoch_millis, value] pairs, got {type(observations).__name__}"
            )
        if not observations:
            raise SuamecaDataUnavailableError(
                f"Series {series.series_id} ({series.name}) returned an empty "
                "'data' array. The endpoint answered but published no "
                "observations."
            )
        return record

    @staticmethod
    def _to_frame(record: dict[str, Any], series: SuamecaSeries) -> pd.DataFrame:
        """Turn ``[[epoch_millis, value], ...]`` into the SERIES_COLUMNS schema.

        Timestamps are midnight in America/Bogota (a fixed -05:00 offset), so
        they are localised before the date is taken; using UTC would shift every
        observation to the previous day at 19:00.
        """
        rows = record["data"]
        malformed = [r for r in rows[:50] if not (isinstance(r, list | tuple) and len(r) == 2)]
        if malformed:
            raise SuamecaSchemaError(
                f"Series {series.series_id} 'data' must hold [epoch_millis, value] "
                f"pairs; found {malformed[0]!r}"
            )
        stamps = pd.to_datetime([r[0] for r in rows], unit="ms", utc=True)
        frame = pd.DataFrame(
            {
                "fecha": stamps.tz_convert(BOGOTA_TZ).tz_localize(None).normalize(),
                "id_serie": series.series_id,
                "nombre": record.get("nombre", series.name),
                "valor": pd.to_numeric([r[1] for r in rows], errors="coerce"),
                "unidad": record.get("unidad", series.unit),
            }
        )
        return frame.sort_values("fecha").reset_index(drop=True)

    def _archive(self, payload: bytes, *, label: str, reference_date: date) -> Path:
        """Write a raw payload to the cache and register it in the manifest.

        Called before parsing, so a schema failure leaves the exact bytes on
        disk for debugging instead of forcing another request.
        """
        self.raw_cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.raw_cache_dir / f"{reference_date.isoformat()}_{label}.json"
        destination.write_bytes(payload)
        logger.debug("Archived %d bytes to %s", len(payload), destination)

        if self.manifest_path is not None:
            register_data_source(
                self.manifest_path,
                name=f"SUAMECA {label}",
                url=self.base_url,
                endpoint_type=EndpointType.SUAMECA,
                file_path=str(destination),
                sha256=sha256_bytes(payload),
                row_count=_count_observations(payload),
                date_range=DateRange(start=reference_date, end=reference_date),
            )
        return destination

    def _register_file(
        self,
        path: Path,
        endpoint_type: EndpointType,
        name: str,
        row_count: int,
        dates: pd.Series,
    ) -> None:
        """Register an already-on-disk file (a manual export) in the manifest."""
        if self.manifest_path is None:
            return
        valid = dates.dropna()
        span = DateRange(
            start=valid.min().date() if not valid.empty else date.min,
            end=valid.max().date() if not valid.empty else date.min,
        )
        register_data_source(
            self.manifest_path,
            name=f"Manual export {name}",
            url=f"file://{path.resolve()}",
            endpoint_type=endpoint_type,
            file_path=str(path),
            sha256=sha256_bytes(path.read_bytes()),
            row_count=row_count,
            date_range=span,
        )


def _count_observations(payload: bytes) -> int:
    """Best-effort observation count for the manifest; 0 if the payload is opaque."""
    try:
        decoded = json.loads(payload)
        return len(decoded[0]["data"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return 0


def _to_numeric_co(series: pd.Series) -> pd.Series:
    """Parse Colombian-formatted numbers: ``1.234,56`` becomes ``1234.56``."""
    cleaned = (
        series.astype("string")
        .str.strip()
        .str.replace(r"\.(?=\d{3}(\D|$))", "", regex=True)
        .str.replace(",", ".", regex=False)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string, for manifest timestamps."""
    return datetime.now(UTC).isoformat(timespec="seconds")
