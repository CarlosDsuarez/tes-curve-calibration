# SUAMECA / Banco de la República — endpoint reverse engineering

**Investigation date: 2026-09-07.** Performed by inspecting the Network tab of the
Angular front-end at `https://suameca.banrep.gov.co` and reading its compiled
JavaScript bundles. Everything below is a public, unauthenticated, read-only
statistics endpoint.

> **These are internal endpoints of a single-page app. They are undocumented and
> can change without notice.** Re-run this investigation if
> `SuamecaClient.health_check()` starts failing. The date above is the last time
> the findings were verified end to end.

---

## 0. Headline finding — read this before writing pricing code

**SUAMECA does not publish per-ISIN TES prices.** There is no series anywhere in
the catalogue carrying `precio_sucio`, `precio_limpio` or `tasa_negociacion` for
an individual bond. The complete TES inventory is 16 daily series (§3): a fitted
zero-coupon curve at three fixed tenors, the four fitted model parameters, and
two bid-ask spread series.

The portal states the reason itself, in the `notas` field of every TES series:

> Fuente: SEN y MEC, con cálculos Banco de la República.

Banco de la República *consumes* the SEN and MEC trade tapes and publishes only
the **fitted curve**. The underlying per-ISIN quotes are the property of the
trading venues and are distributed commercially (Precia/Infovalmer, BVC), not by
the central bank.

**Consequence for this project:** the `fetch_tes_prices(bond_isin, ...)` contract
cannot be served over the network from SUAMECA, by any endpoint, at any date
range. It is served only by `load_from_manual_export()`. This is a real
limitation of the data source, not a gap in the implementation.

A second, subtler finding: **BanRep fits Nelson-Siegel (1987), not
Nelson-Siegel-Svensson.** Three betas and one tau, per the `notas` field:

> ...que calcula el Banco de la República mediante la metodología de Nelson y
> Siegel (1987).

So the published betas are **not** directly comparable to this project's
six-parameter NSS calibration. Treat them as an independent 4-parameter
reference, not as a ground truth for our `NSSParams`.

---

## 1. Hosts and services

`https://www.banrep.gov.co/es/estadisticas` redirects to
`https://suameca.banrep.gov.co`. Two REST services back the SPAs:

| Base | Used by |
| --- | --- |
| `https://suameca.banrep.gov.co/estadisticas-economicas-back/rest/estadisticaEconomicaRestService` | main portal — **carries the observations** |
| `https://suameca.banrep.gov.co/buscador-de-series/rest/buscadorSeriesRestService` | series search / bulk download — **metadata only** |

Discovered by grepping the bundles
`/estadisticas-economicas/main-JJO7UXF5.js` and
`/descarga-multiple-de-datos/main-XTCY3WHB.js` (filenames are content-hashed and
will change on redeploy).

The interactive dashboards are **Oracle Analytics Cloud** embeds
(`URL_ORACLE_JAVASCRIPT`, `.../oac-token/...-oac-authtoken`) and require a
session token. They were not used and are not worth pursuing: the plain REST
endpoint below returns the same numbers without authentication.

## 2. The endpoint that actually returns observations

```
GET /estadisticas-economicas-back/rest/estadisticaEconomicaRestService/consultaInformacionSerieXTipoDato
    ?idSerie={int}&tipoDato=1
```

Headers: none required. No cookie, no token, no referer check. Send
`Accept: application/json`.

Response: a **JSON array with exactly one object**. Metadata fields plus:

```jsonc
"data": [ [1041483600000, 0.06], [1041570000000, 0.04], ... ]
```

Each element is `[epoch_millis, value]`. Timestamps are **midnight in
America/Bogota (UTC-05:00, no DST)**, so `1041483600000` is `2003-01-02`.
Convert by localising to `America/Bogota` and taking the date — never with
`utcfromtimestamp`, which lands on the previous day at 19:00.

**The endpoint ignores date ranges.** It has no date parameters and returns the
entire history (5758 daily points for the TES series). Filter client-side.

### Endpoints that look right but are not

| Endpoint | Why it is useless |
| --- | --- |
| `consultaInformacionSerieXTipoDatoXFechaDesde?idSerie=&tipoDato=&cantDatos=&frecuenciaDatos=` | Returns metadata with `"data": []` for every combination of `tipoDato ∈ {0,1,12,18}` × `frecuenciaDatos ∈ {0,1,12}` tried. |
| `POST /buscador-de-series/.../consultaDatosSeries` body `{"series":[{"idSerie":15278,"idPeriodicidades":[1]}],"fechaInicio":<millis>,"fechaFin":<millis>}` | Accepts the request (200) and echoes metadata, but `data` is always `[]`. Dates **must** be epoch millis; ISO strings give `500 "Error al consultar los datos de las series."` |
| `consultaSerieParaGrafica?idSerie=` | `404`, HTML error page. |
| `listarSeriesXCategoria`, `consultaListadoSeries?series=`, `busquedaPalabraClave?palabraClave=` | Metadata only. Useful for discovery (§3), never for values. |

## 3. Complete TES series inventory

