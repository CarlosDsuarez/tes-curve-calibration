# Narrativa técnica — preparación para entrevista

Respuestas anticipadas a las preguntas que un entrevistador técnico de una
mesa de derivados hace sobre este proyecto. No es un guion para recitar: es
el mapa de qué decisiones tomé, por qué, qué medí, y dónde el sistema se
queda corto. Cada respuesta está escrita para poder ser interrumpida en
cualquier frase con "¿y cómo sabe eso?" y tener un test, una medición o una
fecha de verificación que la respalde.

Regla de oro para toda la entrevista: **nunca inflar**. Un entrevistador
senior detecta en segundos un "verificado contra QuantLib" que en realidad
cubre un módulo, o un "curva OIS" que es una interpolación de fixings. La
credibilidad se pierde en una frase y no se recupera en la hora siguiente.
Cada respuesta abajo dice explícitamente qué **no** hace el sistema.

Números que conviene tener en la cabeza sin mirar:

| Dato | Valor |
| --- | --- |
| Universo de calibración | 16 TES tasa fija, 1.22y–31.6y, 14-ago-2026, fuente MinHacienda |
| RMSE del ajuste NSS sobre datos reales | 5.34 bp (umbral 20 bp), 25/25 arranques convergen |
| YTM recalculado vs tasa publicada | máx. 0.066 bp sobre 16 bonos |
| NSS vs QuantLib `SvenssonFitting` (cero cupón sintético) | 6 × 10⁻¹² bp, tolerancia 1 bp |
| Sesgo del objetivo en yields sobre bonos con cupón | 68 bp a 15y, 1 338 bp a 0.5y |
| Costo de confundir κ con λ en QuantLib | 76 bp a 15y |
| Costo de mezclar continua con efectiva anual al 10 % | ≈46 bp de tasa |
| Forward 90d del snapshot | 3 166.75 COP/USD sobre spot 3 127.51 |
| Tests | 352 passed, 21 xfail (stubs), 4 benchmark passed / 6 xfail |

---

## 1. "Camínenos por su arquitectura"

Tres capas, y una regla que las define: **el núcleo numérico nunca toca la
red.**

**Capa de datos (`tes_pricer.data`).** Todo lo que sale a internet vive aquí:
el cliente de SUAMECA (Banco de la República), el cliente Socrata de
datos.gov.co para la TRM, y el loader del export manual de precios. Su
contrato de salida es un `DataFrame` validado con tasas en decimal, y un
registro en `data/manifest.json` con URL, timestamp, ruta del payload crudo
archivado, SHA-256, conteo de filas y rango de fechas. El payload se archiva
y se hashea **antes** de parsearlo, así una falla de parsing deja la
evidencia en disco en lugar de obligar a re-descargar.

**Capa matemática (`tes_pricer.math`).** Funciones puras sobre arrays:
pricing de bonos y YTM por Brent, el modelo NSS con su calibración
multi-start, la curva corta por interpolación de fixings, el forward bajo
CIP y las griegas. Objetos inmutables (`frozen dataclass`) en cada frontera:
`NSSParams`, `ShortRateCurve`, `FXForwardQuote`, `ForwardGreeks`. Cada uno
valida sus invariantes en construcción.

**Capa de interfaz (`tes_pricer.interface`).** Donde el resultado se
entrega: CLI, puente Excel vía `xlwings`, y el test end-to-end que produce un
reporte de evidencia HTML. Aquí tengo que ser directo: **hoy solo el test
end-to-end y la API de librería funcionan**. El CLI tiene los parsers y los
códigos de salida definidos pero los handlers son stubs; el puente Excel es
un stub. El proyecto es una librería con evidencia, no un sistema operativo.

Lo que me importa defender de la arquitectura no es el dibujo sino **cómo se
impone**. La regla "math no toca la red" está escrita tres veces en código,
no una vez en un README:

1. `ruff` con `flake8-tidy-imports` prohíbe importar `requests` y `sodapy`
   fuera de `tes_pricer.data`.
2. Un test unitario parsea el AST de cada módulo de `math/` y falla si
   aparece un import de red.
