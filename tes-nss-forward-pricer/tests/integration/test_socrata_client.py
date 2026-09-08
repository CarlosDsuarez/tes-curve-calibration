"""Live datos.gov.co (Socrata) calls. Deselected by default; see the SUAMECA module."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration]


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_fetch_respects_page_size() -> None:
    """A single `fetch` must not exceed the requested page size."""
    raise NotImplementedError("Phase 2: Socrata paging contract")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_fetch_all_pages_without_duplicates() -> None:
    """Paged retrieval must be duplicate-free under a stable ordering."""
    raise NotImplementedError("Phase 2: Socrata paging correctness")


@pytest.mark.xfail(raises=NotImplementedError, reason="Phase 2", strict=True)
def test_works_without_app_token() -> None:
    """The token is optional: anonymous access must still succeed, if throttled."""
    raise NotImplementedError("Phase 2: anonymous Socrata access")
