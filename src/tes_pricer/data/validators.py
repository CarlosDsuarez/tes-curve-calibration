"""Raw-schema validation and provenance manifests.

This module is the gate between the network and the numerical core. A frame that
has not passed through a validator here must never reach ``tes_pricer.math``.

Validation is deliberately paranoid about the failure modes that actually bite
on Colombian market data:

* Comma decimal separators and thousands separators arriving as strings.
* Dates as ``dd/mm/yyyy`` (not ISO) and holidays silently carrying the previous
  business day's fixing.
* Rates that are sometimes percentages (``9.5``) and sometimes decimals
  (``0.095``) depending on the series.
* Stale quotes: an unchanged price across many sessions on an illiquid bond,
  which will otherwise anchor the long end of the calibration.

It also owns the ``manifest.json`` provenance record (schema ``1.0.0``), because
``validation_status`` is a validation outcome and belongs with the code that
produces it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import pandas as pd

MANIFEST_SCHEMA_VERSION = "1.0.0"


class ValidationStatus(StrEnum):
    """Overall outcome recorded in the manifest."""

    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class EndpointType(StrEnum):
    """How a data source was obtained."""

    SUAMECA = "SUAMECA"
    SOCRATA = "Socrata"
    MANUAL = "manual"


class SchemaValidationError(ValueError):
    """Raised when a raw frame does not satisfy its declared schema."""


@dataclass(frozen=True, slots=True)
class DateRange:
    """Inclusive observation window of a data source."""

    start: date
    end: date


@dataclass(frozen=True, slots=True)
class DataSourceRecord:
    """One entry of the ``data_sources`` array in ``manifest.json``."""

    name: str
    url: str
    endpoint_type: EndpointType
    retrieved_at: str
    """ISO 8601 UTC timestamp of the fetch."""
    file_path: str
    sha256: str
    row_count: int
    date_range: DateRange


@dataclass(slots=True)
class Manifest:
    """The provenance record regenerated on every ingestion run."""

    generated_at: str
    data_sources: list[DataSourceRecord] = field(default_factory=list)
    validation_status: ValidationStatus = ValidationStatus.PENDING
    schema_version: str = MANIFEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        """Serialise to the exact JSON shape of the manifest specification."""
        return {
            "generated_at": self.generated_at,
            "data_sources": [
                {
                    "name": s.name,
                    "url": s.url,
                    "endpoint_type": s.endpoint_type.value,
                    "retrieved_at": s.retrieved_at,
                    "file_path": s.file_path,
                    "sha256": s.sha256,
                    "row_count": s.row_count,
                    "date_range": {
                        "start": s.date_range.start.isoformat(),
                        "end": s.date_range.end.isoformat(),
                    },
                }
                for s in self.data_sources
            ],
            "validation_status": self.validation_status.value,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Manifest:
        """Parse a manifest, rejecting an unknown ``schema_version``."""
        version = str(payload.get("schema_version", ""))
        if version != MANIFEST_SCHEMA_VERSION:
            raise SchemaValidationError(
                f"Unsupported manifest schema_version {version!r}; "
                f"this build understands {MANIFEST_SCHEMA_VERSION!r}"
            )
        raw_sources = payload.get("data_sources") or []
        if not isinstance(raw_sources, list):
            raise SchemaValidationError("data_sources must be a list")
        sources = [
            DataSourceRecord(
                name=str(entry["name"]),
                url=str(entry["url"]),
                endpoint_type=EndpointType(entry["endpoint_type"]),
                retrieved_at=str(entry["retrieved_at"]),
                file_path=str(entry["file_path"]),
                sha256=str(entry["sha256"]),
                row_count=int(entry["row_count"]),
                date_range=DateRange(
                    start=date.fromisoformat(entry["date_range"]["start"]),
                    end=date.fromisoformat(entry["date_range"]["end"]),
                ),
            )
            for entry in raw_sources
        ]
        return cls(
            generated_at=str(payload.get("generated_at", "")),
            data_sources=sources,
            validation_status=ValidationStatus(str(payload.get("validation_status", "PENDING"))),
            schema_version=version,
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Result of validating one raw frame."""

    status: ValidationStatus
    row_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def sha256_bytes(payload: bytes) -> str:
    """Return the SHA-256 hex digest of an in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_frame(frame: pd.DataFrame) -> str:
    """Return a stable SHA-256 digest of a DataFrame's canonical CSV form."""
    raise NotImplementedError("Phase 2: frame hashing")