3. `mypy --strict` solo sobre `math/`: un `Any` que se filtre desde un JSON
   sin tipar es un error de tipos, no un precio silenciosamente mal.

Y la razón es económica, no estética: reproducibilidad. Con el manifest y
los payloads archivados, una calibración se vuelve a correr offline meses
después y da el mismo número. Ese es el requisito que un comité de riesgo o
un auditor va a pedir primero.

*Lo que no diría:* "está listo para producción". No lo está. Diría: "es el
esqueleto de un sistema de producción con las convenciones fijadas y
testeadas, y los stubs marcados con `xfail(strict=True)` para que el suite
se ponga en rojo el día que una implementación llegue sin cumplir el
contrato".

---

## 2. "¿Por qué Nelson-Siegel-Svensson y no splines cúbicos?"

Es el trade-off **interpretabilidad vs. flexibilidad**, y para el uso que le
doy —una curva que un comité de riesgo tiene que poder explicar— elegí
interpretabilidad. Pero conviene ser preciso sobre lo que cada lado compra.

**NSS es paramétrico y sus seis parámetros significan algo.** `β₀` es la
tasa larga asintótica; `β₀ + β₁` es la tasa corta instantánea, así que `β₁`
es la pendiente con signo cambiado; `β₂` y `β₃` son dos jorobas de curvatura,
ubicadas en el tiempo por `λ₁` y `λ₂`. Cuando el ajuste de hoy difiere del de
ayer, se puede decir "subió el nivel 8 bp y la joroba a 3 años se aplanó", y
eso es una frase que un trader entiende y un comité puede cuestionar. Con
seis parámetros, además, el modelo **no puede** ajustar el ruido de
microestructura de 16 bonos: el RMSE de 5.3 bp que obtengo sobre datos
reales es, en parte, el ruido que el modelo se niega a interpolar, y eso es
deseable.

**Los splines cúbicos compran ajuste exacto, y lo pagan en los forwards.**
Un spline interpolante pasa por cada nodo, lo que suena bien hasta que se
deriva: la tasa forward instantánea es `f(τ) = z(τ) + τ·z'(τ)`, y un spline
que serpentea entre nodos para pasar exactamente por ellos produce forwards
que oscilan —a veces con jorobas y valles que no corresponden a ninguna
expectativa de política monetaria, solo a la geometría del interpolador—.
En una curva con 16 nodos irregularmente espaciados (hay un hueco de 4.4
años entre 9.9y y 14.3y en mi universo) esa oscilación es real. Un spline
suavizado con penalización (à la Fisher-Nychka-Zervos o Waggoner) mitiga
esto, pero introduce un parámetro de suavizado que hay que elegir y
justificar, y pierde la interpretabilidad económica sin recuperar del todo
la parsimonia.

**El costo de mi elección, dicho sin rodeos:** NSS es rígido. Con seis
parámetros no puede reproducir una curva con tres jorobas, ni un *kink*
idiosincrático en un vencimiento específico (por ejemplo, un bono que
cotiza rico por ser el *cheapest-to-deliver* o por un efecto de liquidez).
Y en mi snapshot el precio de esa rigidez es visible: el mejor ajuste clava
`β₁` en el bound de −0.5 e implica una tasa corta instantánea de −37 % —no
porque el modelo sea malo, sino porque no hay ningún instrumento por debajo
de 1.22 años que restrinja el tramo corto, y NSS extrapola con lo que tiene.
Un spline con extrapolación plana habría dado un número menos absurdo ahí,
pero igual de infundado. Mi respuesta a eso no es cambiar de modelo, sino
poner un pilar de mercado monetario al frente, que es exactamente lo que la
curva IBR hace para el forward.

**Qué usan los bancos centrales:** la documentación técnica del BIS sobre
curvas cero cupón muestra a la mayoría (Bundesbank, Banco de España, Banco
de Francia, Suiza, Noruega) en NS o Svensson; la Fed publica la curva
Gürkaynak-Sack-Wright, que es NSS; el Banco de Inglaterra usa un spline
suavizado (VRP) y Canadá un spline exponencial. Banco de la República,
según las notas de sus propias series en SUAMECA, ajusta Nelson-Siegel
(1987), tres betas y un tau. Ninguna de las dos
familias es "la correcta"; depende de si la curva se usa para comunicar
(paramétrica) o para valorar un libro con exposiciones a nodos específicos
(spline con suavizado, o un bootstrap directo).

