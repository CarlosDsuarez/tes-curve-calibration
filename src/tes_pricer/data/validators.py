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

It also owns the static side of the gate: ``validate_tes_referencia_yaml``
checks the benchmark bond universe in ``config/tes_referencia.yaml`` before
it is ever turned into calibration instruments.

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

import jsonschema
import pandas as pd
import yaml

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


# --------------------------------------------------------------------------- #
# Benchmark reference config (config/tes_referencia.yaml)
# --------------------------------------------------------------------------- #

TRAMOS = ("corto", "medio", "largo")
"""Curve buckets. NSS needs all three populated to identify its six parameters."""

MIN_TRAMOS_FOR_STABLE_NSS = 3
"""Below this many distinct buckets the two-hump NSS fit is not identified."""

TES_REFERENCIA_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "tes_referencia",
    "type": "object",
    "required": ["bonos_benchmark", "metadata"],
    "properties": {
        "schema_version": {"type": "string"},
        "bonos_benchmark": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": [
                    "isin",
                    "nombre_corto",
                    "fecha_emision",
                    "fecha_vencimiento",
                    "tasa_cupon_nominal",
                    "frecuencia_cupon",
                    "valor_nominal",
                    "day_count_convention",
                    "fuente_verificacion",
                    "fecha_verificacion",
                    "tramo_curva",
                ],
                "properties": {
                    # Null is legal and load-bearing: MinHacienda and BanRep do
                    # not publish TES ISINs in the open, so `nemotecnico` is the
                    # authoritative identifier and a fabricated ISIN would be
                    # worse than an absent one.
                    "isin": {"type": ["string", "null"], "pattern": "^[A-Z]{2}[A-Z0-9]{9}[0-9]$"},
                    "nemotecnico": {"type": "string", "minLength": 1},
                    "nombre_corto": {"type": "string", "minLength": 1},
                    "fecha_emision": {"type": "string", "format": "date"},
                    "fecha_vencimiento": {"type": "string", "format": "date"},
                    # Decimal, not percent: 0.0725 for a 7.25% coupon.
                    "tasa_cupon_nominal": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "frecuencia_cupon": {"enum": ["anual", "semestral"]},
                    "valor_nominal": {"type": "number", "exclusiveMinimum": 0.0},
                    "day_count_convention": {"enum": ["ACT/365", "ACT/360", "30/360"]},
                    "fuente_verificacion": {"type": "string", "minLength": 1},
                    "fecha_verificacion": {"type": "string", "format": "date"},
                    "tramo_curva": {"enum": list(TRAMOS)},
                    "campos_no_verificados": {"type": "array", "items": {"type": "string"}},
                    "saldo_vigente_cop_mm": {"type": "number", "minimum": 0.0},
                    "precio_limpio_ref": {"type": "number", "exclusiveMinimum": 0.0},
                    "tasa_ref": {"type": "number"},
                    "duracion_ref": {"type": "number", "minimum": 0.0},
                    "fuente_precio": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "metadata": {
            "type": "object",
            "required": ["fecha_construccion", "numero_bonos", "advertencias"],
            "properties": {
                "fecha_construccion": {"type": "string", "format": "date"},
                "numero_bonos": {"type": "integer", "minimum": 0},
                "cobertura_temporal_minima": {"type": "string"},
                "cobertura_temporal_maxima": {"type": "string"},
                "fecha_referencia_tramos": {"type": "string", "format": "date"},
                "fecha_corte_datos_mercado": {"type": "string", "format": "date"},
                "definicion_tramos": {"type": "object"},
                "fuentes": {"type": "object"},
                "advertencias": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


def _bucket_for(years: float) -> str:
    """Return the curve bucket a maturity in ``years`` belongs to."""
    if years < 3.0:
        return "corto"
    if years <= 10.0:
        return "medio"
    return "largo"


def _schema_errors(payload: object) -> list[str]:
    """Return every JSON Schema violation, deepest path first, as flat strings."""
    validator = jsonschema.Draft202012Validator(
        TES_REFERENCIA_SCHEMA,
        format_checker=jsonschema.FormatChecker(),
    )
    return [
        f"schema: {'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"{error.message}"
        for error in sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    ]


def validate_tes_referencia_yaml(filepath: str) -> ValidationReport:
    """Validate the benchmark TES universe in ``config/tes_referencia.yaml``.

    Four checks are mandatory, in this order, because each one presumes the
    previous one held:

    1. The file parses as YAML and satisfies :data:`TES_REFERENCIA_SCHEMA`.
    2. ``fecha_vencimiento`` is strictly after ``fecha_emision`` on every bond.
    3. No identifier repeats -- neither ``isin`` (ignoring nulls, which are
       legitimately absent) nor ``nemotecnico``. A duplicate would double-weight
       one point of the curve in the least-squares objective.
    4. At least :data:`MIN_TRAMOS_FOR_STABLE_NSS` distinct ``tramo_curva``
       values appear. Falling short is a **warning**, not an error: the file is
       well-formed, but a universe bunched into one bucket cannot separate the
       two NSS hump terms, and the solver will return whatever the optimiser
       wandered into rather than a fit.

    Anything that makes the numbers untrustworthy is an error and sets
    ``FAILED``; anything that makes them *unstable* or merely unverified is a
    warning and still passes.

    Args:
        filepath: Path to the reference YAML.

    Returns:
        A :class:`ValidationReport` whose ``row_count`` is the number of bonds
        parsed (0 when the file could not be read or parsed at all).
    """
    path = Path(filepath)
    errors: list[str] = []
    warnings: list[str] = []

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ValidationReport(ValidationStatus.FAILED, 0, (f"cannot read {filepath}: {exc}",))

    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return ValidationReport(ValidationStatus.FAILED, 0, (f"YAML parse error: {exc}",))

    if not isinstance(payload, dict):
        return ValidationReport(
            ValidationStatus.FAILED, 0, ("top level of the YAML must be a mapping",)
        )

    # (1) JSON Schema. Every later check reads fields by name, so a schema
    # failure short-circuits: reporting "fecha_vencimiento missing" twice, once
    # as a schema error and once as a date error, is noise.
    schema_errors = _schema_errors(payload)
    bonds = payload.get("bonos_benchmark")
    if not isinstance(bonds, list):
        bonds = []
    if schema_errors:
        return ValidationReport(ValidationStatus.FAILED, len(bonds), tuple(schema_errors))

    metadata = payload.get("metadata", {})

    # Bucket labels are checked against a stated reference date, not against
    # "now": a maturity drifts across a bucket boundary as time passes, and the
    # file is a dated snapshot, not a live view.
    ref_raw = metadata.get("fecha_referencia_tramos") or metadata.get("fecha_construccion")
    reference_date = date.fromisoformat(str(ref_raw))

    seen_isin: dict[str, int] = {}
    seen_nemo: dict[str, int] = {}
    tramos_present: set[str] = set()
    maturities_years: list[float] = []
    # Grouped by field rather than by bond: on a universe where every entry
    # shares the same gap, one warning per bond buries the checks that matter.
    unverified_by_field: dict[str, list[str]] = {}

    for index, bond in enumerate(bonds):
        label = bond.get("nemotecnico") or bond.get("isin") or bond.get("nombre_corto") or index
        issue = date.fromisoformat(bond["fecha_emision"])
        maturity = date.fromisoformat(bond["fecha_vencimiento"])

        # (2) Ordering of the two dates.
        if maturity <= issue:
            errors.append(
                f"{label}: fecha_vencimiento ({maturity.isoformat()}) must be strictly after "
                f"fecha_emision ({issue.isoformat()})"
            )

        # (3) Duplicate identifiers. A null ISIN is absence, not a collision.
        isin = bond.get("isin")
        if isin is not None:
            if isin in seen_isin:
                errors.append(f"duplicate isin {isin!r} at entries {seen_isin[isin]} and {index}")
            else:
                seen_isin[isin] = index
        nemo = bond.get("nemotecnico")
        if nemo is not None:
            if nemo in seen_nemo:
                errors.append(
                    f"duplicate nemotecnico {nemo!r} at entries {seen_nemo[nemo]} and {index}"
                )
            else:
                seen_nemo[nemo] = index

        years = (maturity - reference_date).days / 365.25
        maturities_years.append(years)
        tramo = bond["tramo_curva"]
        tramos_present.add(tramo)

        if maturity <= reference_date:
            errors.append(
                f"{label}: already matured on the reference date {reference_date.isoformat()}"
            )
        elif tramo != _bucket_for(years):
            warnings.append(
                f"{label}: tramo_curva is {tramo!r} but {years:.2f}y from "
                f"{reference_date.isoformat()} falls in {_bucket_for(years)!r}"
            )

        for field_name in bond.get("campos_no_verificados") or []:
            unverified_by_field.setdefault(field_name, []).append(str(label))

    for field_name, affected in sorted(unverified_by_field.items()):
        scope = (
            "every bond"
            if len(affected) == len(bonds)
            else f"{len(affected)} bond(s): {', '.join(affected)}"
        )
        warnings.append(
            f"{field_name!r} is not verified against a primary source on {scope}; "
            "see metadata.advertencias"
        )

    # (4) Curve coverage. The whole point of the universe is spread.
    if len(tramos_present) < MIN_TRAMOS_FOR_STABLE_NSS:
        warnings.append(
            f"only {len(tramos_present)} distinct tramo(s) present "
            f"({sorted(tramos_present)}); NSS calibration needs at least "
            f"{MIN_TRAMOS_FOR_STABLE_NSS} ({list(TRAMOS)}) to identify beta0, beta1, beta2, "
            "beta3, lambda1 and lambda2 -- expect an ill-conditioned, numerically unstable fit"
        )
    missing = [t for t in TRAMOS if t not in tramos_present]
    if missing and len(tramos_present) >= MIN_TRAMOS_FOR_STABLE_NSS:  # pragma: no cover
        warnings.append(f"tramos with no instruments: {missing}")

    declared = metadata.get("numero_bonos")
    if declared is not None and declared != len(bonds):
        errors.append(
            f"metadata.numero_bonos is {declared} but bonos_benchmark holds {len(bonds)} entries"
        )

    if not metadata.get("advertencias") and any(
        bond.get("campos_no_verificados") for bond in bonds
    ):
        warnings.append(
            "bonds carry campos_no_verificados but metadata.advertencias is empty; "
            "every unverified field must be spelled out there"
        )

    if maturities_years:
        span = max(maturities_years) - min(maturities_years)
        if span < 5.0:
            warnings.append(
                f"maturity span is only {span:.2f}y; too little dispersion for a stable NSS fit"
            )

    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    return ValidationReport(status, len(bonds), tuple(errors), tuple(warnings))


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "MIN_TRAMOS_FOR_STABLE_NSS",
    "TES_REFERENCIA_SCHEMA",
    "TRAMOS",
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
    "validate_tes_referencia_yaml",
    "validate_trm_series",
    "write_manifest",
]