def validate_tes_prices(frame: pd.DataFrame) -> ValidationReport:
    """Validate a raw TES price/yield frame.

    Required columns: ``isin``, ``trade_date``, ``clean_price``, ``yield``,
    ``maturity_date``. Checks dtypes, the absence of nulls in the key columns,
    monotone unique dates per ISIN, and that yields sit in a plausible range.
    """
    raise NotImplementedError("Phase 2: TES price validation")


def validate_ibr_series(frame: pd.DataFrame) -> ValidationReport:
    """Validate a raw IBR overnight fixing series.

    Required columns: ``fixing_date``, ``rate``. Checks business-day continuity
    against the Colombian calendar and flags repeated fixings as warnings.
    """
    raise NotImplementedError("Phase 2: IBR validation")


def validate_trm_series(frame: pd.DataFrame) -> ValidationReport:
    """Validate a raw TRM (USD/COP) series."""
    raise NotImplementedError("Phase 2: TRM validation")


def normalize_numeric_column(series: pd.Series, *, as_decimal: bool) -> pd.Series:
    """Coerce a locale-formatted numeric column to float.

    Strips thousands separators, converts a comma decimal separator, and when
    ``as_decimal`` is set divides percentage-scaled values by 100.
    """
    raise NotImplementedError("Phase 2: numeric normalisation")


def detect_stale_quotes(
    frame: pd.DataFrame,
    *,
    value_column: str,
    group_column: str = "isin",
    max_repeats: int = 3,
) -> pd.DataFrame:
    """Return the rows whose value has been unchanged for ``max_repeats`` sessions."""
    raise NotImplementedError("Phase 2: stale quote detection")


def write_manifest(manifest: Manifest, destination: Path) -> None:
    """Write ``manifest.json`` atomically, via a temporary file plus rename.

    Atomic because an interrupted ingestion must not leave a half-written
    provenance record: a truncated manifest would make the whole archive
    unverifiable.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, destination)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_manifest(source: Path) -> Manifest:
    """Read and parse an existing manifest."""
    return Manifest.from_dict(json.loads(source.read_text(encoding="utf-8")))


def register_data_source(
    manifest_path: Path,
    *,
    name: str,
    url: str,
    endpoint_type: EndpointType,
    file_path: str,
    sha256: str,
    row_count: int,
    date_range: DateRange,
) -> None:
    """Append or replace one source entry in the manifest at ``manifest_path``.

    Entries are keyed by ``file_path``: re-ingesting the same file updates its
    hash and row count in place rather than appending a duplicate. Creates the
    manifest if it does not exist yet.
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        manifest = read_manifest(manifest_path)
    except (FileNotFoundError, json.JSONDecodeError, SchemaValidationError):
        manifest = Manifest(generated_at=now)

    record = DataSourceRecord(
        name=name,
        url=url,
        endpoint_type=endpoint_type,
        retrieved_at=now,
        file_path=file_path,
        sha256=sha256,
        row_count=row_count,
        date_range=date_range,
    )
    manifest.data_sources = [s for s in manifest.data_sources if s.file_path != file_path]
    manifest.data_sources.append(record)
    manifest.generated_at = now
    write_manifest(manifest, manifest_path)


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "DataSourceRecord",
    "DateRange",
    "EndpointType",
    "Manifest",
    "SchemaValidationError",
    "ValidationReport",
    "ValidationStatus",
    "detect_stale_quotes",
    "normalize_numeric_column",
    "read_manifest",
    "register_data_source",
    "sha256_bytes",
    "sha256_file",
    "sha256_frame",
    "validate_ibr_series",
    "validate_tes_prices",
    "validate_trm_series",
    "write_manifest",
]
