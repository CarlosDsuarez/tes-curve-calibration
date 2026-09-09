# TES Curve Calibration

Nelson-Siegel-Svensson calibration on Colombian TES, COP OIS bootstrapping from
IBR, and USD/COP forward pricing under covered interest parity.

> **Status: Phase 1 — scaffolding.** The structure, tooling and contracts are in
> place. Bond pricing functions and the benchmark universe validator are
> implemented; most other numerical functions still raise `NotImplementedError`.
> `src/tes_pricer/config/tes_referencia.yaml` now carries 16 TES tasa fija
> references sourced from the MinHacienda daily public-debt report; two fields
> (`isin`, `fecha_emision`) could not be tied to a primary source and are
> flagged in `metadata.advertencias`, which
> `tes_pricer.data.validators.validate_tes_referencia_yaml` re-reports as
> warnings. Nothing here should be used for production until the benchmark tier
> passes against QuantLib.

## Why the layout looks like this

The one architectural decision everything else follows from: **the numerical
core never touches the network.**

```
tes_pricer.data       →  fetches, validates, hashes, records provenance
       ↓ validated arrays / DataFrames only
tes_pricer.math       →  pure numerics, offline, deterministic
       ↓
tes_pricer.interface  →  CLI and Excel delivery
```

No module under `src/tes_pricer/math/` may import `requests`, `sodapy`, or read
a URL. This is enforced three ways, on purpose, because a convention that is
only written down is a convention that erodes:

1. `ruff`'s `flake8-tidy-imports` banned-api rule, at lint time.
2. `tests/unit/test_architecture.py`, which parses each module's AST.
3. `mypy --strict` on `math/` only, which makes an accidental `Any` from an
   untyped network payload a type error.

The payoff is reproducibility: given `data/manifest.json` and the archived raw
payloads, a calibration re-runs to the same numbers offline, months later.

## Quick start

```bash
poetry install --with dev,test
poetry run pre-commit install
poetry run pytest
```

The default `pytest` run is the offline unit tier: no network, no QuantLib.

## Test tiers

| Marker | Requires | Command |
| --- | --- | --- |
| `unit` | nothing beyond the core deps | `pytest` |
| `integration` | nothing; replays archived fixtures | `pytest -m integration` |
| `benchmark` | QuantLib installed | `pytest -m benchmark` |
| `network` | network + opt-in env var | `TES_PRICER_ALLOW_NETWORK=1 pytest -m network` |

`integration` and `benchmark` are deselected by `addopts`, and skipped with an
explicit reason if selected on a machine that cannot run them. QuantLib is a
referee, not a dependency: nothing under `src/` imports it.

`network` is a separate axis from `integration`, not a synonym for it. An
integration test is one that runs several modules together; the end-to-end
pipeline does exactly that against an archived snapshot, offline and
deterministically, which is what lets CI run it. Only the tests that actually
reach a live endpoint carry `network`, and only those need the opt-in.

Unimplemented functions are covered by tests marked
`xfail(raises=NotImplementedError, strict=True)`. The suite is green now, and
turns red the moment an implementation lands that does not meet the stated
contract.

### The end-to-end run

`tests/integration/test_end_to_end_pipeline.py` is the only place where
ingestion, yield inversion, NSS calibration, the COP and USD short curves, the
USD/COP forward and its greeks run in sequence, in one process, off one
snapshot. That is where the failures *between* modules show up - a day count
that disagrees across two phases, a percent that should have been a decimal, a
curve read at the wrong point of its own axis.

```bash
pytest -m integration tests/integration/test_end_to_end_pipeline.py
```

The snapshot is 14 August 2026, frozen: it is the one day this repository holds
a per-bond price set with a citation. `tests/fixtures/e2e_20260814/PROVENANCE.md`
gives the verification tier of every input, including the two that are scenario
assumptions rather than sourced fixings. A run writes
`reports/e2e_pipeline_report.{html,md}` - what went in, what each stage
produced, and which economic property was asserted about it. CI uploads it as a
build artifact.

One extra test, marked `network`, re-fetches the TRM for the snapshot date from
datos.gov.co and asserts the cached FX spot still equals what the
Superintendencia Financiera publishes. A cached number nobody re-checks against
its source stops being data and becomes folklore.

### Coverage floors

Two, because two tiers of code carry different consequences when they go
untested:

| Scope | Floor | Enforced by |
| --- | --- | --- |
| `src/tes_pricer` | 85% | `fail_under` in `[tool.coverage.report]` |
| `src/tes_pricer/math/` | 95% | `scripts/check_coverage.py` |