---

## 3. "¿Cómo manejó el riesgo de converger a un mínimo local?"

Primero la naturaleza del problema, porque de ahí sale la solución.

**El objetivo es no convexo, y el culpable son λ₁ y λ₂.** Con los dos
decaimientos fijos, NSS es *lineal* en los cuatro betas: las cargas
`L₁(τ) = (1−e^{−τ/λ₁})/(τ/λ₁)` etc. son números conocidos y el ajuste es
una regresión OLS con solución cerrada. Con los decaimientos libres, el
problema se vuelve multimodal: pares (λ₁, λ₂) muy distintos producen
curvas casi idénticas *dentro* del rango observado y curvas muy distintas
*entre* nodos y en la extrapolación. Además hay una simetría de etiquetado
(intercambiar λ₁↔λ₂ con β₂↔β₃ da el mismo modelo) y una degeneración cuando
λ₁ ≈ λ₂ (el término de Svensson se vuelve redundante y el Jacobiano pierde
rango). Un solo `least_squares` desde un punto arbitrario reporta la cuenca
donde cayó, sin ninguna señal de que otra cuenca ajusta mejor.

Tengo la evidencia de esto medida, no supuesta: en el benchmark contra
QuantLib, su Simplex y su Levenberg-Marquardt sobre los *mismos* datos
exactos encuentran λ₂ = 8.0 y λ₂ = 171.9 respectivamente —21 veces de
diferencia— y las dos curvas difieren 0.107 bp. Dos optimizadores correctos,
dos cuencas, la misma curva.

**Lo que hago:**

1. **Multi-start con Latin Hypercube Sampling** sobre el cuadrado
   (λ₁, λ₂) ∈ [0.1, 15]², 25 puntos por defecto, semilla fija. LHS y no una
   grilla porque una grilla regular puede hacer *alias* con las ubicaciones
   de las jorobas; y no uniforme i.i.d. porque con 25 muestras el azar deja
   huecos. LHS estratifica cada proyección unidimensional: 25 puntos cubren
   25 bandas distintas de λ₁ **y** 25 bandas distintas de λ₂. Cada par se
   ordena λ₁ ≤ λ₂ para romper la simetría de etiquetado.
2. **Un solo seed de betas para todos los arranques**, derivado de la forma
   de la curva observada: β₀ = media del cuartil más largo (no el bono más
   largo solo, porque el tramo largo es el ilíquido y una cotización
   obsoleta no debe fijar el nivel), β₁ = yield más corto − β₀,
   β₂ = β₃ = 0 para que el primer paso de Gauss-Newton decida el signo de
   las jorobas desde los residuos en lugar de que el seed lo imponga.
3. **`scipy.optimize.least_squares` con `method="trf"`**, el único de los
   tres métodos que respeta *bounds*. Los bounds son |β| ≤ 0.5 (guardia
   numérica, no económica: sin ella el optimizador se va a regiones donde un
   β₁ enorme se cancela con un β₂ enorme, ajusta in-sample, extrapola
   absurdo y estanca la región de confianza en un Jacobiano casi singular)
   y λ ∈ [0.01, 30]. `x_scale` separa los betas (~0.1) de los lambdas (~1–10)
   porque sin escalar la región de confianza es esférica en un espacio donde
   una dirección es 100× más gruesa que otra.
4. **Selección por menor RMSE entre los arranques que convergieron**, y si
   ninguno convergió, `NSSCalibrationError` con los mensajes del solver, no
   un resultado parcial. Un vector de parámetros no convergido descuenta
   flujos igual de bien que uno convergido, y nada aguas abajo lo notaría.
5. **Diagnósticos adjuntos al resultado**: Svensson degenerado
   (|λ₁−λ₂|/λ₁ < 5 %), curva plana, β o λ clavado en un bound, ajuste
   subdeterminado (< 6 bonos). El snapshot real dispara dos de ellos, y los
   reporto en lugar de silenciarlos.

