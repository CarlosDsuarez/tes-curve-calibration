"""Persistence of a calibration run for the Excel bridge.

The bridge must never calibrate inside a cell formula, so a finished run is
written once to ``data/processed`` and read back on every call. These tests
pin the round trip through JSON, the two files a save produces (dated and
``latest``), and the lookup rules the UDFs rely on.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from tes_pricer.interface import calibration_cache as cache
from tes_pricer.interface.calibration_cache import (
    LATEST_FILENAME,
    SCHEMA_VERSION,
    CalibrationSnapshot,
    dated_filename,
    load_snapshot,
    resolve_snapshot_path,
    save_snapshot,
)

pytestmark = pytest.mark.unit


def _assert_snapshots_equal(left: CalibrationSnapshot, right: CalibrationSnapshot) -> None:
    assert left.calibration_date == right.calibration_date
    assert left.generated_at == right.generated_at
    assert left.params == right.params
    assert left.diagnostics == right.diagnostics
    for name in ("cop_curve", "usd_curve"):
        a, b = getattr(left, name), getattr(right, name)
        assert np.array_equal(a.tenors_years, b.tenors_years)
        assert np.array_equal(a.rates, b.rates)
        assert a.currency == b.currency
        assert a.curve_date == b.curve_date
    assert left.fx_spot_cop_per_usd == right.fx_spot_cop_per_usd
    assert left.bonds == right.bonds
    assert left.aliases == right.aliases
    assert left.sources == right.sources


# --------------------------------------------------------------------------- #
# JSON round trip
# --------------------------------------------------------------------------- #
def test_to_dict_is_plain_json(sample_calibration_snapshot: CalibrationSnapshot) -> None:
    payload = sample_calibration_snapshot.to_dict()
    text = json.dumps(payload)  # raises if anything is not JSON-serialisable
    assert json.loads(text)["schema_version"] == SCHEMA_VERSION
    assert json.loads(text)["calibration_date"] == "2026-03-16"


def test_from_dict_inverts_to_dict(sample_calibration_snapshot: CalibrationSnapshot) -> None:
    rebuilt = CalibrationSnapshot.from_dict(sample_calibration_snapshot.to_dict())
    _assert_snapshots_equal(rebuilt, sample_calibration_snapshot)


def test_from_dict_rejects_an_unknown_schema_version(
    sample_calibration_snapshot: CalibrationSnapshot,
) -> None:
    payload = sample_calibration_snapshot.to_dict()
    payload["schema_version"] = "99.0.0"
    with pytest.raises(ValueError, match="schema_version"):
        CalibrationSnapshot.from_dict(payload)


def test_bond_terms_survive_the_round_trip_with_their_conventions(
    sample_calibration_snapshot: CalibrationSnapshot,
) -> None:
    payload = sample_calibration_snapshot.to_dict()
    entry = payload["bonds"]["TEST00000001"]
    assert entry["maturity_date"] == "2030-03-16"
    assert entry["coupon_rate"] == 0.07
    assert entry["coupon_frequency"] == 1
    assert entry["day_count"] == "ACT/365"
    rebuilt = CalibrationSnapshot.from_dict(payload)
    assert rebuilt.bonds["TEST00000001"] == sample_calibration_snapshot.bonds["TEST00000001"]


# --------------------------------------------------------------------------- #
# Files on disk
# --------------------------------------------------------------------------- #
def test_save_writes_a_dated_file_and_latest(
    sample_calibration_snapshot: CalibrationSnapshot, tmp_path: Path
) -> None:
    dated, latest = save_snapshot(sample_calibration_snapshot, tmp_path)
    assert dated == tmp_path / dated_filename(date(2026, 3, 16))
    assert latest == tmp_path / LATEST_FILENAME
    assert dated.read_text(encoding="utf-8") == latest.read_text(encoding="utf-8")


def test_save_creates_the_directory(
    sample_calibration_snapshot: CalibrationSnapshot, tmp_path: Path
) -> None:
    target = tmp_path / "nested" / "processed"
    save_snapshot(sample_calibration_snapshot, target)
    assert (target / LATEST_FILENAME).is_file()


def test_load_returns_what_was_saved(
    sample_calibration_snapshot: CalibrationSnapshot, tmp_path: Path
) -> None:
    _, latest = save_snapshot(sample_calibration_snapshot, tmp_path)
    _assert_snapshots_equal(load_snapshot(latest), sample_calibration_snapshot)


def test_resolve_defaults_to_latest(tmp_path: Path) -> None:
    assert resolve_snapshot_path(tmp_path, None) == tmp_path / LATEST_FILENAME


def test_resolve_picks_the_dated_file_for_a_date(tmp_path: Path) -> None:
    assert resolve_snapshot_path(tmp_path, date(2026, 8, 14)) == (
        tmp_path / "calibration_2026-08-14.json"
    )


def test_load_missing_file_names_the_command_that_creates_it(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="calibration_cache"):
        load_snapshot(tmp_path / LATEST_FILENAME)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def test_main_writes_the_snapshot_to_the_output_directory(
    sample_calibration_snapshot: CalibrationSnapshot,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI is the one-shot the setup guide tells users to run; only its
    plumbing is tested here, the calibration itself is the integration tier's."""
    captured: dict[str, object] = {}

    def fake_build(
        prices_path: Path,
        market_snapshot_path: Path,
        reference_path: Path,
        settlement_date: date,
        **kwargs: object,
    ) -> CalibrationSnapshot:
        captured["settlement_date"] = settlement_date
        captured["prices_path"] = prices_path
        return sample_calibration_snapshot

    monkeypatch.setattr(cache, "build_snapshot", fake_build)
    exit_code = cache.main(["--date", "2026-03-16", "--output", str(tmp_path), "--prices", "x.csv"])
    assert exit_code == 0
    assert captured["settlement_date"] == date(2026, 3, 16)
    assert captured["prices_path"] == Path("x.csv")
    assert (tmp_path / LATEST_FILENAME).is_file()
    assert (tmp_path / "calibration_2026-03-16.json").is_file()
