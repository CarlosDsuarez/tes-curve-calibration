# Proyecto 1 — Ejecución Completa: Curva Cero Cupón TES (Nelson-Siegel-Svensson) y Pricer de Forwards USD/COP

**Documento maestro de ejecución por prompts secuenciales.** Diseñado para ejecutarse con Claude Code (o Claude conversacional) prompt por prompt, en orden, dentro de una sesión de desarrollo continua. Cada prompt es autocontenido: incluye teoría financiera exacta, fórmulas, supuestos explícitos, y criterios de aceptación verificables.

**Convención de uso:** copia cada bloque de prompt (delimitado por ` ``` `) tal cual en tu sesión de Claude Code. No avances al siguiente prompt hasta que el criterio de aceptación del actual esté satisfecho — cada fase depende de artefactos generados en la anterior.

**Etiquetado epistémico:** [D] = dato/hecho verificable, [I] = inferencia lógica, [J] = juicio de diseño. Se usa en las notas técnicas de este documento, no dentro del código generado.

---

## Índice de Fases

| Fase | Prompt | Entregable | Duración estimada |
|---|---|---|---|
| 0 | Setup y arquitectura | Estructura de repo + manifest.json | 1-2 horas |
| 1 | Ingesta de datos SUAMECA/Socrata | `raw_data/` con provenance SHA256 | 3-4 horas |
| 2 | Construcción de precios sucios/limpios TES | `tes_universe.py` + `tes_referencia.yaml` validado | 4-6 horas |
| 3 | Teoría y calibración NSS | `nss_calibration.py` + tests | 6-8 horas |
| 4 | Validación y benchmark QuantLib | `test_nss_benchmark.py` | 3-4 horas |
| 5 | Curva OIS/IBR para descuento de forwards | `ois_curve.py` | 3-4 horas |
| 6 | Pricer Forward USD/COP (Garman-Kohlhagen / CIP) | `fx_forward_pricer.py` | 4-5 horas |
| 7 | Griegas y matriz de sensibilidades | `greeks.py` + tests | 3-4 horas |
| 8 | Suite de tests pytest completa | `tests/` con cobertura >85% | 4-5 horas |
| 9 | Integración VBA/Excel vía xlwings | `tes_toolkit.xlsm` + UDFs | 5-6 horas |
| 10 | Documentación y narrativa de entrevista | `README.md` + `NARRATIVE.md` | 2-3 horas |

**Total estimado:** 38-50 horas de desarrollo efectivo (~2-3 semanas a ritmo de tiempo parcial).

---

## FASE 0 — Setup y Arquitectura del Repositorio

### Teoría y racional de diseño

Antes de escribir una sola línea de matemática financiera, el sistema necesita una arquitectura que separe responsabilidades. Basado en el patrón de 3 capas de OptimalPortfolios (Artur Sepp) adaptado a este dominio:

- **Capa 1 — Datos (Data Layer):** ingesta, limpieza, validación, provenance. Nunca contiene lógica de pricing.
- **Capa 2 — Matemática (Math Layer):** calibración NSS, bootstrapping OIS, pricing de forwards, cálculo de griegas. Funciones puras, sin I/O.
- **Capa 3 — Interfaz (Interface Layer):** CLI, exportación a Excel/VBA, reportes.

Esta separación es lo que distingue un sistema production-grade de un notebook: la Capa 2 debe poder testearse con datos sintéticos sin tocar ninguna API externa, y la Capa 1 debe poder fallar (endpoint caído, dato corrupto) sin que la Capa 2 lo note — porque nunca ve datos crudos, solo datos ya validados.

### Prompt 0.1 — Estructura del repositorio

```
Actúa como un ingeniero de software cuantitativo senior configurando un repositorio 
de nivel producción para un proyecto de pricing de renta fija y derivados FX.

Crea la siguiente estructura de directorios y archivos base para un proyecto llamado 
`tes-nss-forward-pricer`:

tes-nss-forward-pricer/
├── README.md
├── pyproject.toml              # usa poetry o hatch, Python >=3.11,<3.13
├── manifest.json               # ver especificación abajo
├── .env.example
├── .gitignore
├── src/
│   └── tes_pricer/
│       ├── __init__.py
│       ├── data/
│       │   ├── __init__.py
│       │   ├── suameca_client.py      # cliente para Banco de la República SUAMECA
│       │   ├── socrata_client.py      # cliente para datos.gov.co
│       │   └── validators.py          # validación de esquemas de datos crudos
│       ├── math/
│       │   ├── __init__.py
│       │   ├── day_count.py           # convenciones de conteo de días
│       │   ├── bond_pricing.py        # precio sucio/limpio, YTM
│       │   ├── nss_model.py           # modelo Nelson-Siegel-Svensson puro
│       │   ├── calibration.py         # optimización least_squares
│       │   ├── ois_bootstrap.py       # curva OIS/IBR
│       │   ├── fx_forward.py          # pricing Garman-Kohlhagen/CIP
│       │   └── greeks.py              # delta, gamma, DV01
│       ├── interface/
│       │   ├── __init__.py
│       │   ├── cli.py
│       │   └── excel_bridge.py        # funciones expuestas a xlwings
│       └── config/
│           └── tes_referencia.yaml    # bonos benchmark (a completar en Fase 2)
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   └── benchmark/                     # comparación contra QuantLib
├── data/
│   ├── raw/                           # nunca versionado en git salvo manifest
│   ├── processed/
│   └── manifest.json                  # se regenera con cada ingesta
├── excel/
│   └── tes_toolkit.xlsm               # placeholder, se construye en Fase 9
└── scripts/
    └── run_daily_calibration.py       # entry point operativo

ESPECIFICACIÓN de manifest.json (provenance tracking):
{
  "generated_at": "<ISO 8601 timestamp>",
  "data_sources": [
    {
      "name": "string",
      "url": "string",
      "endpoint_type": "SUAMECA | Socrata | manual",
      "retrieved_at": "<ISO 8601>",
      "file_path": "string",
      "sha256": "string",
      "row_count": "int",
      "date_range": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
    }
  ],
  "validation_status": "PENDING | PASSED | FAILED",
  "schema_version": "1.0.0"
}

REQUISITOS NO NEGOCIABLES:
- Ningún módulo en `math/` debe importar `requests`, `pandas.read_csv` desde URL, 
  ni ningún cliente de red. Debe recibir arrays/DataFrames ya validados como argumentos.
- Configura pytest con markers: `unit`, `integration` (requiere red), `benchmark` 
  (requiere QuantLib instalado). Los tests `unit` deben poder correr sin conexión 
  a internet y sin QuantLib instalado.
- Incluye pre-commit hooks: ruff (lint + format), mypy (type checking estricto en 
  `math/`).
- El .env.example debe incluir placeholders para: SUAMECA_BASE_URL, SOCRATA_APP_TOKEN 
  (opcional, aumenta rate limit), DATOS_GOV_CO_DATASET_ID.

Genera todos los archivos con contenido mínimo funcional (no vacío) — cada __init__.py 
con docstring de módulo, cada archivo de math/ con la firma de función esperada y un 
NotImplementedError, pyproject.toml con dependencias reales: numpy, scipy, pandas, 
pyyaml, requests, sodapy (cliente oficial Socrata), xlwings, pytest, pytest-cov, 
QuantLib-Python (como dependencia opcional bajo grupo [test]), ruff, mypy.

Al finalizar, corre `pytest --collect-only` y confirma que la estructura de tests 
es descubrible aunque los tests aún no tengan lógica.
```

**Criterio de aceptación:** `pytest --collect-only` corre sin errores de import; `ruff check .` pasa; estructura de carpetas coincide exactamente con la especificación.

---

## FASE 1 — Ingesta de Datos: SUAMECA y Socrata

### Teoría: por qué esta fase es el mayor riesgo del proyecto

El riesgo conocido más crítico de este proyecto es que **SUAMECA no es una API JSON estándar** — es un visor Angular embebido del Banco de la República que renderiza series a través de llamadas internas no documentadas oficialmente. Esto significa que:

1. No existe garantía de estabilidad del endpoint entre sesiones.
2. La respuesta puede venir en formatos no triviales (XML, JSON anidado, o incluso HTML renderizado si se accede mal).
3. Cualquier fallo silencioso aquí (ej. capturar una página de error 200 OK con HTML en vez del dato) contaminaría toda la calibración aguas abajo sin que el optimizador lo detecte — un vector de precios erróneo puede converger a parámetros NSS "razonables" pero completamente falsos.

**Regla de diseño no negociable:** todo dato ingerido debe pasar por un validador de esquema explícito ANTES de tocar cualquier función matemática. Fallo de validación = excepción dura, nunca un `NaN` silencioso.

### Datos requeridos (especificación exacta)

| Dato | Fuente | Frecuencia | Uso |
|---|---|---|---|
| Precios sucios/limpios TES Tasa Fija (benchmark) | SUAMECA (Banco de la República) | Diaria | Input para calibración NSS |
| Precios TES UVR (opcional, fase posterior) | SUAMECA | Diaria | Curva real, no prioritaria en v1 |
| TRM (Tasa Representativa del Mercado) | SFC vía datos.gov.co (Socrata) | Diaria | Spot FX para pricing forward |
| IBR (Indicador Bancario de Referencia) overnight | Banco de la República / SFC | Diaria | Construcción de curva OIS doméstica |
| SOFR (Secured Overnight Financing Rate) | FRED (Federal Reserve Economic Data) | Diaria | Curva de descuento en USD para CIP |

### Prompt 1.1 — Cliente SUAMECA con manejo defensivo

```
Actúa como ingeniero de datos financieros especializado en fuentes gubernamentales 
colombianas no estandarizadas. Vas a implementar `src/tes_pricer/data/suameca_client.py`.

CONTEXTO TÉCNICO CRÍTICO:
SUAMECA (Sistema Unificado de Alertas y Monitoreo de Estabilidad del Mercado de 
Capitales) del Banco de la República NO expone un API REST/JSON documentado 
públicamente. Su interfaz pública es un visor web basado en Angular que consume 
endpoints internos. Antes de escribir código de producción, ejecuta esta 
investigación:

1. Usa el navegador (o Selenium/Playwright si es necesario) para inspeccionar 
   las llamadas de red (Network tab) que hace https://www.banrep.gov.co/es/estadisticas 
   o el visor específico de tasas de TES cuando se filtra por un rango de fechas.
2. Identifica: (a) la URL exacta del endpoint interno que retorna los datos, 
   (b) el formato de respuesta (JSON/XML/CSV), (c) los parámetros de query 
   requeridos (rango de fechas, código de serie, tipo de título).
3. Documenta lo encontrado en un archivo `docs/suameca_reverse_engineering.md` 
   con: URL exacta, headers requeridos, ejemplo de payload de respuesta (primeras 
   5 filas), y fecha en que se hizo esta investigación (los endpoints internos 
   pueden cambiar sin aviso).

Si el endpoint interno resulta inaccesible, inestable, o requiere autenticación 
de sesión no trivial, implementa un FALLBACK explícito: función 
`load_from_manual_export(filepath: str)` que ingiere un archivo CSV/XLSX exportado 
manualmente desde el visor web de SUAMECA (esto es una limitación real y debe 
documentarse como tal, NUNCA ocultarse).

IMPLEMENTA `suameca_client.py` con esta interfaz:

class SuamecaClient:
    def fetch_tes_prices(
        self, 
        bond_isin: str, 
        start_date: date, 
        end_date: date
    ) -> pd.DataFrame:
        \"\"\"
        Retorna DataFrame con columnas exactas:
        ['fecha', 'isin', 'precio_sucio', 'precio_limpio', 'tasa_negociacion', 
         'cupon', 'fecha_vencimiento']
        Lanza SuamecaDataUnavailableError si el endpoint falla o retorna un 
        payload que no matchea el esquema esperado (NUNCA retorna un DataFrame 
        vacío silenciosamente).
        \"\"\"

    def health_check(self) -> bool:
        \"\"\"Verifica que el endpoint (o el fallback manual) esté disponible.\"\"\"

REQUISITOS DE ROBUSTEZ:
- Todo request HTTP debe tener timeout explícito (10s) y máximo 3 reintentos 
  con backoff exponencial.
- Si la respuesta HTTP es 200 pero el Content-Type es text/html (señal de que 
  recibiste la página del visor, no el dato), debe lanzar excepción inmediatamente 
  — este es el modo de fallo silencioso más peligroso del proyecto.
- Cachea localmente cada respuesta cruda exitosa en `data/raw/suameca/{fecha}_{isin}.json` 
  (o .csv) ANTES de parsearla, para permitir debugging y evitar re-consultar si 
  el parsing falla.
- Registra en el manifest.json (ver Fase 0) cada archivo descumido con su SHA256.

Escribe también `tests/unit/test_suameca_client.py` usando `responses` o `httpx.MockTransport` 
para simular: (a) respuesta exitosa válida, (b) respuesta 200 con HTML (debe fallar), 
(c) timeout (debe reintentar y luego fallar con excepción clara), (d) esquema de 
columnas inesperado (debe fallar con mensaje que indique qué columna faltó).
```

**Criterio de aceptación:** existe `docs/suameca_reverse_engineering.md` con hallazgos reales (o documentación honesta de que se requiere fallback manual); los 4 tests unitarios pasan; ningún test unitario requiere conexión real a internet.

### Prompt 1.2 — Cliente Socrata (TRM, IBR) — este SÍ es API estándar

```
Implementa `src/tes_pricer/data/socrata_client.py` usando la librería oficial 
`sodapy` para consumir datos.gov.co (plataforma Socrata de la SFC/Banco de la 
República), que a diferencia de SUAMECA sí expone un API REST/JSON documentado 
y estable.

DATASETS CONFIRMADOS A CONSUMIR (usa estos IDs exactos, verificando primero 
que sigan activos con una llamada de health-check):
- TRM diaria: buscar en datos.gov.co el dataset oficial de la SFC "Tasa 
  Representativa del Mercado (TRM) - Histórico". Verifica el dataset ID actual 
  vía la API de búsqueda de Socrata (no lo hardcodees sin confirmar).
- IBR: buscar el dataset del Banco de la República o SFC para "Indicador 
  Bancario de Referencia (IBR)". Igual, confirma el ID vigente.

IMPLEMENTA:

class SocrataClient:
    def __init__(self, app_token: str | None = None):
        \"\"\"app_token es opcional pero recomendado (evita rate limiting agresivo).\"\"\"

    def fetch_trm(self, start_date: date, end_date: date) -> pd.DataFrame:
        \"\"\"Retorna columnas: ['fecha', 'trm_cop_usd']. TRM se expresa como 
        COP por 1 USD (convención estándar del mercado colombiano).\"\"\"

    def fetch_ibr(
        self, 
        start_date: date, 
        end_date: date, 
        tenor: str = "overnight"
    ) -> pd.DataFrame:
        \"\"\"Retorna columnas: ['fecha', 'ibr_tasa', 'tenor']. 
        La tasa IBR se publica en convención E.A. (Efectiva Anual) por defecto 
        en las fuentes oficiales colombianas — esto es crítico y se reconcilia 
        en la Fase 5 (curva OIS).\"\"\"

    def dataset_health_check(self, dataset_id: str) -> dict:
        \"\"\"Retorna metadata del dataset: última fecha de actualización, 
        número de filas, para detectar si un dataset fue descontinuado.\"\"\"

VALIDACIONES OBLIGATORIAS post-fetch:
1. La TRM debe estar en un rango plausible (ej. entre 2,000 y 10,000 COP/USD 
   para el periodo 2015-2026; ajusta el rango si trabajas con datos históricos 
   más amplios). Un valor fuera de rango debe generar un warning explícito, 
   no descartarse silenciosamente.
2. No debe haber gaps de más de 5 días hábiles consecutivos sin dato (los 
   fines de semana y festivos colombianos no cuentan como gap — implementa 
   un calendario de festivos colombianos usando la librería `holidays` con 
   el país "CO").
3. El IBR debe ser positivo y menor a 30% E.A. (rango histórico plausible para 
   Colombia); fuera de rango = warning.

Escribe tests unitarios con datos mockeados (no llamadas reales a Socrata en 
tests unitarios) y un test de integración separado (marcado `@pytest.mark.integration`) 
que sí llame al API real para confirmar que los dataset IDs siguen vigentes.
```

**Criterio de aceptación:** tests unitarios pasan sin red; el test de integración (corrido manualmente una vez) confirma que los datasets existen y devuelven datos recientes (últimos 30 días con al menos una observación).

---

## FASE 2 — Construcción del Universo de Bonos TES y `tes_referencia.yaml`

### Teoría financiera: precio sucio vs. precio limpio

Un bono TES paga cupones periódicos. El **precio limpio** (*clean price*) es la cotización de mercado sin incluir el interés devengado desde el último pago de cupón. El **precio sucio** (*dirty price*, también llamado *full price*) es lo que efectivamente paga el comprador:

```
P_sucio = P_limpio + Interés_Acumulado
```

El interés acumulado (*accrued interest*, AI) se calcula como:

```
AI = Cupón_periódico × (días_transcurridos_desde_último_cupón / días_totales_del_periodo_de_cupón)
```

**Punto crítico de convención (riesgo conocido del proyecto):** la convención de conteo de días para TES en COP es **Actual/365** para el mercado local colombiano (no 30/360, que es más común en mercados desarrollados de renta fija corporativa, ni Actual/360, que se usa típicamente en el mercado monetario/IBR). Esta distinción debe confirmarse explícitamente contra la documentación de la BVC o el Banco de la República — **nunca asumirse por defecto** — porque un error aquí desplaza sistemáticamente toda la curva calibrada.

### La relación precio-tasa (Yield to Maturity)

Para un bono TES Tasa Fija con cupones anuales (la convención estándar colombiana — confirmar si el bono específico paga anual o semestral, algunos TES largos pagan cupón anual):

```
P_sucio = Σ(t=1 hasta N) [C / (1+y)^t] + [VN / (1+y)^N]
```

Donde:
- `C` = cupón periódico en unidades monetarias (tasa cupón × valor nominal)
- `VN` = valor nominal (típicamente COP 100 o COP 1,000 por unidad, verificar convención BVC)
- `y` = Yield to Maturity (YTM), la tasa interna de retorno que iguala el VP de los flujos al precio sucio observado
- `N` = número de periodos de cupón restantes hasta el vencimiento

Este YTM por bono es el dato de entrada crudo para la calibración de la curva — **no se calibra la curva directamente sobre precios**, se calibra sobre YTMs implícitos (o, en versiones más rigurosas, se calibra directamente sobre precios minimizando error de precio, lo cual es más robusto pero computacionalmente más pesado; se especifica cuál usar en la Fase 3).

### Prompt 2.1 — Construcción y validación de `tes_referencia.yaml`

```
Actúa como analista de renta fija especializado en el mercado de deuda pública 
colombiana. Tu tarea es construir y validar el archivo de configuración 
`src/tes_pricer/config/tes_referencia.yaml`, que define el universo de bonos 
TES benchmark usados para calibrar la curva.

INVESTIGACIÓN REQUERIDA (usa web_search antes de escribir el YAML):
1. Identifica los TES Tasa Fija benchmark actualmente vigentes y más líquidos 
   en el mercado colombiano (busca "TES benchmark 2026 Colombia" y "curva de 
   rendimientos TES Banco de la República" para confirmar los ISINs vigentes 
   — estos títulos benchmark rotan cada pocos años cuando el Ministerio de 
   Hacienda emite nuevas referencias).
2. Para CADA bono, confirma con fuente verificable (BVC, Ministerio de Hacienda, 
   o Banco de la República):
   - ISIN o código de identificación
   - Fecha de emisión
   - Fecha de vencimiento
   - Tasa cupón nominal
   - Frecuencia de pago de cupón (anual o semestral — NO asumas, verifica)
   - Convención de day count aplicable (confirma si es Actual/365 o Actual/360 
     para el mercado de TES en COP; cita la fuente exacta, ej. reglamento de 
     la BVC o circular del Banco de la República)
   - Valor nominal por unidad (COP 100 vs COP 1,000, según convención vigente)

ESTRUCTURA del YAML:

bonos_benchmark:
  - isin: "string"
    nombre_corto: "TES [tasa]% [año_vencimiento]"
    fecha_emision: "YYYY-MM-DD"
    fecha_vencimiento: "YYYY-MM-DD"
    tasa_cupon_nominal: float  # en decimal, ej. 0.07 para 7%
    frecuencia_cupon: "anual" | "semestral"
    valor_nominal: float
    day_count_convention: "ACT/365" | "ACT/360" | "30/360"
    fuente_verificacion: "URL o referencia exacta del documento oficial"
    fecha_verificacion: "YYYY-MM-DD"
    tramo_curva: "corto" | "medio" | "largo"  # corto: <3Y, medio: 3-10Y, largo: >10Y

metadata:
  fecha_construccion: "YYYY-MM-DD"
  numero_bonos: int
  cobertura_temporal_minima: "X años"
  cobertura_temporal_maxima: "Y años"
  advertencias: []  # cualquier supuesto o limitación no verificable con fuente primaria

REQUISITOS:
- Mínimo 6 bonos benchmark cubriendo al menos los tramos corto (1-3 años), 
  medio (5-7 años) y largo (10+ años) de la curva — la calibración NSS necesita 
  suficiente dispersión de vencimientos para identificar los 6 parámetros 
  (β0, β1, β2, β3, λ1, λ2) sin problemas de identificabilidad numérica.
- Si no puedes verificar con fuente primaria alguno de los campos para un bono, 
  NO lo incluyas en el universo — es preferible tener 6 bonos 100% verificados 
  que 10 con datos inferidos.
- Etiqueta explícitamente cada campo no verificable en `advertencias`.

Escribe también `src/tes_pricer/data/validators.py` con una función 
`validate_tes_referencia_yaml(filepath: str) -> ValidationReport` que:
1. Verifique que el YAML parsea correctamente contra un JSON Schema.
2. Verifique que fecha_vencimiento > fecha_emision para cada bono.
3. Verifique que no haya ISINs duplicados.
4. Verifique que el rango de vencimientos cubra al menos 3 "tramos" de curva 
   distintos (corto/medio/largo) — si no, lanza un warning porque la calibración 
   NSS será numéricamente inestable con datos concentrados en un solo tramo.
```

**Criterio de aceptación:** `tes_referencia.yaml` existe con mínimo 6 bonos, cada campo con fuente citada; `validate_tes_referencia_yaml` pasa sin errores; existe al menos un test unitario que falle intencionalmente el YAML (ISIN duplicado, fecha inválida) para confirmar que el validador efectivamente detecta problemas.

### Prompt 2.2 — Cálculo de precio sucio, precio limpio y YTM

```
Implementa `src/tes_pricer/math/bond_pricing.py` con las siguientes funciones 
puras (sin I/O, testeable con inputs sintéticos):

def accrued_interest(
    settlement_date: date,
    last_coupon_date: date,
    next_coupon_date: date,
    coupon_rate: float,
    face_value: float,
    frequency: int,  # 1 = anual, 2 = semestral
    day_count: str  # "ACT/365", "ACT/360", "30/360"
) -> float:
    \"\"\"
    Calcula el interés acumulado (AI) usando la fórmula:
    AI = (Cupón_periódico) × (días_transcurridos / días_totales_del_periodo)
    
    Donde Cupón_periódico = (coupon_rate / frequency) × face_value
    
    Implementa las 3 convenciones de day count exactamente:
    - ACT/365: días_transcurridos = (settlement_date - last_coupon_date).days
               días_totales = 365 / frequency (aproximación estándar) 
               O, más riguroso: (next_coupon_date - last_coupon_date).days 
               — usa esta segunda forma (periodo real), es más precisa.
    - ACT/360: igual pero denominador base 360.
    - 30/360: usa la convención bancaria donde cada mes se trata como 30 días 
      y el año como 360 (implementa el método "30/360 Bond Basis" ISDA estándar, 
      no la variante europea simplificada, salvo que la fuente colombiana 
      especifique lo contrario).
    \"\"\"

def dirty_price_from_ytm(
    ytm: float,
    coupon_rate: float,
    face_value: float,
    settlement_date: date,
    maturity_date: date,
    frequency: int,
    day_count: str
) -> float:
    \"\"\"
    Calcula el precio sucio a partir del YTM usando descuento de flujos:
    
    P_sucio = Σ(i=1 hasta N) [C_i / (1+y/frequency)^(t_i × frequency)] 
              + [VN / (1+y/frequency)^(t_N × frequency)]
    
    donde t_i son los tiempos fraccionarios (en años) desde settlement_date 
    hasta cada fecha de pago de cupón i, calculados según day_count.
    
    Para el ÚLTIMO periodo de cupón (el que contiene settlement_date), el 
    tiempo fraccionario debe ajustarse por el periodo parcial ya transcurrido 
    (esto conecta con accrued_interest).
    \"\"\"

def clean_price(dirty_price: float, accrued: float) -> float:
    \"\"\"P_limpio = P_sucio - AI. Trivial pero debe existir como función 
    nombrada para claridad de dominio en el resto del código.\"\"\"

def ytm_from_dirty_price(
    dirty_price: float,
    coupon_rate: float,
    face_value: float,
    settlement_date: date,
    maturity_date: date,
    frequency: int,
    day_count: str,
    initial_guess: float = 0.08
) -> float:
    \"\"\"
    Resuelve el YTM implícito dado un precio sucio observado, usando 
    scipy.optimize.brentq o newton sobre la función:
    
    f(y) = dirty_price_from_ytm(y, ...) - dirty_price_observado = 0
    
    Usa brentq con un bracket amplio [0.0001, 0.50] (0.01% a 50% E.A.) para 
    garantizar convergencia incluso en escenarios de estrés de tasas. Si no 
    converge, lanza YTMConvergenceError con diagnóstico (no falles silenciosamente 
    retornando el initial_guess).
    \"\"\"

REQUISITOS DE TESTING (tests/unit/test_bond_pricing.py):
1. Test de round-trip: genera un YTM sintético, calcula dirty_price_from_ytm, 
   luego recupera el YTM con ytm_from_dirty_price, y verifica que el error 
   absoluto sea menor a 1e-8. Este test debe correr para las 3 convenciones 
   de day count y para frequency=1 y frequency=2.
2. Test de caso conocido: usa un ejemplo de bond pricing de un libro de texto 
   estándar (ej. Fabozzi, "Bond Markets, Analysis, and Strategies") con valores 
   publicados de precio/YTM, y verifica que tu implementación reproduce el 
   resultado publicado con tolerancia razonable (documenta la fuente exacta 
   del ejemplo en el docstring del test).
3. Test de edge case: bono a menos de un periodo de cupón del vencimiento 
   (verifica que no haya división por cero ni comportamiento indefinido).
4. Test de accrued_interest en la fecha exacta de pago de cupón (debe dar 
   AI = 0, no un valor cercano a 0 por error de redondeo — usa comparación 
   con tolerancia explícita, no ==).
```

**Criterio de aceptación:** todos los tests de round-trip pasan con error <1e-8; el test contra el caso de libro de texto reproduce el valor publicado; cobertura de este módulo >95% (es el módulo más crítico del proyecto).

---

## FASE 3 — Teoría y Calibración del Modelo Nelson-Siegel-Svensson

### Teoría financiera completa: el modelo NSS

El modelo de Nelson-Siegel (1987), extendido por Svensson (1994), aproxima la curva de tasas spot (o forward instantáneas) mediante una función paramétrica de 6 parámetros. La tasa spot cero-cupón para un vencimiento `τ` (tau, en años) se modela como:

```
y(τ) = β0 + β1 · [(1 - e^(-τ/λ1)) / (τ/λ1)] + β2 · [((1 - e^(-τ/λ1)) / (τ/λ1)) - e^(-τ/λ1)] + β3 · [((1 - e^(-τ/λ2)) / (τ/λ2)) - e^(-τ/λ2)]
```

**Interpretación económica de cada parámetro (esto es lo que hace que el modelo sea *interpretable*, no solo un ajuste numérico):**

- **β0 (nivel / *level*):** representa la tasa asintótica de largo plazo, cuando τ → ∞. Es el componente que no decae — todos los factores de carga convergen a β0 en el infinito. Económicamente, refleja las expectativas de inflación y crecimiento de largo plazo.

- **β1 (pendiente / *slope*):** su factor de carga `(1 - e^(-τ/λ1))/(τ/λ1)` comienza en 1 cuando τ→0 y decae monótonamente a 0 cuando τ→∞. Por tanto β0 + β1 = tasa de corto plazo instantánea (τ→0). β1 negativo implica una curva normal (ascendente); β1 positivo implica una curva invertida. Refleja la política monetaria de corto plazo del Banco de la República.

- **β2 (curvatura / *curvature*, primera joroba):** su factor de carga `[(1-e^(-τ/λ1))/(τ/λ1)] - e^(-τ/λ1)` comienza en 0, alcanza un máximo en un vencimiento intermedio determinado por λ1, y vuelve a 0 en el largo plazo. Este término captura la "joroba" (*hump*) típica de curvas de mercados emergentes en el tramo medio.

- **β3 (segunda curvatura, extensión de Svensson):** funcionalmente idéntico a β2 pero con su propio parámetro de decaimiento λ2, permitiendo una segunda joroba en un vencimiento distinto. Esta es la extensión que Svensson añadió a Nelson-Siegel original precisamente porque el modelo de 4 parámetros no siempre captura curvas emergentes con dos puntos de inflexión (común en TES debido a la interacción entre expectativas de política monetaria de corto plazo y prima de riesgo soberano de largo plazo).

- **λ1, λ2 (parámetros de decaimiento):** controlan la velocidad a la que decaen los factores de carga de β1/β2 y β3 respectivamente, y por tanto EN QUÉ VENCIMIENTO ocurre el máximo de cada joroba. Matemáticamente, el máximo del factor de carga de β2 ocurre aproximadamente en τ ≈ 1.8 × λ1 (relación exacta requiere derivar la función de carga e igualar a cero — se implementa como validación en el test suite, no se hardcodea).

**Restricciones de dominio no negociables:**
- λ1 > 0 y λ2 > 0 (deben ser estrictamente positivos; un λ negativo o cero produce una función indefinida o economicamente sin sentido — división por cero en τ/λ cuando τ→0 se resuelve por límite analítico, ya que el factor de carga tiende a 1, pero λ≤0 rompe la interpretación).
- Convencionalmente, λ1 ≠ λ2 (si son iguales, el modelo se degenera a Nelson-Siegel de 4 parámetros con redundancia — el optimizador puede intentar converger ahí, lo cual NO es un error sino una señal de que los datos no soportan la complejidad de 6 parámetros).
- β0 > 0 típicamente (tasa de largo plazo nominal positiva) pero no se debe forzar como constraint duro — se valida post-calibración, no se restringe en el optimizador (restringirlo artificialmente sesgaría el ajuste en escenarios legítimos de tasas negativas, aunque no es el caso actual de Colombia).

### La función objetivo de calibración

El problema de calibración es un problema de mínimos cuadrados no lineales:

```
min (β0,β1,β2,β3,λ1,λ2) Σ(i=1 hasta n) [y_obs(τ_i) - y_NSS(τ_i; β0,β1,β2,β3,λ1,λ2)]²
```

Donde `y_obs(τ_i)` son los YTMs observados de mercado (calculados en la Fase 2) para cada bono `i` con vencimiento `τ_i`. Esto se resuelve con `scipy.optimize.least_squares`, que implementa el algoritmo de Levenberg-Marquardt (o Trust Region Reflective si hay bounds) — mucho más eficiente y estable que Nelder-Mead o gradiente simple para este tipo de problema no lineal con 6 parámetros.

**Problema crítico de optimización no convexa — por qué se requiere multi-start:** la función objetivo NSS **no es convexa** en λ1 y λ2 (los β son lineales condicionales a λ fijos, pero λ entra no linealmente). Esto significa que el optimizador puede converger a un mínimo local dependiendo del punto de partida. La solución estándar en la literatura (y la que especifica el roadmap original) es **multi-start**: correr la optimización desde múltiples puntos iniciales de (λ1, λ2) distribuidos sobre una grilla razonable, y quedarse con la solución de menor error cuadrático medio entre todas las convergencias válidas.

### Prompt 3.1 — Implementación del modelo NSS puro

```
Implementa `src/tes_pricer/math/nss_model.py` — este módulo NO debe importar 
scipy.optimize (esa es responsabilidad de calibration.py). Aquí solo va la 
función matemática pura del modelo.

import numpy as np

def nss_yield(
    tau: np.ndarray | float,
    beta0: float,
    beta1: float,
    beta2: float,
    beta3: float,
    lambda1: float,
    lambda2: float
) -> np.ndarray | float:
    \"\"\"
    Calcula la tasa spot NSS para uno o más vencimientos tau (en años).
    
    Fórmula exacta:
    y(τ) = β0 
         + β1 * [(1 - exp(-τ/λ1)) / (τ/λ1)]
         + β2 * [((1 - exp(-τ/λ1)) / (τ/λ1)) - exp(-τ/λ1)]
         + β3 * [((1 - exp(-τ/λ2)) / (τ/λ2)) - exp(-τ/λ2)]
    
    MANEJO CRÍTICO DEL LÍMITE τ→0:
    Cuando τ es extremadamente pequeño (o exactamente 0), el término τ/λ 
    causa 0/0 en el cálculo directo. El límite analítico correcto es:
    lim(τ→0) [(1 - exp(-τ/λ)) / (τ/λ)] = 1
    
    Implementa esto con np.where o una función auxiliar _factor_carga_nivel(x) 
    que use un umbral (ej. |x| < 1e-7) para devolver 1.0 directamente en vez 
    de evaluar la división, evitando errores de punto flotante (NO errores 
    matemáticos — numpy no lanzará ZeroDivisionError con floats, pero SÍ puede 
    devolver NaN si τ=0 exacto, lo cual contaminaría silenciosamente resultados 
    aguas abajo).
    
    Debe soportar tanto un tau escalar como un array de numpy (vectorizado, 
    sin loops de Python) — esto es lo que permite evaluar la curva completa 
    en miles de puntos instantáneamente para plotting y para el bootstrapping 
    OIS de la Fase 5.
    \"\"\"

def nss_discount_factor(tau: np.ndarray | float, params: dict) -> np.ndarray | float:
    \"\"\"
    Convierte la tasa spot NSS en factor de descuento, asumiendo composición 
    continua:
    DF(τ) = exp(-y(τ) × τ)
    
    NOTA DE CONVENCIÓN: confirma si el mercado de TES usa composición continua 
    o composición anual discreta (DF = 1/(1+y)^τ) para la curva cero-cupón 
    derivada — esto DEBE ser consistente con la convención usada en 
    bond_pricing.py de la Fase 2. Documenta la decisión explícitamente en el 
    docstring del módulo con la justificación (la composición continua es 
    matemáticamente más conveniente para forwards y derivadas analíticas de 
    griegas, y es el estándar en pricing de derivados FX — se recomienda para 
    este proyecto, pero debe declararse, no asumirse implícitamente).
    \"\"\"

def nss_forward_rate(
    tau1: float, 
    tau2: float, 
    params: dict
) -> float:
    \"\"\"
    Tasa forward instantánea implícita entre dos vencimientos, derivada de 
    los factores de descuento:
    
    f(τ1, τ2) = [ln(DF(τ1)) - ln(DF(τ2))] / (τ2 - τ1)
    
    Esta función será reutilizada en la Fase 6 para el pricing de forwards 
    FX vía interest rate parity, y en la Fase 5 para construir la curva OIS.
    \"\"\"

TESTS OBLIGATORIOS (tests/unit/test_nss_model.py):
1. Test del límite τ→0: evalúa nss_yield con τ=1e-10 y τ=0.0 exacto, y 
   confirma que el resultado es finito y aproximadamente igual a β0+β1 
   (la tasa de corto plazo instantánea), NO NaN.
2. Test de convergencia asintótica: evalúa nss_yield con τ=1000 (extremo) 
   y confirma que converge a β0 con tolerancia <1e-6.
3. Test de vectorización: confirma que nss_yield(array_de_100_taus, ...) 
   produce el mismo resultado que iterar nss_yield(tau_individual, ...) 
   para cada elemento, con error <1e-12 (deben ser matemáticamente idénticos, 
   la vectorización es solo una optimización de performance).
4. Test de forma de joroba: con parámetros donde β2 > 0 y λ1 conocido, 
   verifica numéricamente (usando scipy.optimize.minimize_scalar sobre 
   -nss_yield en función de tau) que el máximo de la joroba ocurre 
   aproximadamente en τ ≈ 1.79 × λ1 (deriva esta constante analíticamente 
   como parte del test, no la hardcodees sin justificación matemática — 
   requiere resolver d/dτ del factor de carga de β2 igualado a cero).
```

**Criterio de aceptación:** los 4 tests pasan; el módulo no tiene ninguna dependencia de scipy.optimize; cobertura 100% (es matemática pura, debe ser trivial de cubrir completamente).

### Prompt 3.2 — Motor de calibración con multi-start

```
Implementa `src/tes_pricer/math/calibration.py`, que SÍ usa scipy.optimize.

from dataclasses import dataclass
from scipy.optimize import least_squares
import numpy as np

@dataclass
class NSSCalibrationResult:
    beta0: float
    beta1: float
    beta2: float
    beta3: float
    lambda1: float
    lambda2: float
    rmse: float                    # error cuadrático medio en YTM (unidades: decimal, ej. 0.0012 = 12bps)
    max_abs_error: float           # error absoluto máximo entre todos los bonos
    n_bonds_used: int
    converged: bool
    n_starts_tried: int
    optimization_message: str
    per_bond_errors: dict          # {isin: error_bps} para diagnóstico

def calibrate_nss(
    observed_yields: np.ndarray,      # YTMs observados (Fase 2)
    maturities: np.ndarray,           # τ en años, correspondiente a cada yield
    bond_isins: list[str],
    n_multistart: int = 25,
    lambda_grid_bounds: tuple[float, float] = (0.1, 15.0)
) -> NSSCalibrationResult:
    \"\"\"
    ALGORITMO DE MULTI-START (obligatorio, no opcional — el problema NO es 
    convexo en lambda1/lambda2):
    
    1. Genera n_multistart puntos iniciales para (lambda1, lambda2) usando 
       Latin Hypercube Sampling (scipy.stats.qmc.LatinHypercube) sobre 
       lambda_grid_bounds — esto da mejor cobertura del espacio que una 
       grilla regular o puntos aleatorios uniformes puros.
    2. Para cada punto inicial de lambda, los betas iniciales (beta0..beta3) 
       se pueden estimar con un guess razonable: beta0 = promedio de los 
       yields observados de mayor vencimiento, beta1 = yield_corto - beta0, 
       beta2 = 0, beta3 = 0 (punto de partida neutral para las jorobas).
    3. Ejecuta scipy.optimize.least_squares con method='trf' (Trust Region 
       Reflective, soporta bounds) y bounds: 
       - beta0, beta1, beta2, beta3: sin bound estricto pero con bound suave 
         [-0.5, 0.5] (50% es un límite generoso para tasas en cualquier escenario 
         realista, previene divergencia numérica)
       - lambda1, lambda2: (0.01, 30.0) — estrictamente positivos, límite 
         superior generoso para evitar decaimiento numéricamente indistinguible 
         de una constante.
    4. Para cada convergencia exitosa (optimize_result.success == True), 
       calcula el RMSE de ese resultado.
    5. Selecciona como resultado final la convergencia con MENOR RMSE entre 
       todas las que convergieron. Si NINGUNA convergió, lanza 
       NSSCalibrationError con diagnóstico completo (no retornes un resultado 
       parcial silenciosamente).
    6. Si lambda1 y lambda2 convergen a valores muy cercanos entre sí 
       (|lambda1 - lambda2| / lambda1 < 0.05), agrega un WARNING al resultado 
       indicando que el modelo se está degenerando a Nelson-Siegel de 4 
       parámetros — esto es información valiosa, no un error, pero debe 
       reportarse porque afecta la interpretabilidad.
    
    LA FUNCIÓN DE RESIDUOS pasada a least_squares debe ser:
    
    def residuals(params, taus, observed_yields):
        beta0, beta1, beta2, beta3, lambda1, lambda2 = params
        model_yields = nss_yield(taus, beta0, beta1, beta2, beta3, lambda1, lambda2)
        return model_yields - observed_yields
    
    NOTA: se calibra sobre YIELDS (YTM), no sobre PRECIOS directamente. Esto 
    es una decisión de diseño explícita: calibrar sobre yields es computacionalmente 
    más simple y es el estándar pedagógico (Diebold-Li, Nelson-Siegel original), 
    pero calibrar sobre precios minimizando error de precio es más consistente 
    con cómo se usará la curva después (para descontar flujos de caja) y es 
    el estándar en mesas profesionales porque pondera implícitamente por 
    duración. Documenta esta decisión en el docstring del módulo como una 
    limitación conocida de v1, y deja un comentario TODO explícito para una 
    v2 que calibre por error de precio ponderado por duración modificada 
    inversa (los bonos de menor duración tienen mayor sensibilidad de precio 
    a error de yield, por lo que ponderar por 1/duración es la corrección 
    estándar).

Escribe tests exhaustivos en tests/unit/test_calibration.py:
1. Test de recuperación de parámetros conocidos (el más importante): genera 
   una curva NSS sintética con parámetros conocidos (ej. beta0=0.09, beta1=-0.02, 
   beta2=0.01, beta3=0.005, lambda1=2.0, lambda2=8.0 — valores plausibles para 
   TES colombianos), evalúa en 8-10 vencimientos típicos de tu tes_referencia.yaml, 
   añade ruido gaussiano pequeño (sigma=0.0005, es decir 5bps) para simular 
   error de mercado realista, y calibra. Verifica que los parámetros recuperados 
   estén dentro de una tolerancia razonable de los verdaderos (define la 
   tolerancia explícitamente y justifícala — con ruido de 5bps, no esperes 
   recuperación exacta, pero sí que el RMSE del fit sea del orden del ruido 
   inyectado, no mayor).
2. Test de sensibilidad al número de bonos: confirma que calibrar con solo 
   4 bonos (mínimo teórico dado que hay 6 parámetros pero 4 son linealmente 
   independientes en la práctica) produce un RMSE notablemente peor o mayor 
   inestabilidad (medida por varianza entre distintos multi-starts) que 
   calibrar con 8+ bonos — esto documenta empíricamente por qué el universo 
   de tes_referencia.yaml necesita mínimo 6-8 bonos bien distribuidos.
3. Test de detección de degeneración: fuerza un escenario donde los datos 
   son consistentes con un modelo NS de 4 parámetros (beta3=0 exacto en la 
   generación sintética) y confirma que el warning de degeneración lambda1≈lambda2 
   se dispara apropiadamente O que beta3 calibrado converge a un valor cercano 
   a 0.
4. Test de no-convergencia manejada: pasa datos adversariales (ej. yields 
   idénticos para todos los vencimientos, curva perfectamente plana) y 
   verifica que el sistema NO crashea sino que retorna un resultado válido 
   marcado con converged=True pero con un warning de que la curva es 
   degenerada/plana (beta1, beta2, beta3 cercanos a 0).
```

**Criterio de aceptación:** el test de recuperación de parámetros pasa con RMSE del fit menor a 0.0010 (10bps, ligeramente por encima del ruido inyectado de 5bps, tolerancia razonable); el multi-start efectivamente encuentra el óptimo global en al menos 3 corridas independientes con distintas semillas aleatorias.

---

## FASE 4 — Validación Cruzada con QuantLib

### Por qué esta fase es obligatoria, no opcional

Nunca confíes en tu propia implementación de un modelo financiero estándar sin contrastarla contra una librería auditada externamente. QuantLib es el estándar de facto de código abierto para pricing de renta fija, usado y auditado por miles de practicantes institucionales desde 2000. Si tu implementación de NSS diverge de QuantLib en más de un umbral de tolerancia numérica, el error está en tu código, no en QuantLib — este test existe para atraparlo antes de que contamine el pricer de forwards.

**Riesgo técnico conocido:** compatibilidad de wheels de QuantLib-Python con Python 3.13. Verificar disponibilidad antes de comprometerse con esta versión de Python para el proyecto completo.

### Prompt 4.1 — Verificación de compatibilidad y benchmark

```
Antes de escribir el test de benchmark, verifica la compatibilidad de 
QuantLib-Python con el entorno del proyecto:

1. Ejecuta `pip index versions QuantLib` (o consulta PyPI directamente) para 
   confirmar qué versión de QuantLib-Python tiene wheels precompilados 
   disponibles para tu versión exacta de Python y tu sistema operativo.
2. Si Python 3.13 no tiene wheels disponibles para QuantLib-Python al momento 
   de ejecutar esto, DOCUMENTA esta limitación explícitamente en 
   docs/known_limitations.md y considera fijar el proyecto a Python 3.11 o 
   3.12 en pyproject.toml (ajusta el `requires-python` acordemente) — esto 
   es preferible a bloquear el desarrollo esperando wheels que pueden tardar 
   meses en publicarse tras un release de Python.
3. Confirma la instalación con: 
   python -c "import QuantLib as ql; print(ql.__version__)"

Una vez confirmada la compatibilidad, implementa 
`tests/benchmark/test_nss_quantlib_benchmark.py`:

import QuantLib as ql

def test_nss_matches_quantlib_svensson_curve():
    \"\"\"
    QuantLib implementa nativamente la curva de Svensson vía 
    ql.SvenssonFitting, que usa el mismo modelo matemático que nss_model.py 
    pero con su propia rutina de calibración interna (Levenberg-Marquardt 
    de QuantLib, no scipy).
    
    PROCEDIMIENTO:
    1. Construye los mismos bonos sintéticos usados en el test de recuperación 
       de parámetros de calibration.py (Fase 3), pero ahora como objetos 
       ql.FixedRateBond de QuantLib, respetando exactamente la misma convención 
       de day count (ql.Actual365Fixed() o la que corresponda según lo 
       determinado en Fase 2) y calendario (ql.Colombia() si existe en QuantLib; 
       si no existe un calendario colombiano nativo, usa ql.NullCalendar() con 
       ajuste manual de festivos, y documenta esta limitación).
    2. Calibra la curva usando ql.FittedBondDiscountCurve con 
       ql.SvenssonFitting() como método.
    3. Extrae los parámetros calibrados por QuantLib (ql tiene un método 
       para acceder a los parámetros fiteados del SvenssonFitting) y compara 
       directamente contra los parámetros calibrados por tu calibration.py 
       sobre el MISMO set de datos sintéticos.
    4. ADEMÁS de comparar parámetros, compara las TASAS SPOT implícitas en 
       10 vencimientos de prueba entre 0.5 y 15 años, calculadas por ambos 
       sistemas — esta es la comparación más robusta porque distintas 
       parametrizaciones internas (QuantLib puede tener una convención de 
       signo o escala ligeramente distinta) pueden llegar a la MISMA curva 
       aunque los parámetros crudos difieran superficialmente.
    
    CRITERIO DE ACEPTACIÓN: la diferencia absoluta en tasa spot entre tu 
    implementación y QuantLib, en cada uno de los 10 vencimientos de prueba, 
    debe ser menor a 1 basis point (0.0001) para datos sintéticos sin ruido. 
    Con datos reales (Fase posterior), la tolerancia se relaja a 5bps porque 
    ambos sistemas pueden converger a mínimos locales ligeramente distintos 
    dado que el problema no es convexo — esto se documenta explícitamente 
    como limitación esperada, no como bug.

Si el benchmark falla por más del umbral, NO ajustes la tolerancia para que 
pase — audita nss_model.py y calibration.py línea por línea contra la 
documentación matemática de QuantLib (revisa el código fuente de 
SvenssonFitting en el repositorio de QuantLib si es necesario) para encontrar 
la discrepancia real. Los candidatos más probables de error son: (a) 
convención de composición (continua vs anual) no siendo consistente entre 
los dos sistemas al comparar, (b) un signo invertido en algún término de la 
fórmula, (c) definición de tau (años vs. días) inconsistente.
```

**Criterio de aceptación:** benchmark pasa con tolerancia <1bp en datos sintéticos; cualquier divergencia encontrada y corregida se documenta en un changelog técnico explicando la causa raíz.

---

## FASE 5 — Curva OIS/IBR para Descuento

### Teoría: por qué se necesita una curva de descuento separada de la curva TES

La curva TES calibrada en las Fases 3-4 representa el costo de financiamiento del **gobierno colombiano** en COP. Para valorar correctamente un Forward USD/COP bajo la condición de no arbitraje (Fase 6), se necesita la curva de tasas **libres de riesgo de contraparte a corto plazo** en ambas monedas — esto se aproxima con curvas OIS (Overnight Index Swap), no con la curva soberana de bonos, porque:

1. La curva TES incluye prima de plazo y prima de liquidez específica de bonos, no es la tasa "pura" de fondeo overnight.
2. El estándar de mercado post-crisis financiera 2008 (y consolidado en Colombia con el IBR como tasa de referencia desde 2008) es descontar flujos colateralizados con la curva OIS, reservando la curva de bonos soberanos para el pricing de los bonos mismos.

**Construcción de la curva OIS doméstica (COP) vía bootstrapping:**

El IBR overnight es la tasa base. Para construir una curva a plazos mayores a overnight, se necesita el mercado de swaps OIS-IBR (donde existen cotizaciones a distintos tenores: 1M, 3M, 6M, 1Y, etc.). El bootstrapping extrae factores de descuento implícitos secuencialmente:

```
Para el primer nodo (tenor más corto, ej. 1M):
DF(t1) = 1 / (1 + IBR_swap_rate(t1) × t1)     [si convención es simple/lineal]

Para nodos subsecuentes (ej. tenor t2 > t1):
El swap de tenor t2 paga la tasa fija swap_rate(t2) contra el flotante IBR 
compuesto. La condición de no arbitraje que el swap valga cero al inicio 
implica una ecuación que se resuelve para DF(t2) dado que DF(t1) ya se conoce 
del paso anterior — este es el proceso iterativo de "bootstrapping".
```

**Curva USD (SOFR):** de manera análoga, se requiere la curva OIS en USD, para la cual SOFR (que reemplazó a USD LIBOR como tasa de referencia estándar desde 2023) es el input base, obtenida de fuentes como FRED o directamente de proveedores de datos de mercado.

**Simplificación pragmática para v1 del proyecto (declarar explícitamente):** dado que el proyecto es para un desarrollador individual (no una mesa institucional con acceso a Bloomberg/Refinitiv para curvas swap completas), la v1 puede aproximar la curva de corto plazo usando **interpolación simple entre el IBR overnight/1M/3M publicados directamente** (sin bootstrapping de swaps completo) para los tenores relevantes al pricing de forwards FX de corto plazo (típicamente hasta 1 año, que cubre la mayoría de forwards comerciales). El bootstrapping completo de swaps OIS es un refinamiento de v2 si se requiere pricing de forwards a plazos mayores a 1 año.

### Prompt 5.1 — Curva de descuento de corto plazo (IBR y SOFR)

```
Implementa `src/tes_pricer/math/ois_curve.py`:

from dataclasses import dataclass

@dataclass
class ShortRateCurve:
    tenors_years: np.ndarray       # ej. [0.0028 (overnight~1/365), 0.083 (1M), 0.25 (3M), 0.5 (6M), 1.0 (1Y)]
    rates: np.ndarray              # tasas E.A. correspondientes
    currency: str                  # "COP" o "USD"
    curve_date: date
    
    def discount_factor(self, tau: float) -> float:
        \"\"\"
        Interpola linealmente sobre las tasas (NO sobre los factores de 
        descuento directamente — interpolar tasas y luego convertir a DF es 
        el estándar de mercado, produce curvas forward más suaves que 
        interpolar DFs directamente, lo cual puede generar tasas forward 
        instantáneas con discontinuidades).
        
        Una vez obtenida la tasa interpolada r(tau) en convención E.A.:
        DF(tau) = 1 / (1 + r(tau))^tau
        
        Para tau menor al tenor mínimo disponible o mayor al máximo, usa 
        extrapolación plana (flat extrapolation) del último valor observado 
        — NO extrapoles linealmente sin límite, es una fuente común de 
        resultados económicamente absurdos en el extremo largo.
        \"\"\"

def build_cop_short_curve(
    ibr_overnight: float,
    ibr_1m: float | None,
    ibr_3m: float | None,
    curve_date: date
) -> ShortRateCurve:
    \"\"\"
    Construye la curva de corto plazo en COP. Si solo hay IBR overnight 
    disponible (dato más comúnmente publicado), usa flat extrapolation desde 
    ese único punto y EMITE UN WARNING explícito de que la curva es una 
    aproximación de tasa plana, no una curva real con estructura de plazos 
    — esto es una limitación conocida y debe ser visible para cualquier 
    usuario del sistema, no oculta.
    \"\"\"

def build_usd_short_curve(
    sofr_overnight: float,
    curve_date: date,
    additional_tenors: dict[float, float] | None = None
) -> ShortRateCurve:
    \"\"\"Análogo para USD usando SOFR como tasa base.\"\"\"

INVESTIGACIÓN REQUERIDA antes de implementar:
Confirma vía web_search si existen fuentes públicas gratuitas (no Bloomberg/
Refinitiv) que publiquen IBR a plazos de 1M/3M/6M además del overnight — 
busca en el Banco de la República o la SFC. Si NO existen fuentes públicas 
gratuitas para estos tenores, documenta esto como limitación de datos y 
usa la aproximación de tasa plana desde IBR overnight, dejando el bootstrapping 
completo de swaps OIS como mejora de v2 (requeriría suscripción a datos de 
mercado no gratuitos).

TESTS (tests/unit/test_ois_curve.py):
1. Test de interpolación: con 3 puntos conocidos, verifica que el punto medio 
   interpolado da un factor de descuento consistente con interpolación lineal 
   de tasas (no de DFs) — construye el caso de prueba manualmente y compara 
   contra el cálculo esperado a mano.
2. Test de extrapolación plana en ambos extremos (tau muy pequeño y tau muy 
   grande fuera del rango de datos).
3. Test del caso degenerado (un solo punto de tasa disponible): confirma que 
   se emite el warning esperado y que discount_factor(tau) para cualquier tau 
   usa esa única tasa.
```

**Criterio de aceptación:** tests pasan; el warning de curva plana se dispara correctamente cuando corresponde; queda documentado en `docs/known_limitations.md` la ausencia (o presencia, si se encuentra) de fuentes públicas de IBR a plazo.

---

## FASE 6 — Pricer de Forward USD/COP (Covered Interest Rate Parity)

### Teoría financiera: la condición de no arbitraje para forwards FX

El precio teórico de un contrato forward de tipo de cambio se deriva de la **Paridad de Tasas de Interés Cubierta** (*Covered Interest Rate Parity*, CIP), que es una relación de no arbitraje pura — no depende de expectativas ni de modelos de comportamiento, solo de la imposibilidad de generar una ganancia libre de riesgo mediante una estrategia de arbitraje de cobertura.

**Derivación de la fórmula (esto es lo que el roadmap original llamaba "Garman-Kohlhagen" en la ecuación de forward, aunque Garman-Kohlhagen estrictamente es el modelo de pricing de OPCIONES FX; el forward per se se deriva de CIP, y es importante distinguir esto con precisión técnica):**

Considera un inversionista en `t=0` con dos estrategias equivalentes para tener 1 unidad de USD en `t=T`:

**Estrategia A (directa):** Invierte 1 USD hoy a la tasa libre de riesgo en USD (`r_USD`) durante T años. En T, tiene `1 × (1 + r_USD)^T` USD (o `e^(r_USD × T)` bajo composición continua).

**Estrategia B (cubierta, vía COP):**
1. Convierte 1 USD a COP hoy al tipo de cambio spot `S` (COP por USD): obtiene `S` COP.
2. Invierte esos `S` COP a la tasa libre de riesgo en COP (`r_COP`) durante T años: obtiene `S × (1+r_COP)^T` COP en T.
3. Simultáneamente, en `t=0`, contrata un forward para vender esos COP futuros y comprar USD a la tasa forward `F` (COP por USD) en `t=T` — esto fija el resultado en USD sin riesgo cambiario: en T, recibe `[S × (1+r_COP)^T] / F` USD.

Por no arbitraje, ambas estrategias deben rendir lo mismo en `t=T`:

```
(1 + r_USD)^T = [S × (1+r_COP)^T] / F
```

Despejando F:

```
F = S × [(1 + r_COP)^T / (1 + r_USD)^T]
```

O, en composición continua (consistente con la convención adoptada en `nss_discount_factor` de la Fase 3):

```
F = S × e^[(r_COP - r_USD) × T]
```

**Convención de cotización crítica:** USD/COP se cotiza convencionalmente como "cuántos COP por 1 USD" (ej. S ≈ 4,000). En esta convención, COP es la moneda de cotización ("terms currency") y USD es la moneda base ("base currency"). La fórmula de arriba asume que `r_COP` es la tasa doméstica de la moneda de cotización y `r_USD` es la tasa de la moneda base — **esta asignación debe verificarse cuidadosamente contra la convención estándar de forwards NDF (Non-Deliverable Forward) del mercado colombiano**, ya que el USD/COP forward que opera la industria local es típicamente un NDF liquidado en COP (no hay entrega física de USD en el mercado local para la mayoría de contrapartes), lo cual no cambia la fórmula de pricing per se pero sí afecta la mecánica de liquidación (relevante para la Fase 7 de griegas si se calcula P&L de la posición).

**Relación con las tasas de la Fase 5:** `r_COP` se obtiene de `ShortRateCurve` construida con IBR (Fase 5) evaluada en el tenor `T` del forward. `r_USD` se obtiene de la curva SOFR análoga. **No se usa la curva TES (Fase 3-4) directamente para el pricing del forward** — la curva TES es para pricing de bonos soberanos; el forward FX se descuenta con las curvas de tasa "libre de riesgo de corto plazo" de la Fase 5. Esta distinción es exactamente el tipo de precisión técnica que un entrevistador de mesa de derivados evaluará.

### Prompt 6.1 — Implementación del pricer de forward

```
Implementa `src/tes_pricer/math/fx_forward.py`:

from dataclasses import dataclass

@dataclass
class FXForwardQuote:
    spot_rate: float                # S, COP por 1 USD
    forward_rate: float             # F, COP por 1 USD
    forward_points: float           # (F - S) × 10000, convención de mercado en "puntos"
    tenor_years: float
    r_cop: float                    # tasa doméstica usada, E.A.
    r_usd: float                    # tasa extranjera usada, E.A.
    valuation_date: date
    maturity_date: date

def price_fx_forward(
    spot_rate: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date,
    day_count: str = "ACT/365"
) -> FXForwardQuote:
    \"\"\"
    Implementa la Paridad de Tasas de Interés Cubierta (Covered Interest Rate 
    Parity, CIP):
    
    F = S × [(1 + r_COP)^T / (1 + r_USD)^T]     [composición anual E.A.]
    
    donde T = (maturity_date - valuation_date).days / 365 (o el day_count 
    correspondiente, debe ser CONSISTENTE con la convención usada al extraer 
    r_cop y r_usd de las curvas de la Fase 5 — si las curvas están en E.A. 
    (composición anual), usa la fórmula de arriba; si decidiste usar composición 
    continua en toda la Fase 5 en vez de E.A. para consistencia con nss_model.py, 
    usa en su lugar: F = S × exp((r_cop_continua - r_usd_continua) × T). 
    DEBES declarar explícitamente en el docstring cuál convención estás usando 
    y por qué es consistente con el resto del sistema — esta es una fuente 
    de error sutil pero significativa si se mezclan convenciones sin darse 
    cuenta.
    
    r_cop = cop_curve.discount_factor patrón, pero necesitas la TASA no el 
    factor de descuento directamente para esta fórmula — implementa un 
    método auxiliar en ShortRateCurve: `interpolated_rate(tau)` que devuelva 
    la tasa interpolada directamente (la interpolación de tasas ya la 
    implementaste en discount_factor(), solo expón la tasa interpolada como 
    método separado en vez de solo el DF resultante).
    \"\"\"

def implied_forward_points(spot: float, forward: float) -> float:
    \"\"\"Convención de mercado: forward_points = (F - S) × 10000. 
    Los forward points positivos indican que la moneda de cotización (COP) 
    cotiza con descuento a futuro relativo al spot bajo esta convención 
    (es decir, se requieren más COP por USD en el futuro que hoy) — esto 
    ocurre cuando r_COP > r_USD, que es el caso estructural típico dado el 
    diferencial de tasas Colombia vs. Estados Unidos. Verifica el signo 
    resultante contra esta intuición económica como sanity check.\"\"\"

VALIDACIÓN DE SANITY CHECK OBLIGATORIA:
Después de calcular un forward con datos reales, verifica automáticamente 
que el signo de los forward points sea consistente con el signo del 
diferencial de tasas (r_cop - r_usd). Si r_cop > r_usd pero forward_points 
resulta negativo (o viceversa), esto indica un error de signo en la fórmula 
o en la convención de cotización — lanza un warning fuerte, no falles 
silenciosamente, porque este es exactamente el tipo de error que un trader 
detectaría inmediatamente al ver el output pero que un sistema automatizado 
podría propagar sin detectarlo.

TESTS (tests/unit/test_fx_forward.py):
1. Test de caso trivial: si r_cop == r_usd (tasas idénticas, caso sintético), 
   entonces F debe ser exactamente igual a S (sin arbitraje de tasas, sin 
   forward points) — este es el test de sanity check más básico y debe 
   pasar con igualdad exacta salvo error de punto flotante.
2. Test de caso conocido con números redondos: usa spot=4000, r_cop=0.10 
   (10% E.A.), r_usd=0.05 (5% E.A.), T=1.0 año exacto, y verifica el cálculo 
   manual: F = 4000 × (1.10/1.05) = 4190.476... — compara contra este valor 
   calculado a mano en el docstring del test.
3. Test de consistencia de signo: para 5 combinaciones distintas de r_cop 
   vs r_usd (incluyendo r_cop < r_usd, caso hipotético), verifica que el 
   signo de forward_points siempre sea consistente con el signo de 
   (r_cop - r_usd).
4. Test de sensibilidad a T: confirma que forward_rate es una función 
   monótona de T cuando r_cop > r_usd constante (a mayor plazo, mayor 
   forward, dado diferencial de tasas positivo y constante) — usa 
   assertions de monotonía sobre un array de tenores crecientes.
```

**Criterio de aceptación:** los 4 tests pasan; el test de números redondos reproduce el valor calculado a mano con error <0.01 COP; el sanity check de signo está implementado y testeado con un caso que lo dispare intencionalmente.

---

## FASE 7 — Griegas y Matriz de Sensibilidades

### Teoría: qué mide cada griega en este contexto

Para un forward FX (a diferencia de una opción), el perfil de payoff es lineal, no convexo — por tanto **no existe gamma en el sentido de convexidad de opciones**. Sin embargo, el roadmap original especifica delta y gamma respecto al spot, lo cual debe interpretarse correctamente en el contexto de forward (no de opción):

**Delta del forward (∂V/∂S):** el valor presente de una posición forward larga de N unidades USD, contratada a tasa forward `F_contrato` con valor de mercado actual `F_mercado(t)`, es:

```
V(t) = N × [F_mercado(t) - F_contrato] × DF_COP(t, T)
```

Donde `DF_COP(t,T)` descuenta el payoff futuro (en COP) a valor presente. Dado que `F_mercado(t) = S(t) × e^[(r_cop - r_usd) × (T-t)]` (CIP evaluado en el tiempo actual `t`), la derivada respecto al spot es:

```
∂V/∂S = N × e^[(r_cop - r_usd) × (T-t)] × DF_COP(t,T)
```

Esto es **constante respecto a S** — no depende del nivel del spot, solo de las tasas y el tiempo restante. Por tanto, **la gamma de un forward respecto al spot es exactamente cero** — esto NO es un error del sistema, es una propiedad matemática fundamental de instrumentos con payoff lineal. Es crítico que el sistema calcule esto y lo reporte explícitamente como cero (con explicación), en vez de que el usuario asuma que hay un bug si ve gamma=0.

**Lo que SÍ tiene convexidad real y debe reportarse como parte de la "matriz de sensibilidades" (interpretando el roadmap original con rigor, no literalmente):**

- **DV01 del forward respecto a r_COP:** sensibilidad del valor del forward a un movimiento de 1 basis point en la tasa doméstica — esto SÍ es relevante para cobertura de riesgo de tasa.
- **DV01 respecto a r_USD:** análogo para la tasa extranjera.
- **Vega (si se extiende a opciones FX en v2):** fuera de alcance de v1 (el roadmap especifica forwards, no opciones — no se debe expandir el alcance sin que el usuario lo pida explícitamente).

### Prompt 7.1 — Implementación de griegas con derivación analítica y verificación numérica

```
Implementa `src/tes_pricer/math/greeks.py`:

@dataclass
class ForwardGreeks:
    delta_spot: float          # ∂V/∂S
    gamma_spot: float          # ∂²V/∂S², debe ser ~0 para forward (ver nota)
    dv01_cop: float            # sensibilidad a +1bp en r_cop, en unidades COP
    dv01_usd: float            # sensibilidad a +1bp en r_usd, en unidades COP
    theta: float               # ∂V/∂t, decaimiento temporal (el forward SÍ 
                                # tiene theta no trivial porque el descuento 
                                # cambia con el tiempo restante)
    notes: list[str]           # explicaciones, ej. "gamma_spot=0 es esperado 
                                # para instrumentos de payoff lineal, no es un bug"

def compute_forward_greeks(
    notional_usd: float,
    contract_forward_rate: float,
    current_spot: float,
    cop_curve: ShortRateCurve,
    usd_curve: ShortRateCurve,
    valuation_date: date,
    maturity_date: date
) -> ForwardGreeks:
    \"\"\"
    Calcula las griegas usando DOS métodos en paralelo, y los reconcilia:
    
    MÉTODO 1 — ANALÍTICO (fórmula cerrada derivada arriba):
    delta_spot = notional_usd × exp((r_cop - r_usd) × tau_restante) × DF_cop(tau_restante)
    gamma_spot = 0.0  (exacto, por la linealidad del payoff)
    
    MÉTODO 2 — DIFERENCIACIÓN NUMÉRICA (bump-and-reprice, el estándar de 
    verificación en mesas de trading reales):
    delta_numerico = [V(S + h) - V(S - h)] / (2h)     [diferencia central]
    gamma_numerico = [V(S+h) - 2×V(S) + V(S-h)] / h²
    
    usa h = current_spot × 0.0001 (bump de 1bp relativo al spot, convención 
    estándar de mesa para evitar errores de escala con bumps absolutos fijos).
    
    RECONCILIACIÓN: compara delta_analitico vs delta_numerico. Deben coincidir 
    con error relativo <0.1% — si no coinciden, hay un error en la derivación 
    analítica o en la función de pricing V(S), y el sistema debe lanzar una 
    excepción de reconciliación fallida en vez de reportar silenciosamente 
    el valor analítico (potencialmente erróneo).
    
    Para DV01_cop y DV01_usd, usa ÚNICAMENTE bump-and-reprice (no derives 
    analíticamente, es más propenso a error y el bump numérico es suficientemente 
    preciso para sensibilidades de tasa):
    dv01_cop = V(r_cop + 0.0001) - V(r_cop)     [bump de +1bp exacto]
    dv01_usd = V(r_usd + 0.0001) - V(r_usd)
    
    Para theta, usa bump-and-reprice sobre valuation_date avanzando 1 día 
    calendario (manteniendo todo lo demás constante, incluyendo maturity_date 
    fija):
    theta = V(valuation_date + 1 día) - V(valuation_date)
    \"\"\"

TESTS (tests/unit/test_greeks.py):
1. Test de reconciliación analítico vs. numérico para delta: para 5 escenarios 
   distintos de spot/tasas/tenor, confirma que ambos métodos coinciden con 
   error relativo <0.1%.
2. Test de gamma exactamente cero: confirma que gamma_numerico calculado por 
   bump-and-reprice también da un valor cercano a cero (tolerancia dada por 
   el error de redondeo de punto flotante del método de diferencias finitas, 
   típicamente <1e-6 en las unidades del notional) — esto verifica empíricamente 
   la propiedad teórica derivada arriba, no solo confía en la fórmula analítica.
3. Test de signo de DV01: si r_cop sube, el valor de una posición larga en 
   forward (comprando USD a futuro) debe... [razona el signo correcto basado 
   en la fórmula de pricing y verifica que el test capture la dirección 
   correcta — no asumas el signo, derívalo de V(t) y verifícalo].
4. Test de theta con vencimiento inminente: confirma que theta se comporta 
   razonablemente (no diverge a infinito) cuando el forward está a 1 día de 
   vencer.
```

**Criterio de aceptación:** reconciliación analítico-numérico pasa en los 5 escenarios; el test de gamma≈0 pasa confirmando la propiedad teórica empíricamente.

---

## FASE 8 — Suite de Tests Completa e Integración

### Prompt 8.1 — Test de integración end-to-end

```
Implementa `tests/integration/test_end_to_end_pipeline.py`, marcado 
@pytest.mark.integration (requiere red para los datos reales, pero debe 
tener un modo con datos cacheados/fixture para correr en CI sin red).

El test debe ejecutar el pipeline COMPLETO en secuencia:
1. Ingesta de datos TES (usando fixtures de datos reales cacheados de un 
   día específico conocido, ej. una fecha de hace 30 días, para reproducibilidad)
2. Cálculo de YTMs a partir de precios (Fase 2)
3. Calibración NSS con multi-start (Fase 3)
4. Construcción de curvas de corto plazo COP/USD (Fase 5)
5. Pricing de un forward USD/COP a 90 días con notional de USD 100,000 (Fase 6)
6. Cálculo de griegas completas (Fase 7)

Verifica en cada paso que el output tiene sentido económico:
- Los YTMs calibrados están en un rango plausible para Colombia (ej. 5%-15% 
  E.A., ajustar según régimen de tasas del periodo de datos usado).
- El RMSE de calibración NSS es menor a 20bps (umbral más laxo que en datos 
  sintéticos, porque datos reales de mercado tienen ruido de microestructura 
  que un modelo paramétrico de 6 factores no puede capturar perfectamente 
  — esto es esperado, no un fallo).
- El forward calculado tiene forward_points con signo consistente con el 
  diferencial de tasas COP-USD observado ese día.
- Las griegas reconcilian entre método analítico y numérico.

Genera además un reporte HTML o markdown de este test end-to-end (usando 
pytest-html o un script custom) que sirva como EVIDENCIA DEMOSTRABLE del 
sistema funcionando completo — este reporte es el artefacto que se muestra 
en una entrevista técnica, no solo el código.

Configura además `pytest.ini` o la sección [tool.pytest.ini_options] en 
pyproject.toml para:
- Definir markers: unit, integration, benchmark
- Configurar pytest-cov con umbral mínimo de cobertura del 85% para el 
  paquete completo, y 95% específicamente para src/tes_pricer/math/ 
  (el código más crítico)
- Falla el build de CI si la cobertura cae bajo estos umbrales
```

**Criterio de aceptación:** el pipeline end-to-end corre sin excepciones no manejadas; el reporte de cobertura confirma >85% global y >95% en el módulo math/.

---

## FASE 9 — Integración VBA/Excel con xlwings

### Teoría: por qué xlwings y no win32com puro

`xlwings` provee una capa de abstracción sobre COM que permite exponer funciones Python directamente como UDFs (User Defined Functions) de Excel sin escribir VBA manualmente para la lógica de negocio — el VBA generado es principalmente boilerplate de conexión. Esto reduce drásticamente la superficie de código VBA a mantener (crítico, porque VBA no tiene testing automatizado viable comparable a pytest).

### Prompt 9.1 — UDFs expuestas a Excel

```
Implementa `src/tes_pricer/interface/excel_bridge.py` usando xlwings, 
exponiendo exactamente estas 3 funciones como UDFs de Excel (correspondientes 
a las especificadas en el roadmap original):

import xlwings as xw

@xw.func
def TASA_CERO_CUPON(vencimiento_anos: float, fecha_calibracion: str = None) -> float:
    \"\"\"
    UDF de Excel: =TASA_CERO_CUPON(5.0) retorna la tasa spot cero-cupón NSS 
    para un vencimiento de 5 años, usando la última calibración disponible 
    (o la calibración correspondiente a fecha_calibracion si se especifica).
    
    Debe cachear el resultado de calibración más reciente en un archivo local 
    (ej. data/processed/latest_calibration.json) para no re-calibrar en cada 
    llamada de celda de Excel — esto sería prohibitivamente lento si el 
    usuario arrastra la fórmula sobre 50 celdas.
    \"\"\"

@xw.func
def FORWARD_USDCOP(
    monto_usd: float, 
    fecha_vencimiento: str,  # formato "YYYY-MM-DD"
    fecha_valoracion: str = None  # default: hoy
) -> float:
    \"\"\"
    UDF de Excel: =FORWARD_USDCOP(100000, "2026-12-15") retorna la tasa 
    forward COP/USD teórica para ese monto y vencimiento.
    \"\"\"

@xw.func
@xw.arg('cell_range', ndim=2)
def DV01_TES(isin: str, notional: float) -> list[list[float]]:
    \"\"\"
    UDF de Excel que retorna un array (para usar como fórmula matricial en 
    Excel con Ctrl+Shift+Enter, o dynamic array en Excel 365) con: 
    [DV01_COP, duracion_macaulay, duracion_modificada, convexidad] para el 
    bono TES especificado, usando la curva calibrada más reciente.
    
    DV01 (Dollar Value of 01 / Peso Value of 01 en este contexto) se calcula 
    por bump-and-reprice: reprecia el bono con la curva NSS desplazada 
    +1bp paralelamente, y calcula la diferencia de precio × notional.
    \"\"\"

Además, escribe `excel/build_excel_toolkit.py`, un script que:
1. Crea un archivo .xlsm desde cero (o desde una plantilla base) usando 
   xlwings.
2. Configura los add-ins necesarios de xlwings en ese libro (xlwings requiere 
   habilitar su add-in de Excel o incrustar el runtime — documenta el paso 
   manual de configuración que el usuario debe hacer una vez, ya que esto 
   no es 100% automatizable sin intervención del usuario en su instalación 
   de Excel).
3. Crea una hoja "Calculadora_Forward" con celdas de input (monto, fecha 
   vencimiento) y una celda de output que llama a =FORWARD_USDCOP(...).
4. Crea una hoja "Curva_TES" con una tabla de vencimientos de 0.25 a 20 años 
   en pasos de 0.25, cada uno llamando a =TASA_CERO_CUPON(...), y un gráfico 
   de líneas de Excel (xlwings puede manipular gráficos nativos de Excel 
   vía su API de objetos) mostrando la curva.

Documenta en `docs/excel_setup_guide.md` el proceso de instalación paso a 
paso para un usuario que abra el .xlsm por primera vez (instalación de 
xlwings, habilitación de macros, configuración del intérprete Python que 
Excel debe usar).
```

**Criterio de aceptación:** el archivo .xlsm generado abre en Excel sin errores; las 3 UDFs retornan valores numéricos coherentes al ingresarlas manualmente en celdas de prueba; existe documentación de setup reproducible por un tercero.

---

## FASE 10 — Documentación y Narrativa de Entrevista

### Prompt 10.1 — README técnico y narrativa

```
Escribe dos documentos finales:

1. `README.md` — documentación técnica estándar de repositorio profesional:
   - Descripción del proyecto en 2-3 párrafos (qué problema resuelve, para 
     quién)
   - Arquitectura (diagrama en Mermaid o ASCII de las 3 capas: Datos, 
     Matemática, Interfaz)
   - Instrucciones de instalación (poetry install, configuración de .env)
   - Ejemplo de uso end-to-end (código real ejecutable, no pseudocódigo)
   - Sección "Limitaciones Conocidas" que liste EXPLÍCITAMENTE: (a) la 
     dependencia de exportación manual de SUAMECA si el reverse engineering 
     del endpoint no fue viable, (b) la aproximación de curva plana de corto 
     plazo si no se encontraron fuentes públicas de IBR a plazo, (c) la 
     calibración por yields en vez de por precio ponderado por duración, 
     (d) cualquier otra limitación descubierta durante el desarrollo — esta 
     sección es una señal de madurez de ingeniería, no una debilidad a ocultar.
   - Badges de cobertura de tests y estado de CI si están configurados.

2. `docs/NARRATIVE.md` — el documento de preparación para entrevista técnica, 
   estructurado como respuestas anticipadas a las preguntas más probables 
   de un entrevistador técnico de mesa de derivados:
   
   - "Camínenos por su arquitectura" → usa la separación de 3 capas como 
     hilo conductor.
   - "¿Por qué Nelson-Siegel-Svensson y no otro modelo de curva (ej. 
     splines cúbicos)?" → prepara una respuesta que mencione el trade-off 
     real: NSS es paramétrico e interpretable económicamente (los 6 
     parámetros tienen significado: nivel, pendiente, curvatura), mientras 
     que splines son más flexibles para ajuste exacto pero pueden generar 
     tasas forward implícitas oscilantes no económicamente sensatas entre 
     nodos — esto es precisamente el trade-off interpretabilidad-vs-flexibilidad 
     que un comité de riesgo valora poder explicar.
   - "¿Cómo manejó el riesgo de que su optimizador convergiera a un mínimo 
     local?" → explica el multi-start con Latin Hypercube Sampling y por 
     qué el problema no es convexo en lambda1/lambda2.
   - "¿Cómo valida usted que su implementación es correcta?" → el benchmark 
     contra QuantLib con tolerancia de 1bp.
   - "¿Qué pasa si los datos de SUAMECA fallan un día?" → la respuesta 
     honesta sobre el fallback manual y por qué esto es una limitación real 
     del ecosistema de datos públicos colombiano, no un defecto de diseño.
   - Prepara también 2-3 preguntas que el candidato debería hacer de vuelta 
     al entrevistador sobre la infraestructura real de Credicorp Capital 
     (ej. "¿qué fuente de datos usa la mesa internamente para curvas TES 
     en tiempo real?"), demostrando pensamiento crítico sobre el gap entre 
     el proyecto de portafolio y la infraestructura de producción real.

Ambos documentos deben ser HONESTOS sobre las limitaciones — un entrevistador 
técnico senior detecta inmediatamente cuando un candidato infla las 
capacidades de su propio sistema, y la honestidad calibrada sobre limitaciones 
conocidas es en sí misma una señal de madurez profesional más fuerte que 
afirmar que el sistema es perfecto.
```

**Criterio de aceptación:** ambos documentos existen, son específicos al sistema real construido (no genéricos), y la sección de limitaciones refleja hallazgos reales de las fases anteriores.

---

## Notas Finales de Ejecución

**Orden de dependencia estricto:** no saltes fases. La Fase 6 (forward pricer) depende matemáticamente de que la Fase 5 (curvas de corto plazo) esté correctamente validada, que a su vez es independiente de la Fase 3-4 (curva TES/NSS) — nota que **el pricer de forward FX NO depende de la curva TES calibrada**, son dos curvas distintas para propósitos distintos (esto es intencional y se explica en la Fase 6; confirma que tu implementación real respeta esta separación).

**Puntos de mayor riesgo del proyecto, en orden de severidad:**
1. Fase 1 — inestabilidad del endpoint SUAMECA (mitigado con fallback manual documentado)
2. Fase 2 — convención de day count no verificada con fuente primaria (mitigado con el validador que rechaza campos no verificables)
3. Fase 4 — incompatibilidad de QuantLib con Python 3.13 (mitigado fijando versión de Python si es necesario)
4. Fase 3 — convergencia a mínimo local en calibración (mitigado con multi-start obligatorio)

**Registro de decisiones:** mantén un `docs/decision_log.md` actualizado cada vez que se tome una decisión de diseño no trivial durante la ejecución (ej. qué convención de composición se eligió y por qué, qué se hizo cuando SUAMECA resultó inaccesible). Este log es tan valioso para la narrativa de entrevista como el código mismo — demuestra proceso de pensamiento, no solo output final.