**Lo que no resuelve:** la unicidad de los *parámetros*. Por eso el criterio
de aceptación en el benchmark se escribe sobre **tasas spot**, no sobre
β y λ: 1 bp en datos sintéticos sin ruido (donde toda cuenca que ajusta es
la misma curva), 5 bp presupuestado para datos reales. Y por eso la semilla
es fija: una calibración que devuelve una curva distinta en cada llamada no
es reproducible, y la reproducibilidad es el punto de todo el diseño.
Variar la semilla explícitamente es cómo se sondea la estabilidad de un
ajuste, no algo que deba pasar solo.

*Si preguntan por qué no un optimizador global (differential evolution,
basin-hopping):* porque el problema es lineal en cuatro de seis
dimensiones, así que la multimodalidad vive en un plano. 25 arranques
locales sobre ese plano con un solver que explota la estructura de mínimos
cuadrados convergen en menos de un segundo; un optimizador global de caja
negra es más lento y no aprovecha que, fijado λ, el problema tiene solución
cerrada. Una mejora natural sería justamente esa: perfilar sobre (λ₁, λ₂)
resolviendo los betas por OLS en cada evaluación —el problema queda en dos
dimensiones y se puede incluso graficar la superficie del objetivo—.

---

## 4. "¿Cómo valida que su implementación es correcta?"

Cuatro capas de evidencia, de la más fuerte a la más débil, y **el alcance
exacto de cada una**, porque aquí es donde más fácil es sobrevender.

**(i) Benchmark contra QuantLib — solo la curva NSS.** `nss_zero_rate`
reproduce `QuantLib.SvenssonFitting` sobre un universo de bonos cero cupón
sintéticos con una discrepancia de 6 × 10⁻¹² bp, con tolerancia declarada de
1 bp. Seis órdenes de magnitud de margen: si ese test falla es una
discrepancia real, nunca ruido del optimizador. Por el camino tuve que
establecer empíricamente dos convenciones que QuantLib no documenta bien y
que ahora están fijadas por tests cerrados, sin optimizador: que QuantLib
parametriza los decaimientos como *tasas* κ = 1/λ (leerlo como λ mueve el
punto a 15 años 76 bp), y que ambos lados están en composición continua.

Lo que **no** está benchmarkeado contra QuantLib: el pricing de bonos, el
YTM, duración/convexidad, el bootstrap OIS y Garman-Kohlhagen. Esos tests
existen como `xfail` de Fase 10. Decirlo yo antes de que lo pregunten es
más barato que dejar que lo descubran.

**(ii) Validación contra la fuente primaria — pricing y YTM.** El pipeline
toma solo el precio limpio de cada TES, invierte el YTM por Brent, y
compara contra la columna *Tasa* del Informe Diario de MinHacienda, que se
mantiene fuera del pipeline como control independiente. Máximo 0.066 bp de
error sobre los 16 bonos. Eso fija simultáneamente el calendario de
cupones, el devengo ACT/365 y la convención de liquidación. De hecho, ahí
descubrí que la tasa publicada está cotizada a la fecha del precio y no a
T+3: a T+3 los dos bonos cortos fallan por 5–7 bp. Ese hallazgo está
afirmado en el test, no dejado como comentario.

**(iii) Reconciliación interna — las griegas.** `compute_forward_greeks`
calcula cada sensibilidad dos veces: en forma cerrada y por
bump-and-reprice sobre la función de valoración. Si difieren más de la
tolerancia, **lanza** `GreeksReconciliationError` en lugar de devolver el
analítico. Delta reconcilia a 4 × 10⁻¹⁵ relativo; los DV01 a 6 × 10⁻⁵
(el bump lleva el término de segundo orden de 1 bp). Un número de riesgo sin
verificar no se queda quieto: alguien lo cubre.