The second needs its own script because coverage's `fail_under` is global and
has no per-package form. The numerical core carries the tighter floor: a
silently wrong discount factor does not raise, it prices, and the curve, the
forward and the greeks downstream inherit the error with no symptom.

```bash
pytest -m "not network" --cov=src/tes_pricer --cov-report=json
python scripts/check_coverage.py
```

Both exit non-zero below their floor, and CI runs them in that order.
`raise NotImplementedError` is excluded from the measurement, so an
unimplemented stub is not counted as a missed line - its signature and docstring
still are, which is why a module of pure stubs does not reach 100%.

## Provenance

Every ingestion regenerates `data/manifest.json` (schema `1.0.0`): for each
source, its URL, endpoint type, retrieval timestamp, archived file path,
SHA-256, row count and date range, plus an overall
`validation_status` of `PENDING` / `PASSED` / `FAILED`.

`data/raw/` and `data/processed/` are gitignored. **The manifest is the only
thing about the data that is versioned** — it is the audit trail, and it is what
makes a run reproducible without committing market data.

## Data sources

| Source | Endpoint | Contents |
| --- | --- | --- |
| Banco de la Republica | SUAMECA internal REST (reverse-engineered) | TES zero-coupon curve at 1/5/10y, Nelson-Siegel parameters, bid-ask spreads |
| Manual export | CSV/XLSX from a venue or vendor terminal | per-ISIN TES prices |
| datos.gov.co | Socrata / SODA, via `sodapy` | TRM (USD/COP) daily, dataset `32sa-8pi3` |

> **SUAMECA publishes no per-ISIN TES prices.** Banco de la Republica computes
> its curve from the SEN and MEC tapes and publishes only the fit; the
> underlying quotes are distributed commercially. `fetch_tes_prices()` is
> therefore backed by a manual export and raises rather than returning an empty
> frame when none is configured. It also fits **Nelson-Siegel (1987)**, not
> Svensson, so its published betas are a cross-check, not calibration input.
> Endpoints, payloads and failure modes:
> [docs/suameca_reverse_engineering.md](docs/suameca_reverse_engineering.md)
> (investigated 2026-09-07; these are undocumented SPA internals and can change
> without notice).

> **datos.gov.co publishes no queryable IBR.** Its only IBR asset, `ev8i-uzwt`,
> is an `assetType=href` link stub: no columns, no rows, and HTTP 403
> `no row or column access to non-tabular tables` for any query.
> `SocrataClient.fetch_ibr()` therefore raises, naming the SUAMECA series that do
> carry the fixings (241 overnight nominal, 15324 overnight effective, and the
> 1/3/6/12-month pairs), rather than returning an empty frame. Banco de la
> Republica publishes every tenor **twice**, nominal ACT/360 and effective base
> 365; the market fixing is the nominal one, so the OIS bootstrap must read
> `frame.attrs["convention"]` and reconcile rather than assume.

`SOCRATA_APP_TOKEN` is optional; it raises the rate limit rather than granting
access. Copy `.env.example` to `.env` to configure.

## Conventions

These are the ones that cause a silent two-basis-point break if you get them
wrong, so they are stated once, here, and enforced in code:

- TES tasa fija: annual coupons, **ACT/365**, effective annual yield, T+3.
- IBR: **ACT/360**, daily compounding of the nominal overnight rate.
- Prices are per 100 of face; `dirty = clean + accrued`.
- Rates cross every internal boundary as **decimals**, never percentages.
- USD/COP is quoted **COP per USD**, so USD is the foreign currency in
  Garman-Kohlhagen and CIP.

## Roadmap

| Phase | Scope |
| --- | --- |
| 1 | Scaffolding, tooling, architectural guardrails ✅ |
| 2 | Data clients, validators, manifest provenance |
| 3 | Day count conventions and the Colombian holiday calendar |
| 4 | Bond cash flows, clean/dirty pricing, YTM solver |
| 5 | NSS model evaluation |
| 6 | NSS calibration and diagnostics |
| 7 | IBR OIS bootstrap |
| 8 | FX forwards, Garman-Kohlhagen, greeks |
| 9 | Excel workbook via `xlwings` |
| 10 | QuantLib benchmark tier |

## Development

```bash
poetry run ruff check .
poetry run ruff format .
poetry run mypy
poetry run pytest --cov
```

`mypy` is strict on `tes_pricer.math` and pragmatic elsewhere: the numerical
core is where a wrong type becomes a wrong price.

## Licence

MIT.
