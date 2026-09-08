"""Client for datos.gov.co, the Colombian open data portal (Socrata / SODA).

Uses ``sodapy``, the official Socrata Python client, rather than hand-rolled
HTTP: it handles the SoQL query encoding and the paging contract correctly.

Two things to know about the portal itself:

* The app token is **optional**. Anonymous calls work but share a per-IP
  throttling pool; a registered token raises the limit substantially and is the
  only reason ``SOCRATA_APP_TOKEN`` exists in ``.env``.
* ``get`` is capped per call (1000 rows by default, 50000 maximum), so any
  full-dataset pull must page through with ``$offset`` until a short page comes
  back. :meth:`SocrataClient.fetch_all` is that loop.

Dataset inventory, verified live on 2026-09-07
----------------------------------------------

Nothing here is hardcoded on faith. Every identifier below was resolved through
the Socrata discovery API (``api.us.socrata.com/api/catalog/v1``), then confirmed
against ``/api/views/<id>.json`` and a real SoQL query.
:meth:`SocrataClient.discover_datasets` and
:meth:`SocrataClient.dataset_health_check` are those two steps, exposed so a run
can re-verify before trusting an id.

**TRM - available.** ``32sa-8pi3``, "Tasa de Cambio Representativa del Mercado -
TRM", published by the Superintendencia Financiera de Colombia. 8341 rows,
1991-12-02 to 2026-09-05, columns ``valor`` / ``unidad`` / ``vigenciadesde`` /
``vigenciahasta``. ``mcec-87by`` ("... - Historico") is a byte-identical mirror
with the same row count and range; it is kept as :data:`TRM_DATASET_MIRROR` for
when the primary is unavailable.

The TRM is **not** a plain daily series: each row carries a validity window. The
fixing computed on a Friday is valid from Saturday through the next business
day, so ``vigenciadesde`` is frequently a Saturday and ``vigenciahasta`` a Monday
or later. ``fetch_trm`` expands that window by default, so ``fecha`` means "the
date on which this TRM applies" - which is what CIP and Garman-Kohlhagen need.
Pass ``expand_validity=False`` for one row per publication instead.

**IBR - not on this portal.** The only IBR asset on datos.gov.co is
``ev8i-uzwt``, "IBR - Indicador Bancario de Referencia". Its ``assetType`` is
``href``: a link stub pointing at Banco de la Republica, with no columns and no
rows. A SoQL query against it returns **HTTP 403** with
``"no row or column access to non-tabular tables"``. No tabular IBR dataset
exists anywhere on the domain; the closest tabular asset, ``axk9-g2nh``
("Tasas de interes de captacion y operaciones del mercado monetario"), is
per-entity SFC reporting, not the published IBR fixing.

So :meth:`SocrataClient.fetch_ibr` is fully implemented but has no live default
source. It is driven by an :class:`IbrDatasetSpec` describing where the date,
rate and tenor live; with the default spec it raises
:class:`SocrataDataUnavailableError` naming the alternative rather than
returning an empty frame. That alternative is SUAMECA, already wired up in
:mod:`tes_pricer.data.suameca_client`, which does publish the fixings daily::

    overnight  nominal 241    effective 15324
    1 mes      nominal 242    effective 15325
    3 meses    nominal 243    effective 15326
    6 meses    nominal 16560  effective 16561
    12 meses   nominal 16562  effective 16563

**A correction on the IBR convention.** Banco de la Republica publishes each IBR
tenor *twice*: nominal base 360 and effective base 365. The fixing the market
quotes and settles on - and the one this project's OIS bootstrap needs - is the
**nominal ACT/360** series, per the convention table in ``README.md``. The
effective-annual series is a derived transformation, not the primary
publication. ``fetch_ibr`` therefore records which convention the source is in,
on ``frame.attrs["convention"]``, and converts nothing implicitly: the Phase 5
bootstrap must read that attribute and reconcile deliberately.

Conventions honoured here
-------------------------

Rates cross every boundary out of this module as **decimals**, never
percentages (``README.md``, "Conventions"). The TRM is a price level in COP per
one USD and is passed through unscaled.

Validation policy
-----------------

Post-fetch checks **warn, they never drop**. An implausible TRM print or an IBR
outside the plausible band is a fact about the source that a human must see, so
it is emitted as a :class:`SocrataValidationWarning` and recorded in the
:class:`~tes_pricer.data.validators.ValidationReport` attached to
``frame.attrs["validation_report"]`` - with the offending row still in the
frame. Silently discarding it would hide exactly the day that matters.
"""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import holidays
import pandas as pd
import requests
from requests.exceptions import HTTPError, RequestException
from sodapy import Socrata