**(iv) Propiedades económicas — el test end-to-end.** Sobre el snapshot del
14 de agosto, 30 aserciones sobre propiedades que deben cumplirse
independientemente del nivel de los inputs: el signo de los puntos forward
coincide con el del diferencial de tasas; `F = S·DF_usd/DF_cop` a 10⁻¹⁶; el
carry anualizado es `(1+r_cop)/(1+r_usd) − 1` y no `exp(r_cop − r_usd) − 1`
(difieren 35.8 bp en esta curva — ese test es el que atrapa una fuga de
composición continua); un forward al precio CIP vale cero el día uno; gamma
es exactamente cero porque el payoff es afín; un largo en USD es corto el
cero COP y largo el cero USD. Cada corrida escribe un reporte HTML con
inputs, outputs, umbral y valor observado por aserción, y CI lo sube como
artefacto.

**Y la validación que me falta y sé que me falta:** un solo día de datos.
No tengo estabilidad temporal de parámetros, ni residuos fuera de muestra,
ni un backtest. Con acceso a una serie de precios por bono —que es
exactamente lo que no es público— lo primero que haría es correr la
calibración 250 días y mirar la serie de (β, λ) y del RMSE.

---

## 5. "¿Qué pasa si los datos de SUAMECA fallan un día?"

Depende de *qué* dato, y la respuesta honesta empieza por corregir la
premisa: **los precios por bono nunca vienen de SUAMECA**, así que ese es el
dato que "falla" todos los días.

**Lo que SUAMECA sí publica** —y que mi cliente sirve, verificado en vivo—
es la curva cero cupón ajustada por BanRep a 1/5/10 años, sus cuatro betas
de Nelson-Siegel, los spreads bid-ask, y el IBR a overnight, 1M, 3M, 6M y
12M. Si eso falla un día, el cliente reintenta con backoff exponencial
sobre 429/5xx, y si sigue fallando lanza. No devuelve un DataFrame vacío,
porque un DataFrame vacío se parece demasiado a "no hubo operaciones hoy".
Y valida el transporte antes de confiar en el cuerpo, porque el host tiene
tres modos de fallo que devuelven HTTP 200: un path mal escrito devuelve
200 con el shell HTML del SPA; un id de serie inexistente devuelve 200 con
un JSON centinela `isSerie: "NO"` sin clave `data`; y sin `idSerie` devuelve
un 500 de Tomcat con HTML. Un parser que mire solo el status code vería
"éxito" y luego cero filas —el fallo silencioso más peligroso del proyecto—.

**Lo que SUAMECA no publica** son los precios por ISIN. Banco de la
República consume las cintas de SEN y MEC y publica solo el *fit*; las
cotizaciones subyacentes son propiedad de las bolsas y se venden (Precia,
Infovalmer, BVC). Hice la ingeniería inversa completa del SPA —los dos
servicios REST, el inventario de 16 series TES, los endpoints que parecen
correctos y devuelven `data: []`— y la conclusión está en
`docs/suameca_reverse_engineering.md`: no existe, en ningún endpoint, a
ninguna fecha. Por eso `fetch_tes_prices()` lee un export manual CSV/XLSX
del terminal, lo valida con el mismo esquema que el camino de red, y lo
registra en el manifest con su hash.

**Por qué esto es una limitación del ecosistema y no de diseño.** En un
mercado desarrollado, un proyecto así tendría TRACE, o los precios de
cierre del Tesoro publicados, o al menos una API de un venue. En Colombia,
el dato público de precios de deuda pública es un PDF diario de MinHacienda
con una tabla que hay que transcribir a mano; ese PDF es mi fuente primaria
para el único día del que tengo precios citados. El diseño hace lo correcto
con esa realidad: falla ruidosamente cuando no hay export, no inventa un
precio, y deja la trazabilidad (SHA-256, fecha, fuente) para que cualquiera
pueda auditar de dónde salió cada número. Lo que no puede hacer es
convertir un dato que no es público en uno que lo es.

**Y el matiz que aprendí tarde:** el diseño original asumía que el IBR a
plazo también era pago. Es falso —lo verifiqué el 8 de septiembre contra el
endpoint en vivo, series 15325/15326/16561/16563—, así que el *fallback* a
curva plana en COP es un camino de fetch degradado, no una limitación
permanente. Pero todavía no he cableado esas series a un fixture con
verificación de fuente primaria; los pilares IBR del test e2e son supuestos
de escenario, y el reporte los marca `SUPUESTO`. Para USD la situación sí es
estructural: los SOFR Averages de la Fed de NY se componen en arrears (no
son pilares de descuento) y Term SOFR es licenciado, así que ahí la curva
plana con un warning es el default realista sin un feed pago.