Verified against `listarSeriesXCategoria` on 2026-09-07. All daily, all starting
`2003-01-02`, all ending `2026-09-04` with 5758 observations.

| idSerie | Series | Unit |
| --- | --- | --- |
| 15272 | Cero Cupón TES **pesos** — 1 año | Porcentaje |
| 15273 | Cero Cupón TES **pesos** — 5 años | Porcentaje |
| 15274 | Cero Cupón TES **pesos** — 10 años | Porcentaje |
| 15275 | Cero Cupón TES **UVR** — 1 año | Porcentaje |
| 15276 | Cero Cupón TES **UVR** — 5 años | Porcentaje |
| 15277 | Cero Cupón TES **UVR** — 10 años | Porcentaje |
| 15278 | Beta TES pesos — B0 | Unidades |
| 15279 | Beta TES pesos — B1 | Unidades |
| 15280 | Beta TES pesos — B2 | Unidades |
| 15281 | Beta TES pesos — Tau | Unidades |
| 15282–15285 | Beta TES UVR — B0, B1, B2, Tau | Unidades |
| 16720 | BID-ASK Spread TES Pesos (from 2012-12-26) | COP |
| 16721 | BID-ASK Spread TES UVR (from 2012-12-26) | COP/UVR |

Catalogue plan id: `BETAS_TASAS_TES`. Category path: *Tasas de interés y sector
financiero → Tasas de interés → Tasas, Títulos de Tesorería (TES)*.

### Sample payload — first 5 rows, series 15274 (Cero Cupón pesos 10 años)

```jsonc
[{
  "id": 15274,
  "nombre": "Tasa de interés Cero Cupón, Títulos de Tesorería (TES), pesos - 10 años",
  "unidad": "Porcentaje",
  "numeroDecimales": 2,
  "fuente": "SEN y MEC, con cálculos Banco de la República.",
  "isSerie": "SI",
  "data": [
    [1041483600000, 14.69],   // 2003-01-02
    [1041570000000, 14.47],   // 2003-01-03
    [1041915600000, 15.57],   // 2003-01-07
    [1042002000000, 14.63],   // 2003-01-08
    [1042088400000, 15.54]    // 2003-01-09
  ]
}]
```

Tail of the same series: `2026-09-02 → 12.73`, `2026-09-03 → 12.58`,
`2026-09-04 → 12.47`.

### Units trap: the betas are decimals, the zero rates are percent

On 2026-09-04 the published values are `B0=0.12, B1=0.00, B2=0.01, Tau=3.7`
while the zero rates are `1Y=12.17, 5Y=12.40, 10Y=12.47` **percent**. The betas
are decimals, and `numeroDecimales=2` means they are **rounded to two decimal
places** — i.e. to the nearest whole percentage point.

Reconstructing the curve from the published betas therefore reproduces the
published zero rates only to roughly ±20bp (checked at 1Y and 10Y). **Do not use
the beta series to rebuild a curve.** Use series 15272–15277 for cross-checking,
and treat the betas as indicative only.

## 4. Failure modes — all three verified against the live host

These are why `SuamecaClient` validates the transport before it trusts a body.

1. **200 + `text/html`.** `GET /estadisticas-economicas/tasas_interes_cero_cupon_tes`
   returns **HTTP 200** with `Content-Type: text/html;charset=UTF-8` and a 10021-byte
   Angular shell. Any path typo inside the SPA lands here. A parser that does not
   check the content type will see "success" and then find no rows — the most
   dangerous silent failure in the project.

2. **200 + `application/json` + sentinel empty record.** An unknown series id
   does *not* 404:

   ```
   GET .../consultaInformacionSerieXTipoDato?idSerie=99999999&tipoDato=1
   → 200 application/json
   [{"idPeriodicidad": 0, "valor": 0.0, "isSerie": "NO", "tieneHijos": "NO"}]
   ```

   The tell is **`isSerie: "NO"`** and the absence of a `data` key. Checking the
   content type alone is not enough; this record must be rejected explicitly.

3. **500 + `text/html`.** Omitting `idSerie` returns a Tomcat
   `HTTP Status 500 – Internal Server Error` page. `/robots.txt` is a 404 HTML
   page, so there is no crawl policy published for this host.

## 5. Manual export fallback

Per-ISIN prices (§0) must come from outside SUAMECA. `load_from_manual_export()`
ingests a CSV or XLSX exported from a venue or vendor terminal, applies the same
schema validation as the network path, and records the file in the manifest with
its SHA-256 so a calibration remains reproducible. Expected source columns and
the Colombian locale handling (`dd/mm/yyyy` dates, `.` thousands separator, `,`
decimal separator) are documented in the function's docstring.

## 6. Reproducing this investigation

```javascript
// Series discovery (metadata only)
await (await fetch('https://suameca.banrep.gov.co/buscador-de-series/rest/buscadorSeriesRestService/listarSeriesXCategoria')).json();

// Observations
await (await fetch('https://suameca.banrep.gov.co/estadisticas-economicas-back/rest/estadisticaEconomicaRestService/consultaInformacionSerieXTipoDato?idSerie=15274&tipoDato=1')).json();
```