from tes_pricer.data.validators import (
    DateRange,
    EndpointType,
    ValidationReport,
    ValidationStatus,
    register_data_source,
    sha256_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_DOMAIN: Final = "www.datos.gov.co"
DISCOVERY_DOMAIN: Final = "api.us.socrata.com"
"""Socrata's cross-domain catalog, used to re-verify an id rather than trust it."""

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_PAGE_SIZE: Final = 1000
"""SODA's own default per-call cap. The documented maximum is 50000."""

MAX_PAGE_SIZE: Final = 50_000

STALE_AFTER_DAYS: Final = 30
"""A dataset untouched for longer than this is flagged as possibly discontinued."""

TRM_COLUMNS: Final = ("fecha", "trm_cop_usd")
"""The exact, ordered schema returned by :meth:`SocrataClient.fetch_trm`."""

IBR_COLUMNS: Final = ("fecha", "ibr_tasa", "tenor")
"""The exact, ordered schema returned by :meth:`SocrataClient.fetch_ibr`."""

TRM_MIN_COP_USD: Final = 2_000.0
TRM_MAX_COP_USD: Final = 10_000.0
"""Plausible USD/COP band. Covers 2015-2026 with room; widen for a longer history.

The TRM's published history opens at 632.85 COP/USD in December 1991, so a pull
that reaches back that far will legitimately trip the lower bound. That is a
warning, not a rejection - see the module docstring's validation policy.
"""

IBR_MIN_DECIMAL: Final = 0.0
IBR_MAX_DECIMAL: Final = 0.30
"""Plausible IBR band as a decimal: strictly positive, below 30% per annum."""

MAX_GAP_BUSINESS_DAYS: Final = 5
"""A run of *more than* this many consecutive missing business days is a gap.

Colombian weekends and holidays are excluded before counting, so Semana Santa or
a Monday puente never registers as one.
"""


class SocrataError(RuntimeError):
    """Base class for every failure raised by this module."""


class SocrataDataUnavailableError(SocrataError):
    """The requested data could not be obtained, for any reason.

    Raised on transport failure, on a portal error, on a dataset that is not
    tabular, and on an empty window. This module never returns an empty
    DataFrame to signal failure.
    """


class SocrataSchemaError(SocrataDataUnavailableError):
    """A payload was retrieved but does not match the expected schema."""


class SocrataValidationWarning(UserWarning):
    """A fetched frame is structurally sound but numerically suspicious.

    Carries range breaches and business-day gaps. Never raised: the rows stay in
    the frame and the caller decides.
    """


@dataclass(frozen=True, slots=True)
class SocrataDataset:
    """Identifier of a Socrata dataset."""

    dataset_id: str
    """The four-by-four identifier, e.g. ``"32sa-8pi3"``."""
    name: str
    description: str = ""

    @property
    def slug(self) -> str:
        """Filesystem-safe label used in raw cache filenames."""
        return self.dataset_id.replace("/", "-")


TRM_DATASET: Final = SocrataDataset(
    "32sa-8pi3",
    "Tasa de Cambio Representativa del Mercado - TRM",
    "Superintendencia Financiera de Colombia. Columns: valor, unidad, "
    "vigenciadesde, vigenciahasta. Verified 2026-09-07.",
)
TRM_DATASET_MIRROR: Final = SocrataDataset(
    "mcec-87by",
    "Tasa de Cambio Representativa del Mercado - Historico",
    "Byte-identical mirror of 32sa-8pi3 (same 8341 rows, same range). Fallback "
    "only. Verified 2026-09-07.",
)
IBR_DATASET_HREF_STUB: Final = SocrataDataset(
    "ev8i-uzwt",
    "IBR - Indicador Bancario de Referencia",
    "assetType=href. A link to Banco de la Republica, not a table: SoQL returns "
    "HTTP 403 'no row or column access to non-tabular tables'. Verified 2026-09-07.",
)

TRM_DATE_FROM_COLUMN: Final = "vigenciadesde"
TRM_DATE_TO_COLUMN: Final = "vigenciahasta"
TRM_VALUE_COLUMN: Final = "valor"
TRM_UNIT_COLUMN: Final = "unidad"

_TENOR_ALIASES: Final = {
    "overnight": "overnight",
    "on": "overnight",
    "o/n": "overnight",
    "1d": "overnight",
    "un dia": "overnight",
    "1 dia": "overnight",
    "1m": "1m",
    "1 mes": "1m",
    "30d": "1m",
    "3m": "3m",
    "3 meses": "3m",
    "90d": "3m",
    "6m": "6m",
    "6 meses": "6m",
    "180d": "6m",
    "12m": "12m",
    "1y": "12m",
    "12 meses": "12m",
    "360d": "12m",
}
"""Accepted spellings of an IBR tenor, mapped to the canonical label."""


@dataclass(frozen=True, slots=True)
class IbrDatasetSpec:
    """Where the date, rate and tenor live inside an IBR source dataset.

    Parameterised rather than hardcoded because datos.gov.co has no tabular IBR
    dataset today (see the module docstring). A caller who has one - a
    replacement four-by-four, a mirror, or a departmental republication - points
    ``fetch_ibr`` at it by passing a spec, without touching this module.

    Args:
        dataset: The dataset to query.
        date_column: Source column holding the fixing date.
        rate_column: Source column holding the rate.
        tenor_column: Source column holding the tenor, if the dataset is long
            format. ``None`` means the dataset holds a single tenor, named by
            ``single_tenor``.
        tenor_values: Canonical tenor label to the value used in the source, for
            the ``$where`` clause. Only consulted when ``tenor_column`` is set.
        single_tenor: The tenor this dataset holds, when ``tenor_column`` is
            ``None``.
        rate_is_percent: Whether the source publishes ``9.5`` (percent) rather
            than ``0.095``. The frame always leaves this module as a decimal.
        convention: The rate convention the source publishes in. Recorded on
            ``frame.attrs["convention"]``; never used to convert anything.
        is_tabular: ``False`` marks a known-unqueryable asset so ``fetch_ibr``
            fails with a specific, actionable message instead of a bare HTTP 403.
    """

    dataset: SocrataDataset
    date_column: str = "fecha"
    rate_column: str = "valor"
    tenor_column: str | None = None
    tenor_values: Mapping[str, str] = field(default_factory=dict)
    single_tenor: str = "overnight"
    rate_is_percent: bool = True
    convention: str = "unknown"
    is_tabular: bool = True


DEFAULT_IBR_SPEC: Final = IbrDatasetSpec(
    dataset=IBR_DATASET_HREF_STUB,
    convention="unknown",
    is_tabular=False,
)
"""The portal's own IBR asset, which cannot be queried. Present so the failure is named."""

_IBR_UNAVAILABLE_MESSAGE: Final = (
    "datos.gov.co publishes no queryable IBR dataset. Its only IBR asset is "
    "ev8i-uzwt, which has assetType='href': a link to Banco de la Republica with "
    "no columns and no rows, and SoQL against it returns HTTP 403 'no row or "
    "column access to non-tabular tables'. The closest tabular asset, axk9-g2nh, "
    "is per-entity SFC money-market reporting, not the published fixing. Use "
    "SUAMECA instead (tes_pricer.data.suameca_client): series 241 overnight "
    "nominal ACT/360, 15324 overnight effective base 365, 242/15325 for 1 mes, "
    "243/15326 for 3 meses, 16560/16561 for 6 meses, 16562/16563 for 12 meses. "
    "If you have another tabular Socrata source, pass spec=IbrDatasetSpec(...) "
    "to describe it."
)


class SocrataClient:
    """Paging wrapper around ``sodapy.Socrata`` for the datos.gov.co portal.

    Args:
        app_token: Optional, but recommended. Anonymous requests work and share a
            per-IP throttling pool; a registered token raises the limit. Register
            at https://evergreen.data.socrata.com/profile/edit/developer_settings
        domain: Portal host. Only change this to hit another Socrata deployment.
        timeout: Per-request timeout in seconds. Always explicit; never ``None``.
        page_size: Rows per ``$limit``. Capped at :data:`MAX_PAGE_SIZE`.
        raw_cache_dir: Where raw payloads are archived before parsing.
        manifest_path: Manifest to register each archived file in. ``None``
            disables archiving and provenance recording, which is only
            appropriate in tests.
    """

    def __init__(
        self,
        app_token: str | None = None,
        *,
        domain: str = DEFAULT_DOMAIN,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        page_size: int = DEFAULT_PAGE_SIZE,
        raw_cache_dir: Path | str = Path("data/raw/socrata"),
        manifest_path: Path | str | None = Path("data/manifest.json"),
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        if page_size > MAX_PAGE_SIZE:
            raise ValueError(f"page_size must not exceed {MAX_PAGE_SIZE}, got {page_size}")
        self.domain = domain
        self.timeout = timeout
        self.page_size = page_size
        self.raw_cache_dir = Path(raw_cache_dir)
        self.manifest_path = Path(manifest_path) if manifest_path is not None else None
        self._app_token = app_token
        self.has_app_token = app_token is not None
        if not self.has_app_token:
            logger.info(
                "No SOCRATA_APP_TOKEN supplied: requests share the anonymous per-IP "
                "throttling pool. Set one in .env for a higher rate limit."
            )
        self._client = Socrata(domain, app_token, timeout=int(timeout))

    # ---------------------------------------------------------------- public

    def fetch(
        self,
        dataset: SocrataDataset,
        *,
        select: str | None = None,
        where: str | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> pd.DataFrame:
        """Run a single SoQL query and return the raw, unvalidated rows.

        Every value comes back as a string, exactly as SODA serialises it. Type
        coercion belongs to the caller, so a malformed cell stays visible rather
        than turning silently into ``NaN`` deep inside a parser.

        Raises:
            SocrataDataUnavailableError: On transport failure or a portal error.
                A 403 carrying "non-tabular" is reported as such, because it
                means the asset is a link stub and no retry will help.
        """
        rows = self._get_rows(
            dataset,
            select=select,
            where=where,
            order=order,
            limit=limit if limit is not None else self.page_size,
            offset=offset,
        )
        return pd.DataFrame.from_records(rows, coerce_float=False)

    def fetch_all(
        self,
        dataset: SocrataDataset,
        *,
        select: str | None = None,
        where: str | None = None,
        order: str | None = None,
        max_rows: int | None = None,
    ) -> pd.DataFrame:
        """Page through a dataset until exhausted or ``max_rows`` is reached.

        Paging without a stable ``order`` can duplicate or skip rows, so ``:id``
        - Socrata's immutable row identifier - is applied when the caller does
        not supply an ordering of their own.
        """
        effective_order = order if order is not None else ":id"
        collected: list[dict[str, Any]] = []
        offset = 0

        while True:
            remaining = None if max_rows is None else max_rows - len(collected)
            if remaining is not None and remaining <= 0:
                break
            page_limit = self.page_size if remaining is None else min(self.page_size, remaining)
            page = self._get_rows(
                dataset,
                select=select,
                where=where,
                order=effective_order,
                limit=page_limit,
                offset=offset,
            )
            collected.extend(page)
            if len(page) < page_limit:
                break
            offset += len(page)

        return pd.DataFrame.from_records(collected, coerce_float=False)

    def dataset_metadata(self, dataset: SocrataDataset) -> dict[str, Any]:
        """Return the portal's raw metadata record, including its last update time.

        Raises:
            SocrataDataUnavailableError: If the metadata endpoint cannot be read.
            SocrataSchemaError: If it answers with something other than an object.
        """
        try:
            metadata = self._client.get_metadata(dataset.dataset_id)
        except (HTTPError, RequestException, ValueError) as exc:
            raise SocrataDataUnavailableError(
                f"Could not read metadata for {dataset.dataset_id} on {self.domain}: {exc}"
            ) from exc
        if not isinstance(metadata, dict):
            raise SocrataSchemaError(
                f"Metadata for {dataset.dataset_id} should be an object, got "
                f"{type(metadata).__name__}"
            )
        return metadata

    def dataset_health_check(self, dataset_id: str) -> dict[str, Any]:
        """Report whether a dataset is still alive, tabular and being updated.

        This is the guard against an open data portal's quiet failure mode: an
        identifier that still resolves, still returns HTTP 200 for its metadata,
        and has not been refreshed in two years. It is also how an
        ``assetType='href'`` stub - which looks healthy until you query it - is
        caught before a fetch.

        Never raises. A dataset that cannot be reached comes back with
        ``reachable=False`` and the reason in ``error``, so a caller can decide
        whether to start a run at all.

        Returns:
            A dict with ``dataset_id``, ``domain``, ``reachable``, ``name``,
            ``attribution``, ``asset_type``, ``is_tabular``, ``columns``,
            ``rows_updated_at`` (ISO 8601 UTC or ``None``), ``days_since_update``,
            ``is_stale``, ``row_count`` (``None`` when not countable) and
            ``error``.
        """
        report: dict[str, Any] = {
            "dataset_id": dataset_id,
            "domain": self.domain,
            "reachable": False,
            "name": None,
            "attribution": None,
            "asset_type": None,
            "is_tabular": False,
            "columns": [],
            "rows_updated_at": None,
            "days_since_update": None,
            "is_stale": None,
            "row_count": None,
            "error": None,
        }

        try:
            metadata = self.dataset_metadata(SocrataDataset(dataset_id, dataset_id))
        except SocrataError as exc:
            report["error"] = str(exc)
            logger.warning("Health check failed for %s: %s", dataset_id, exc)
            return report

        asset_type = str(metadata.get("assetType") or metadata.get("viewType") or "")
        columns = [
            str(column.get("fieldName"))
            for column in metadata.get("columns") or []
            if isinstance(column, dict) and column.get("fieldName")
        ]
        report.update(
            {
                "reachable": True,
                "name": metadata.get("name"),
                "attribution": metadata.get("attribution"),
                "asset_type": asset_type,
                "is_tabular": asset_type not in {"href", "blob", "story"} and bool(columns),
                "columns": columns,
            }
        )

        updated_at = _epoch_to_datetime(metadata.get("rowsUpdatedAt"))
        if updated_at is not None:
            age_days = (datetime.now(UTC) - updated_at).days
            report["rows_updated_at"] = updated_at.isoformat(timespec="seconds")
            report["days_since_update"] = age_days
            report["is_stale"] = age_days > STALE_AFTER_DAYS
            if report["is_stale"]:
                logger.warning(
                    "Dataset %s (%s) has not been refreshed in %d days; it may have been "
                    "discontinued upstream.",
                    dataset_id,
                    report["name"],
                    age_days,
                )

        if not report["is_tabular"]:
            report["error"] = (
                f"Dataset {dataset_id} has assetType={asset_type!r} and {len(columns)} "
                "column(s): it is not a queryable table."
            )
            logger.warning("%s", report["error"])
            return report

        try:
            counted = self._get_rows(
                SocrataDataset(dataset_id, dataset_id), select="count(1) as n", limit=1
            )
            report["row_count"] = int(counted[0]["n"]) if counted else 0
        except (SocrataError, KeyError, TypeError, ValueError) as exc:
            report["error"] = f"Metadata readable but row count failed: {exc}"
            logger.warning("Row count failed for %s: %s", dataset_id, exc)

        return report

    def discover_datasets(
        self,
        query: str,
        *,
        limit: int = 10,
        only: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search the Socrata catalog, so an identifier is re-verified, not trusted.

        Args:
            query: Free-text search, e.g. ``"Tasa Representativa del Mercado"``.
            limit: Maximum results.
            only: Restrict the asset type, e.g. ``"dataset"`` to exclude the
                ``href`` stubs and saved filters that otherwise crowd the results.

        Returns:
            One dict per hit with ``dataset_id``, ``name``, ``type``,
            ``attribution``, ``updated_at`` and ``description``, in the catalog's
            own relevance order.

        Raises:
            SocrataDataUnavailableError: If the catalog cannot be reached.
        """
        # Deliberately not sodapy's `datasets()`: that helper sends the client's
        # own domain as the `domains` filter and then iterates a string kwarg
        # character by character, so a cross-domain search comes back wrong.
        params: dict[str, Any] = {"domains": self.domain, "q": query, "limit": limit}
        if only is not None:
            params["only"] = only
        headers = {"X-App-Token": self._app_token} if self._app_token else {}

        try:
            response = requests.get(
                f"https://{DISCOVERY_DOMAIN}/api/catalog/v1",
                params=params,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            results = response.json().get("results", [])
        except (HTTPError, RequestException, ValueError) as exc:
            raise SocrataDataUnavailableError(
                f"Catalog search for {query!r} on {self.domain} failed: {exc}"
            ) from exc
        if not isinstance(results, list):
            raise SocrataSchemaError(
                f"Catalog search for {query!r} returned {type(results).__name__} where a "
                "list of results was expected."
            )

        hits: list[dict[str, Any]] = []
        for entry in results:
            resource = entry.get("resource", {}) if isinstance(entry, dict) else {}
            hits.append(
                {
                    "dataset_id": resource.get("id"),
                    "name": resource.get("name"),
                    "type": resource.get("type"),
                    "attribution": resource.get("attribution"),
                    "updated_at": resource.get("updatedAt"),
                    "description": (resource.get("description") or "")[:200],
                }
            )
        return hits

    def fetch_trm(
        self,
        start_date: date,
        end_date: date,
        *,
        dataset: SocrataDataset = TRM_DATASET,
        expand_validity: bool = True,
    ) -> pd.DataFrame:
        """Fetch the TRM over ``[start_date, end_date]``.

        Returns:
            A DataFrame with columns ``fecha`` and ``trm_cop_usd``, sorted by
            ``fecha``. The TRM is **COP per one USD**, the standard Colombian
            market convention, so USD is the foreign currency in CIP.
            ``frame.attrs["validation_report"]`` carries the
            :class:`~tes_pricer.data.validators.ValidationReport`.

        Args:
            dataset: Source dataset. Defaults to :data:`TRM_DATASET`; pass
                :data:`TRM_DATASET_MIRROR` if the primary is unavailable.
            expand_validity: When ``True`` (the default), each published fixing is
                repeated across every calendar date in its
                ``[vigenciadesde, vigenciahasta]`` window, so ``fecha`` means
                "the date on which this TRM applies". This matters: the fixing
                computed on a Friday is valid Saturday through the next business
                day, so keying on ``vigenciadesde`` alone puts market rates on
                Saturdays and leaves Mondays empty. When ``False``, one row per
                publication, dated at ``vigenciadesde``.

        Raises:
            ValueError: If ``start_date`` is after ``end_date``.
            SocrataDataUnavailableError: On transport failure, or when the window
                contains no observations. Never returns an empty frame.
            SocrataSchemaError: If the dataset no longer has the expected columns.
        """
        _require_ordered_window(start_date, end_date)

        # Select by overlap, not containment: a window that opens before
        # start_date can still be the fixing that applies on start_date.
        where = (
            f"{TRM_DATE_TO_COLUMN} >= '{_soql_timestamp(start_date)}' "
            f"AND {TRM_DATE_FROM_COLUMN} <= '{_soql_timestamp(end_date)}'"
        )
        order = f"{TRM_DATE_FROM_COLUMN} ASC"
        rows = self._get_rows(dataset, where=where, order=order, limit=self.page_size)
        rows = self._continue_paging(dataset, rows, where=where, order=order)
        self._archive(
            rows,
            dataset=dataset,
            label=f"trm-{dataset.slug}",
            window=DateRange(start_date, end_date),
        )

        if not rows:
            raise SocrataDataUnavailableError(
                f"TRM dataset {dataset.dataset_id} returned no rows between {start_date} "
                f"and {end_date}. Run dataset_health_check({dataset.dataset_id!r}) to see "
                "whether the dataset is still published and still tabular."
            )

        raw = pd.DataFrame.from_records(rows, coerce_float=False)
        missing = [c for c in (TRM_DATE_FROM_COLUMN, TRM_VALUE_COLUMN) if c not in raw.columns]
        if missing:
            raise SocrataSchemaError(
                f"TRM dataset {dataset.dataset_id} is missing column(s) {missing}. Columns "
                f"returned: {list(raw.columns)}. The dataset schema has changed; re-run "
                "discover_datasets() and confirm the identifier."
            )

        frame = _shape_trm(raw, start_date, end_date, expand_validity=expand_validity)
        if frame.empty:
            raise SocrataDataUnavailableError(
                f"TRM dataset {dataset.dataset_id} returned {len(raw)} row(s) but none fall "
                f"inside {start_date}..{end_date} after parsing. Check the "
                f"{TRM_DATE_FROM_COLUMN} column format."
            )

        report = validate_trm(frame, start_date, end_date)
        _emit_report(report, source=f"TRM {dataset.dataset_id}")
        frame.attrs["validation_report"] = report
        frame.attrs["dataset_id"] = dataset.dataset_id
        frame.attrs["quote_convention"] = "COP per 1 USD"
        return frame

    def fetch_ibr(
        self,
        start_date: date,
        end_date: date,
        tenor: str = "overnight",
        *,
        spec: IbrDatasetSpec = DEFAULT_IBR_SPEC,
    ) -> pd.DataFrame:
        """Fetch the IBR fixing over ``[start_date, end_date]`` for one tenor.

        Returns:
            A DataFrame with columns ``fecha``, ``ibr_tasa`` and ``tenor``, sorted
            by ``fecha``. ``ibr_tasa`` is a **decimal** (``0.095``), not a
            percentage, per the project convention that rates cross every
            internal boundary as decimals.

            The rate convention of the source is recorded on
            ``frame.attrs["convention"]``, never applied. Banco de la Republica
            publishes each tenor both nominal ACT/360 and effective base 365; the
            fixing the market settles on is the **nominal ACT/360** one, so the
            Phase 5 OIS bootstrap must reconcile the two explicitly rather than
            assume either.

        Args:
            tenor: ``"overnight"``, ``"1m"``, ``"3m"``, ``"6m"`` or ``"12m"``.
                Spanish spellings such as ``"1 mes"`` are accepted.
            spec: Describes the source dataset. The default is the portal's own
                IBR asset, which is an ``href`` stub and cannot be queried, so the
                default call raises with the SUAMECA alternative named.

        Raises:
            ValueError: If ``start_date`` is after ``end_date``, or the tenor is
                not recognised.
            SocrataDataUnavailableError: If the configured dataset is not tabular,
                on transport failure, or when the window is empty.
            SocrataSchemaError: If the dataset lacks the columns the spec names.
        """
        _require_ordered_window(start_date, end_date)
        canonical = canonical_tenor(tenor)

        if not spec.is_tabular:
            raise SocrataDataUnavailableError(
                f"Cannot fetch IBR {canonical} for {start_date}..{end_date}. "
                f"{_IBR_UNAVAILABLE_MESSAGE}"
            )

        clauses = [
            f"{spec.date_column} >= '{_soql_timestamp(start_date)}'",
            f"{spec.date_column} <= '{_soql_timestamp(end_date)}'",
        ]
        if spec.tenor_column is not None:
            source_value = spec.tenor_values.get(canonical)
            if source_value is None:
                raise ValueError(
                    f"Spec for {spec.dataset.dataset_id} has no source value for tenor "
                    f"{canonical!r}; it knows {sorted(spec.tenor_values)}"
                )
            clauses.append(f"{spec.tenor_column} = '{source_value}'")
        where = " AND ".join(clauses)
        order = f"{spec.date_column} ASC"

        rows = self._get_rows(spec.dataset, where=where, order=order, limit=self.page_size)
        rows = self._continue_paging(spec.dataset, rows, where=where, order=order)
        self._archive(
            rows,
            dataset=spec.dataset,
            label=f"ibr-{canonical}-{spec.dataset.slug}",
            window=DateRange(start_date, end_date),
        )

        if not rows:
            raise SocrataDataUnavailableError(
                f"IBR dataset {spec.dataset.dataset_id} returned no rows for tenor "
                f"{canonical} between {start_date} and {end_date}."
            )

        raw = pd.DataFrame.from_records(rows, coerce_float=False)
        missing = [c for c in (spec.date_column, spec.rate_column) if c not in raw.columns]
        if missing:
            raise SocrataSchemaError(
                f"IBR dataset {spec.dataset.dataset_id} is missing column(s) {missing}. "
                f"Columns returned: {list(raw.columns)}."
            )

        frame = _shape_ibr(raw, spec, canonical, start_date, end_date)
        if frame.empty:
            raise SocrataDataUnavailableError(
                f"IBR dataset {spec.dataset.dataset_id} returned {len(raw)} row(s) but none "
                f"fall inside {start_date}..{end_date} after parsing."
            )

        report = validate_ibr(frame, start_date, end_date)
        _emit_report(report, source=f"IBR {canonical} {spec.dataset.dataset_id}")
        frame.attrs["validation_report"] = report
        frame.attrs["dataset_id"] = spec.dataset.dataset_id
        frame.attrs["convention"] = spec.convention
        return frame

    def save_raw(self, frame: pd.DataFrame, destination: Path) -> str:
        """Persist a result set as UTF-8 CSV and return its SHA-256 hex digest.

        The digest, not the file, is what makes a calibration reproducible: it
        goes into ``manifest.json`` so a rerun months later can prove it read the
        same bytes.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = frame.to_csv(index=False).encode("utf-8")
        destination.write_bytes(payload)
        digest = sha256_bytes(payload)

        if self.manifest_path is not None:
            dates = (
                pd.to_datetime(frame["fecha"], errors="coerce").dropna()
                if "fecha" in frame.columns
                else pd.Series(dtype="datetime64[ns]")
            )
            span = DateRange(
                start=dates.min().date() if not dates.empty else date.min,
                end=dates.max().date() if not dates.empty else date.min,
            )
            register_data_source(
                self.manifest_path,
                name=f"Socrata processed {destination.name}",
                url=f"https://{self.domain}",
                endpoint_type=EndpointType.SOCRATA,
                file_path=str(destination),
                sha256=digest,
                row_count=len(frame),
                date_range=span,
            )
        return digest

    def close(self) -> None:
        """Release the underlying Socrata client."""
        self._client.close()

    def __enter__(self) -> SocrataClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --------------------------------------------------------------- private

    def _get_rows(
        self,
        dataset: SocrataDataset,
        *,
        select: str | None = None,
        where: str | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Issue one SoQL query, translating every failure into a SocrataError."""
        params: dict[str, Any] = {}
        if select is not None:
            params["select"] = select
        if where is not None:
            params["where"] = where
        if order is not None:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        if offset:
            params["offset"] = offset

        try:
            payload = self._client.get(dataset.dataset_id, **params)
        except HTTPError as exc:
            raise self._describe_http_error(dataset, exc) from exc
        except RequestException as exc:
            raise SocrataDataUnavailableError(
                f"Transport failure querying {dataset.dataset_id} on {self.domain} "
                f"(timeout={self.timeout}s): {exc}"
            ) from exc
        except ValueError as exc:
            raise SocrataSchemaError(
                f"{dataset.dataset_id} returned a body that is not JSON: {exc}"
            ) from exc
        except Exception as exc:
            # sodapy raises a bare Exception for an unrecognised content type,
            # which is how an HTML proxy or captive-portal page arrives. Naming
            # it here stops that page being mistaken for a transport failure.
            raise SocrataSchemaError(
                f"{dataset.dataset_id} on {self.domain} returned an unusable response: "
                f"{exc}. A body that is neither JSON nor CSV usually means a proxy or "
                "error page reached us instead of the portal."
            ) from exc

        if not isinstance(payload, list):
            raise SocrataSchemaError(
                f"{dataset.dataset_id} should return a JSON array of rows, got "
                f"{type(payload).__name__}: {str(payload)[:200]!r}"
            )
        return [row for row in payload if isinstance(row, dict)]

    def _describe_http_error(
        self, dataset: SocrataDataset, exc: HTTPError
    ) -> SocrataDataUnavailableError:
        """Turn an HTTP failure into a message that says what to do next.

        The 403 for a non-tabular asset is the one worth naming: it is not a
        permission problem, and no token or retry fixes it. The identifier points
        at a link stub, and the caller needs a different source entirely.
        """
        response = exc.response
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", "") or ""
        if status == 403 and "non-tabular" in body:
            return SocrataDataUnavailableError(
                f"Dataset {dataset.dataset_id} on {self.domain} is not a table: the portal "
                "answered HTTP 403 'no row or column access to non-tabular tables'. It is "
                "an href/blob asset, so no query and no app token will return rows. Use "
                f"dataset_health_check({dataset.dataset_id!r}) to confirm its assetType."
            )
        if status == 404:
            return SocrataDataUnavailableError(
                f"Dataset {dataset.dataset_id} does not exist on {self.domain} (HTTP 404). "
                "It has been retired or renumbered; re-resolve it with discover_datasets()."
            )
        if status == 429:
            hint = (
                "with an app token"
                if self.has_app_token
                else "anonymously; set SOCRATA_APP_TOKEN for a higher limit"
            )
            return SocrataDataUnavailableError(
                f"Rate limited by {self.domain} (HTTP 429) while querying "
                f"{dataset.dataset_id} {hint}."
            )
        return SocrataDataUnavailableError(
            f"{self.domain} returned HTTP {status} for {dataset.dataset_id}: {body[:300]!r}"
        )

    def _continue_paging(
        self,
        dataset: SocrataDataset,
        first_page: list[dict[str, Any]],
        *,
        where: str | None,
        order: str | None,
    ) -> list[dict[str, Any]]:
        """Keep requesting pages while the previous one came back full."""
        rows = list(first_page)
        page = first_page
        while len(page) == self.page_size:
            page = self._get_rows(
                dataset, where=where, order=order, limit=self.page_size, offset=len(rows)
            )
            rows.extend(page)
        return rows

    def _archive(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        dataset: SocrataDataset,
        label: str,
        window: DateRange,
    ) -> Path | None:
        """Write the raw SODA rows to the cache before anything parses them.

        Called before parsing, so a schema failure leaves the exact payload on
        disk for debugging instead of forcing another request against a throttled
        endpoint. Returns ``None`` when provenance recording is disabled.
        """
        if self.manifest_path is None:
            return None
        self.raw_cache_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(list(rows), ensure_ascii=False, indent=2).encode("utf-8")
        destination = self.raw_cache_dir / f"{window.end.isoformat()}_{label}.json"
        destination.write_bytes(payload)
        logger.debug("Archived %d bytes to %s", len(payload), destination)

        register_data_source(
            self.manifest_path,
            name=f"Socrata {label}",
            url=f"https://{self.domain}/resource/{dataset.dataset_id}.json",
            endpoint_type=EndpointType.SOCRATA,
            file_path=str(destination),
            sha256=sha256_bytes(payload),
            row_count=len(rows),
            date_range=window,
        )
        return destination


# --------------------------------------------------------------------------- #
# Colombian business-day calendar
# --------------------------------------------------------------------------- #


def colombian_holidays(start_date: date, end_date: date) -> set[date]:
    """Return the Colombian public holidays inside ``[start_date, end_date]``.

    Backed by the ``holidays`` package's ``CO`` calendar, which implements the
    Ley Emiliani rule that moves most Colombian holidays to the following Monday.
    Getting that wrong is what turns an ordinary puente into a phantom data gap.
    """
    if start_date > end_date:
        return set()
    years = list(range(start_date.year, end_date.year + 1))
    calendar = holidays.country_holidays("CO", years=years)
    return {day for day in calendar if start_date <= day <= end_date}


def colombian_business_days(start_date: date, end_date: date) -> list[date]:
    """Return every Monday-to-Friday date in the window that is not a CO holiday."""
    if start_date > end_date:
        return []
    non_business = colombian_holidays(start_date, end_date)
    days: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5 and current not in non_business:
            days.append(current)
        current += timedelta(days=1)
    return days


def find_business_day_gaps(
    observed: Iterable[date],
    start_date: date,
    end_date: date,
    *,
    max_gap_business_days: int = MAX_GAP_BUSINESS_DAYS,
) -> list[tuple[date, date, int]]:
    """Find runs of consecutive business days with no observation.

    Weekends and Colombian holidays are removed before counting, so they can
    never register as a gap. Only runs **longer than** ``max_gap_business_days``
    are returned; shorter ones are ordinary publication lag.

    Returns:
        ``(first_missing, last_missing, length)`` per offending run, in order.
    """
    have = set(observed)
    gaps: list[tuple[date, date, int]] = []
    run: list[date] = []

    for day in colombian_business_days(start_date, end_date):
        if day in have:
            if len(run) > max_gap_business_days:
                gaps.append((run[0], run[-1], len(run)))
            run = []
        else:
            run.append(day)
    if len(run) > max_gap_business_days:
        gaps.append((run[0], run[-1], len(run)))
    return gaps


# --------------------------------------------------------------------------- #
# Post-fetch validation. Warns, never drops.
# --------------------------------------------------------------------------- #


def validate_trm(
    frame: pd.DataFrame,
    start_date: date,
    end_date: date,
    *,
    minimum: float = TRM_MIN_COP_USD,
    maximum: float = TRM_MAX_COP_USD,
    max_gap_business_days: int = MAX_GAP_BUSINESS_DAYS,
) -> ValidationReport:
    """Check a TRM frame for implausible levels and business-day gaps.

    Out-of-range prints are reported as warnings and **left in the frame**: a
    devaluation that breaks the band is exactly the observation a human must see,
    and dropping it would silently bias a CIP calculation.
    """
    errors: list[str] = []
    warns: list[str] = []

    missing_columns = [c for c in TRM_COLUMNS if c not in frame.columns]
    if missing_columns:
        errors.append(f"missing column(s) {missing_columns}")
        return ValidationReport(ValidationStatus.FAILED, len(frame), tuple(errors), ())

    values = frame["trm_cop_usd"]
    null_count = int(values.isna().sum())
    if null_count:
        warns.append(
            f"{null_count} TRM value(s) are null after numeric parsing; the source cell was "
            "empty or not a number."
        )

    breaches = frame[(values < minimum) | (values > maximum)].dropna(subset=["trm_cop_usd"])
    if not breaches.empty:
        sample = ", ".join(
            f"{row.fecha.date()}={row.trm_cop_usd:,.2f}"
            for row in breaches.head(5).itertuples(index=False)
        )
        warns.append(
            f"{len(breaches)} TRM value(s) outside the plausible band [{minimum:,.0f}, "
            f"{maximum:,.0f}] COP/USD: {sample}{' ...' if len(breaches) > 5 else ''}. Rows "
            "retained, not dropped. If this is a deliberate deep-history pull, widen the "
            "band explicitly."
        )

    warns.extend(
        _gap_warnings(frame["fecha"], start_date, end_date, max_gap_business_days, label="TRM")
    )
    warns.extend(_duplicate_warnings(frame["fecha"], label="TRM"))

    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    return ValidationReport(status, len(frame), tuple(errors), tuple(warns))


def validate_ibr(
    frame: pd.DataFrame,
    start_date: date,
    end_date: date,
    *,
    minimum: float = IBR_MIN_DECIMAL,
    maximum: float = IBR_MAX_DECIMAL,
    max_gap_business_days: int = MAX_GAP_BUSINESS_DAYS,
) -> ValidationReport:
    """Check an IBR frame for implausible rates and business-day gaps.

    Bounds are **decimals**: strictly above ``minimum`` and strictly below
    ``maximum``, i.e. positive and under 30% per annum by default. As with the
    TRM, breaches warn and the rows stay.
    """
    errors: list[str] = []
    warns: list[str] = []

    missing_columns = [c for c in IBR_COLUMNS if c not in frame.columns]
    if missing_columns:
        errors.append(f"missing column(s) {missing_columns}")
        return ValidationReport(ValidationStatus.FAILED, len(frame), tuple(errors), ())

    rates = frame["ibr_tasa"]
    null_count = int(rates.isna().sum())
    if null_count:
        warns.append(f"{null_count} IBR value(s) are null after numeric parsing.")

    breaches = frame[(rates <= minimum) | (rates >= maximum)].dropna(subset=["ibr_tasa"])
    if not breaches.empty:
        sample = ", ".join(
            f"{row.fecha.date()}={row.ibr_tasa:.6f}"
            for row in breaches.head(5).itertuples(index=False)
        )
        warns.append(
            f"{len(breaches)} IBR value(s) outside the plausible band ({minimum:.2%}, "
            f"{maximum:.2%}) per annum: {sample}{' ...' if len(breaches) > 5 else ''}. Rows "
            "retained, not dropped."
        )

    # A whole series above 1.0 is not a rate spike, it is a unit mistake: the
    # source was already in percent and got scaled a second time, or not at all.
    finite = rates.dropna()
    if not finite.empty and float(finite.median()) > 1.0:
        warns.append(
            f"IBR median is {float(finite.median()):.4f}, far above 1.0. This frame is almost "
            "certainly still in percentage points; rates must cross this boundary as "
            "decimals. Check IbrDatasetSpec.rate_is_percent."
        )

    warns.extend(
        _gap_warnings(frame["fecha"], start_date, end_date, max_gap_business_days, label="IBR")
    )
    warns.extend(_duplicate_warnings(frame["fecha"], label="IBR"))

    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    return ValidationReport(status, len(frame), tuple(errors), tuple(warns))


def canonical_tenor(tenor: str) -> str:
    """Normalise an IBR tenor spelling to its canonical label.

    Raises:
        ValueError: If the tenor is not one this module knows about. Guessing
            would put a three-month rate in an overnight column.
    """
    key = " ".join(str(tenor).strip().lower().split())
    try:
        return _TENOR_ALIASES[key]
    except KeyError:
        raise ValueError(
            f"Unknown IBR tenor {tenor!r}. Known spellings: {sorted(_TENOR_ALIASES)}"
        ) from None


# --------------------------------------------------------------------------- #
# Module-private helpers
# --------------------------------------------------------------------------- #


def _shape_trm(
    raw: pd.DataFrame,
    start_date: date,
    end_date: date,
    *,
    expand_validity: bool,
) -> pd.DataFrame:
    """Turn raw TRM rows into the :data:`TRM_COLUMNS` schema."""
    frame = pd.DataFrame(
        {
            "desde": pd.to_datetime(raw[TRM_DATE_FROM_COLUMN], errors="coerce"),
            "trm_cop_usd": pd.to_numeric(raw[TRM_VALUE_COLUMN], errors="coerce"),
        }
    )
    frame["hasta"] = (
        pd.to_datetime(raw[TRM_DATE_TO_COLUMN], errors="coerce")
        if TRM_DATE_TO_COLUMN in raw.columns
        else frame["desde"]
    )
    # A missing or inverted end date degrades to a single-day window rather than
    # dropping the observation.
    frame["hasta"] = frame["hasta"].fillna(frame["desde"])
    frame.loc[frame["hasta"] < frame["desde"], "hasta"] = frame["desde"]

    if TRM_UNIT_COLUMN in raw.columns:
        units = set(raw[TRM_UNIT_COLUMN].dropna().astype(str).str.strip().unique())
        if units - {"COP"}:
            logger.warning(
                "TRM rows carry unexpected unit(s) %s; expected COP per 1 USD only.",
                sorted(units),
            )

    frame = frame.dropna(subset=["desde"])
    if expand_validity:
        frame = frame.assign(
            fecha=[
                pd.date_range(row.desde, row.hasta, freq="D")
                for row in frame.itertuples(index=False)
            ]
        ).explode("fecha", ignore_index=True)
    else:
        frame = frame.assign(fecha=frame["desde"])

    frame["fecha"] = pd.to_datetime(frame["fecha"]).dt.normalize()
    window = frame[
        (frame["fecha"] >= pd.Timestamp(start_date)) & (frame["fecha"] <= pd.Timestamp(end_date))
    ]
    window = window.sort_values("fecha").drop_duplicates(subset="fecha", keep="last")
    return window.loc[:, list(TRM_COLUMNS)].reset_index(drop=True)


def _shape_ibr(
    raw: pd.DataFrame,
    spec: IbrDatasetSpec,
    tenor: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Turn raw IBR rows into the :data:`IBR_COLUMNS` schema, as decimals."""
    rates = pd.to_numeric(raw[spec.rate_column], errors="coerce")
    if spec.rate_is_percent:
        rates = rates / 100.0

    frame = pd.DataFrame(
        {
            "fecha": pd.to_datetime(raw[spec.date_column], errors="coerce"),
            "ibr_tasa": rates,
            "tenor": tenor,
        }
    ).dropna(subset=["fecha"])
    frame["fecha"] = frame["fecha"].dt.normalize()

    window = frame[
        (frame["fecha"] >= pd.Timestamp(start_date)) & (frame["fecha"] <= pd.Timestamp(end_date))
    ]
    window = window.sort_values("fecha").drop_duplicates(subset="fecha", keep="last")
    return window.loc[:, list(IBR_COLUMNS)].reset_index(drop=True)


def _gap_warnings(
    dates: pd.Series,
    start_date: date,
    end_date: date,
    max_gap_business_days: int,
    *,
    label: str,
) -> list[str]:
    """Render :func:`find_business_day_gaps` output as human-readable warnings."""
    observed = {stamp.date() for stamp in pd.to_datetime(dates, errors="coerce").dropna()}
    gaps = find_business_day_gaps(
        observed, start_date, end_date, max_gap_business_days=max_gap_business_days
    )
    return [
        f"{label} has no observation for {length} consecutive Colombian business days, "
        f"{first} to {last} (weekends and CO holidays already excluded; the threshold is "
        f"{max_gap_business_days})."
        for first, last, length in gaps
    ]


def _duplicate_warnings(dates: pd.Series, *, label: str) -> list[str]:
    """Warn when a date still appears more than once after de-duplication."""
    duplicated = dates[dates.duplicated()]
    if duplicated.empty:
        return []
    sample = sorted({stamp.date() for stamp in duplicated})[:5]
    return [f"{label} has {len(duplicated)} duplicate date(s) after shaping, e.g. {sample}."]


def _emit_report(report: ValidationReport, *, source: str) -> None:
    """Log and re-emit every warning in a report, so nothing is quietly swallowed."""
    for message in report.warnings:
        logger.warning("%s: %s", source, message)
        warnings.warn(f"{source}: {message}", SocrataValidationWarning, stacklevel=2)
    for message in report.errors:
        logger.error("%s: %s", source, message)


def _require_ordered_window(start_date: date, end_date: date) -> None:
    """Reject an inverted window before a query is built from it."""
    if start_date > end_date:
        raise ValueError(f"start_date {start_date} is after end_date {end_date}")


def _soql_timestamp(day: date) -> str:
    """Render a date as the floating timestamp literal SoQL expects."""
    return f"{day.isoformat()}T00:00:00.000"


def _epoch_to_datetime(value: object) -> datetime | None:
    """Convert Socrata's ``rowsUpdatedAt`` epoch seconds to an aware datetime."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


__all__ = [
    "DEFAULT_DOMAIN",
    "DEFAULT_IBR_SPEC",
    "IBR_COLUMNS",
    "IBR_DATASET_HREF_STUB",
    "IBR_MAX_DECIMAL",
    "IBR_MIN_DECIMAL",
    "MAX_GAP_BUSINESS_DAYS",
    "TRM_COLUMNS",
    "TRM_DATASET",
    "TRM_DATASET_MIRROR",
    "TRM_MAX_COP_USD",
    "TRM_MIN_COP_USD",
    "IbrDatasetSpec",
    "SocrataClient",
    "SocrataDataUnavailableError",
    "SocrataDataset",
    "SocrataError",
    "SocrataSchemaError",
    "SocrataValidationWarning",
    "canonical_tenor",
    "colombian_business_days",
    "colombian_holidays",
    "find_business_day_gaps",
    "validate_ibr",
    "validate_trm",
]