---

## 6. Otras preguntas probables, en corto

**"¿Por qué calibra en yields y no en precios?"** Porque es el estándar
pedagógico (Nelson-Siegel 87, Diebold-Li 06) y porque en la v1 quería un
residuo que se lea en bp. Pero sé que es lo incorrecto para una mesa: la
curva descuenta flujos, así que el error que importa es el de precio, y el
YTM de un bono con cupón es una mezcla ponderada de toda la curva, no el
cero a su vencimiento. Lo medí: sobre bonos sintéticos con cupón, QuantLib
en espacio de precios recupera los parámetros exactos y mi ajuste en yields
queda 68 bp desviado a 15 años. Hay un segundo error apilado: `nss_yield`
es continua y el YTM es efectiva anual, y no aplico `z = ln(1+y)` —38 bp al
9 %—. La v2 está especificada: residuos de precio ponderados por inversa de
duración modificada, con la duración calculada una vez desde el yield
observado para que el problema siga siendo mínimos cuadrados plano. El
multi-start y los diagnósticos no cambian.

**"¿Por qué la curva NSS es continua pero la curva corta es efectiva
anual?"** Porque cada una está en la base de su consumidor natural. NSS en
continua hace que los forwards sean diferencias de log-DF y que el forward
instantáneo sea una derivada analítica —lo que necesitan las griegas de
curva—. La curva corta está en efectiva anual ACT/365 porque es la base en
que BanRep publica el IBR ("tasa efectiva, base 365") y no quiero convertir
un dato de mercado en el momento de ingerirlo. La regla es que **nunca se
mezclan implícitamente**: la conversión es explícita en la frontera, y el
test e2e tiene una aserción diseñada para atrapar la fuga (el carry
compuesto vs. exponenciado, 35.8 bp de diferencia en esta curva).

**"¿Por qué `raise` en lugar de devolver vacío o `NaN`?"** Porque en un
pipeline de valoración, un valor faltante que no explota se convierte en un
precio. Un DataFrame vacío se agrega a cero; un `NaN` en un pilar se propaga
a un DF que alguien redondea. Prefiero que la corrida diaria falle con un
mensaje que dice qué serie, qué fecha y qué esperaba, a que produzca una
curva a la que le falta el tramo largo. El script diario está diseñado
fail-closed por la misma razón.

**"Si tuviera una semana más, ¿qué haría primero?"** En este orden: (1)
cablear las series IBR de SUAMECA al fixture e2e para que los pilares COP
pasen de `SUPUESTO` a `FUENTE_PRIMARIA`; (2) la calibración en precio
ponderado por duración, que destraba el benchmark estricto sobre bonos con
cupón; (3) el módulo `day_count` con el calendario colombiano, para que las
fechas de cupón se ajusten por día hábil. Los tres tienen tests `xfail` ya
escritos que definen el contrato.

**"¿Cuál es el bug más caro que encontró?"** No fue un bug mío, fue una
convención ajena: QuantLib parametriza el decaimiento de Svensson como
κ = 1/λ, y ningún docstring lo dice. Leído como λ, el benchmark daba 76 bp
de discrepancia a 15 años y parecía que mi curva estaba mal. Lo resolví
comparando dos curvas cerradas sin optimizador, y ese test quedó como guardia
permanente. La lección: cuando dos implementaciones difieren, auditar en
orden convención de decaimiento, base de composición, signo de cada carga —
antes de tocar una tolerancia.

**"¿Cuánto tarda?"** La calibración con 25 arranques sobre 16 bonos, del
orden de un segundo. El suite offline completo (unit + e2e), unos 30 s en
un portátil. No hay nada aquí que necesite optimización de rendimiento; lo
que hay que optimizar es la confianza.

---

## 7. Preguntas para hacer de vuelta

