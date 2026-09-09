# End-to-end fixture — 14 August 2026

Frozen market snapshot the end-to-end pipeline test replays. Frozen so the test
is reproducible: a test that re-fetches "today" produces a different curve every
morning and can never be a golden run.

The date is not arbitrary. `src/tes_pricer/config/tes_referencia.yaml` carries
`precio_limpio_ref` / `tasa_ref` / `duracion_ref` for all sixteen benchmark TES
**as of 14-ago-2026**, sourced to one primary document. That is the only day for
which this repository holds a per-bond price set with a citation, so it is the
day the pipeline is replayed on.

## Files

| File | Content | Consumed by |
| --- | --- | --- |
| `tes_precios_2026-08-14.csv` | Per-bond TES clean prices and traded yields | `SuamecaClient.load_from_manual_export` |
| `market_snapshot_2026-08-14.yaml` | FX spot, COP and USD short-curve pillars, the forward trade | the test directly |

## Verification tier, by input

| Input | Tier | Source |
| --- | --- | --- |
| TES clean prices (`precio_limpio`) | primary | MinHacienda DGCPTN, *Informe Diario de Deuda Publica*, 14-ago-2026, table *Tasa Fija en Pesos*. Mirrors `precio_limpio_ref` in `tes_referencia.yaml`. |
| TES traded yields (`tasa_negociacion`) | primary | Same report, column *Tasa*. Held out of the pipeline: the pipeline recomputes the yield from the price, and the report's yield is the independent check. |
| TES coupons and maturities | primary | Same report. |
| `precio_sucio` | **absent** | The report publishes clean prices only. The column is left empty rather than back-filled with a number this project computed itself, which would make a derived figure look sourced. |
| FX spot 3127.51 | primary | SFC TRM, datos.gov.co dataset `32sa-8pi3`, validity covering 2026-08-14. Fetched 2026-09-08. |
| IBR pillars | **assumption** | No open primary source. See the header of the snapshot YAML. |
| SOFR pillars | **assumption** | No free term source exists. See the header of the snapshot YAML. |

The two assumption tiers are why the forward assertions test the **sign** of the
forward points against the rate differential and the **internal reconciliation**
of the greeks, and never the level of the forward. Those properties hold for any
COP curve above the USD curve; the specific pillars only set the scale.

## CSV format

Deliberately in the shape a Colombian venue terminal exports: `;` separated,
`,` as the decimal mark, `DD/MM/YYYY` dates, `nemotecnico` rather than `isin`
(TES ISINs are not published in open primary sources). Parsing that shape is
`load_from_manual_export`'s job, and the end-to-end test exercises it rather
than reading the CSV itself.
