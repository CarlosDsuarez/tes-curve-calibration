"""Client for datos.gov.co, the Colombian open data portal (Socrata / SODA).

Uses ``sodapy``, the official Socrata Python client, rather than hand-rolled
HTTP: it handles the SoQL query encoding and the paging contract correctly.

Two things to know about the portal:

* The app token is **optional**. Anonymous calls work but share a per-IP
  throttling pool; a registered token raises the limit substantially and is the
  only reason ``SOCRATA_APP_TOKEN`` exists in ``.env``.
* ``get`` is capped per call (1000 rows by default, 50000 maximum), so any
  full-dataset pull must page through with ``$offset`` until a short page comes
  back. :meth:`SocrataClient.fetch_all` is that loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from sodapy import Socrata


class SocrataError(RuntimeError):
    """Raised when the portal rejects a query or returns an unusable payload."""


@dataclass(frozen=True, slots=True)
class SocrataDataset:
    """Identifier of a Socrata dataset."""

    dataset_id: str
    """The four-by-four identifier, e.g. ``"abcd-1234"``."""
    name: str
    description: str = ""


class SocrataClient:
    """Paging wrapper around ``sodapy.Socrata``."""

    def __init__(
        self,
        domain: str = "www.datos.gov.co",
        app_token: str | None = None,
        *,
        timeout: float = 30.0,
        page_size: int = 1000,
    ) -> None:
        self.domain = domain
        self.page_size = page_size
        self._client = Socrata(domain, app_token, timeout=int(timeout))

    def fetch(
        self,
        dataset: SocrataDataset,
        *,
        where: str | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> pd.DataFrame:
        """Run a single SoQL query and return the raw, unvalidated rows."""
        raise NotImplementedError("Phase 2: Socrata single-page fetch")

    def fetch_all(
        self,
        dataset: SocrataDataset,
        *,
        where: str | None = None,
        order: str | None = None,
        max_rows: int | None = None,
    ) -> pd.DataFrame:
        """Page through a dataset until exhausted or ``max_rows`` is reached.

        Paging without a stable ``order`` can duplicate or skip rows, so an
        explicit ordering is applied when the caller does not supply one.
        """
        raise NotImplementedError("Phase 2: Socrata paged fetch")

    def dataset_metadata(self, dataset: SocrataDataset) -> dict[str, object]:
        """Return the portal's metadata record, including its last update time."""
        raise NotImplementedError("Phase 2: Socrata metadata")

    def save_raw(self, frame: pd.DataFrame, destination: Path) -> str:
        """Persist a raw result set and return its SHA-256 hex digest."""
        raise NotImplementedError("Phase 2: raw payload archiving")

    def close(self) -> None:
        """Release the underlying Socrata client."""
        self._client.close()

    def __enter__(self) -> SocrataClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