Las que muestran que entiendo el hueco entre un proyecto de portafolio y la
infraestructura de una mesa real. Elegir dos o tres según cómo vaya la
conversación; no las cinco.

1. **"¿Qué fuente usa la mesa para precios de TES por ISIN en tiempo real y
   para la curva de cierre? ¿Precia, Infovalmer, el feed directo de SEN/MEC,
   o una combinación con Bloomberg?"** — Es la pregunta que mi limitación
   (a) obliga. La respuesta me dice si el problema de datos que yo tuve
   existe en producción (no: lo compran) y cómo reconcilian el precio
   ejecutable con el precio de valoración regulatorio (Precia/Infovalmer
   son proveedores de precios para valoración bajo la normativa SFC, y no
   necesariamente coinciden con dónde se puede operar).

2. **"¿La curva de descuento COP de la mesa se construye desde swaps IBR par
   —y hasta qué plazo hay liquidez suficiente—, o desde fixings? ¿Y cómo
   manejan la base cross-currency USD/COP: la cotizan, la implican de los
   forwards, o la asumen cero?"** — Mi `ois_curve` interpola fixings y mi
   forward asume CIP sin base. Quiero saber cuál de las dos simplificaciones
   es la que más cuesta en su libro, y a qué plazo la curva IBR deja de ser
   observable y pasa a ser un modelo.

3. **"¿Cómo valida el equipo un modelo de curva antes de que entre en
   producción —hay un benchmark contra QuantLib, contra el vendor, contra
   el modelo de riesgo—, y quién es dueño de las tolerancias?"** — Mi
   benchmark cubre solo la curva NSS con 1 bp/5 bp; quiero saber si esa
   granularidad de tolerancia es la que usan y quién decide cuándo una
   discrepancia es un hallazgo y cuándo es ruido.

4. **"¿La calibración es paramétrica (NS/NSS) o por bootstrap/spline? Y si
   es paramétrica, ¿qué hacen con el tramo corto cuando el TES más corto
   está a más de un año?"** — Mi snapshot dispara exactamente ese problema
   (tasa corta instantánea de −37 % sin instrumento por debajo de 1.22y).
   La respuesta me dice si lo anclan con IBR, con TES de corto plazo (TCO),
   o si simplemente no usan la curva NSS por debajo de cierto plazo.

5. **"¿Qué tan a menudo cambian los endpoints o formatos de sus fuentes
   públicas (BanRep, SFC, datos.gov.co), y quién es dueño de ese
   mantenimiento?"** — Mi cliente SUAMECA depende de internals de un SPA
   verificados a una fecha. Quiero saber si en producción eso se considera
   aceptable con un canario, o si la regla es "solo feeds con contrato".

---

## 8. Lo que no debo decir

Lista corta de afirmaciones que serían técnicamente falsas o infladas, y la
versión honesta de cada una.

| No decir | Decir |
| --- | --- |
| "Está validado contra QuantLib" | "La curva NSS está validada contra QuantLib a 6e-12 bp; bond pricing y YTM están validados contra MinHacienda a 0.07 bp; OIS y GK no tienen benchmark todavía" |
| "Construyo la curva OIS COP" | "Interpolo fixings IBR publicados en tasa; el bootstrap desde swaps par es un stub porque esos swaps no son públicos" |
| "El sistema corre diariamente" | "La librería y el test e2e corren; el CLI y el script diario tienen contrato pero no implementación" |
| "Uso datos reales" | "Uso un día de precios reales con cita, TRM real, y pilares de curva corta que son supuestos marcados como tales" |
| "El ajuste es de 5 bp" | "El ajuste es de 5.3 bp in-sample entre 1.2 y 31.6 años, y el tramo corto extrapolado es inutilizable, cosa que el módulo reporta" |
| "Hice ingeniería inversa de SUAMECA para obtener precios" | "Hice ingeniería inversa de SUAMECA y demostré que los precios por bono no están ahí; lo que obtuve fue la curva ajustada, los betas y el IBR por plazo" |
| "Los parámetros NSS son estables" | "No lo sé: tengo un solo día. Sé que la curva es estable entre optimizadores a 0.1 bp aunque los parámetros no lo sean" |
