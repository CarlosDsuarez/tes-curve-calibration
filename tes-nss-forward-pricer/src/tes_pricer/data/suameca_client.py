"""Client for the Banco de la Republica SUAMECA series service.

SUAMECA exposes Banco de la Republica's statistical series (TES zero-coupon
reference curve, IBR fixings, TRM) over a JSON POST endpoint that takes a series
identifier and a date range.

Operational notes that shape this client:

* The service is unauthenticated but rate-limited and occasionally slow; every
  call goes through a retrying :class:`requests.Session` with an explicit
  timeout, never a bare ``requests.get``.
* Responses are returned **raw**. Schema validation is
  :mod:`tes_pricer.data.validators`' job, and nothing reaches
  ``tes_pricer.math`` without passing through it.
* Every successful fetch is recorded in the manifest with its SHA-256, so a
  calibration can be reproduced from the archived payload alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests


class SuamecaError(RuntimeError):
    """Raised when SUAMECA returns an error payload or an unusable response."""


@dataclass(frozen=True, slots=True)
class SuamecaSeries:
    """Identifier of a single SUAMECA series."""

    series_id: str
    name: str
    description: str = ""


class SuamecaClient:
    """Thin, retrying HTTP client for the SUAMECA series service."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session if session is not None else requests.Session()

    def fetch_series(
        self,
        series: SuamecaSeries,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch one series over ``[start, end]`` and return it unvalidated.

        Raises:
            SuamecaError: On a non-200 response, an error payload, or a body
                that does not parse as the expected JSON envelope.
        """
        raise NotImplementedError("Phase 2: SUAMECA series fetch")

    def fetch_tes_zero_curve(self, start: date, end: date) -> pd.DataFrame:
        """Fetch the published TES zero-coupon reference curve.

        Used as an independent cross-check on the in-house NSS calibration, not
        as its input.
        """
        raise NotImplementedError("Phase 2: TES zero curve fetch")

    def fetch_ibr_overnight(self, start: date, end: date) -> pd.DataFrame:
        """Fetch the IBR overnight nominal fixings."""
        raise NotImplementedError("Phase 2: IBR fixings fetch")

    def fetch_trm(self, start: date, end: date) -> pd.DataFrame:
        """Fetch the official USD/COP representative market rate (TRM)."""
        raise NotImplementedError("Phase 2: TRM fetch")

    def save_raw(self, payload: bytes, destination: Path) -> str:
        """Persist a raw payload and return its SHA-256 hex digest."""
        raise NotImplementedError("Phase 2: raw payload archiving")

    def close(self) -> None:
        """Release the underlying HTTP session."""
        self._session.close()

    def __enter__(self) -> SuamecaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
