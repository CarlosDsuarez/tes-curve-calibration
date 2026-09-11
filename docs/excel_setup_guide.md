# Guía de instalación: `tes_toolkit.xlsm`

Cómo dejar funcionando, en una máquina nueva, el libro de Excel que expone las
tres funciones del proyecto como fórmulas de celda:

| Fórmula | Devuelve |
| --- | --- |
| `=TASA_CERO_CUPON(vencimiento_anos; [fecha_calibracion])` | Tasa spot cero cupón NSS (decimal) a ese plazo. |
| `=FORWARD_USDCOP(monto_usd; fecha_vencimiento; [fecha_valoracion])` | Tasa forward COP por USD por paridad cubierta de tasas. |
| `=DV01_TES(isin; notional)` | Fila de cuatro celdas: DV01 en COP, duración Macaulay, duración modificada, convexidad. |

El libro no contiene lógica de negocio. Cada fórmula llama, a través del
complemento (add-in) de xlwings, a `src/tes_pricer/interface/excel_bridge.py`,
que a su vez delega en `tes_pricer.math`. Por eso hay que instalar Python y el
proyecto en la máquina donde corre Excel: **Excel no calcula nada por sí solo**.

> **Lea esto primero: las UDFs de xlwings solo funcionan en Excel para
> Windows.** En macOS el complemento permite construir el libro y ejecutar
> macros `RunPython`, pero las funciones de celda no se registran y las fórmulas
> muestran `#NAME?`. Si trabaja en Mac, use la línea de comandos del proyecto o
> una máquina virtual con Windows. Esta es una limitación de xlwings, no de este
> repositorio.

---

## 0. Qué se automatiza y qué no

| Paso | Automatizado | Cómo |
| --- | --- | --- |
| Instalar el proyecto y sus dependencias | Sí | `poetry install` |
| Calibrar y guardar la curva que leen las fórmulas | Sí | `python -m tes_pricer.interface.calibration_cache` |
| Construir el `.xlsm` con sus hojas, fórmulas y gráfico | Sí (requiere Excel instalado) | `python excel/build_excel_toolkit.py` |
| Escribir en el libro qué Python usar (`xlwings.conf`) | Sí | el mismo script |
| Instalar el complemento de xlwings en **su** Excel | **No** — una vez por máquina | `xlwings addin install` (sección 3) |
| Habilitar macros y el acceso al modelo de objetos VBA | **No** — configuración de Office | Centro de confianza (sección 3) |
| Registrar las tres funciones en Excel ("Import Functions") | **No** — un clic al abrir el libro | cinta xlwings (sección 5) |

Los pasos no automatizables tocan la instalación de Office del usuario y
requieren su intervención explícita; ningún script de este repositorio los
ejecuta a sus espaldas.

---

## 1. Requisitos

- **Windows 10/11** con **Microsoft Excel de escritorio** (2016 o posterior;
  Microsoft 365 recomendado: las matrices dinámicas de `DV01_TES` se derraman
  solas). Excel Online y Excel para Mac no ejecutan UDFs.
- **Python 3.11 o 3.12** (`pyproject.toml` fija `>=3.11,<3.13`). Instálelo desde
  python.org marcando *Add python.exe to PATH*, o use el que ya tenga. Anote su
  ruta: la necesitará en la sección 4.
- **Poetry** (`pipx install poetry`) — o `pip` si prefiere un venv manual; la
  guía muestra Poetry porque es lo que usa CI.
- **Git**, para clonar el repositorio.

---

## 2. Instalar el proyecto

```bash
git clone <url-del-repositorio> "TES Curve Calibration"
cd "TES Curve Calibration"
poetry install --with dev,test
```

`xlwings` es dependencia principal del proyecto, así que queda instalado en el
entorno virtual de Poetry. Anote la ruta del intérprete de ese entorno, que es
la que Excel deberá lanzar:

```bash
poetry env info --executable
```

Devuelve algo como `C:\Users\usted\AppData\Local\pypoetry\Cache\virtualenvs\tes-curve-calibration-XXXX-py3.12\Scripts\python.exe`.

Verifique que la suite pasa antes de seguir; si falla aquí, fallará dentro de
Excel con un mensaje mucho menos legible:

```bash
poetry run pytest
```

---

## 3. Instalar el complemento de xlwings (una vez por máquina)

Con Excel **cerrado**:

```bash
poetry run xlwings addin install
```

Esto copia `xlwings.xlam` a la carpeta `XLSTART` de su perfil y, al abrir Excel,
aparece la pestaña **xlwings** en la cinta. Si no aparece: *Archivo →
Opciones → Complementos → Administrar: Complementos de Excel → Ir…* y marque
`xlwings`.

Luego, en *Archivo → Opciones → Centro de confianza → Configuración del Centro
de confianza*:

1. **Configuración de macros**: *Deshabilitar macros de VBA con notificación*
   (la opción por defecto; bastará con pulsar *Habilitar contenido* al abrir el
   libro). No hace falta habilitar todas las macros.
2. **Confiar en el acceso al modelo de objetos de proyectos de VBA**: márquelo.
   El botón *Import Functions* de xlwings escribe los envoltorios VBA de las
   funciones dentro del libro y sin este permiso falla con
   *"Programmatic access to Visual Basic Project is not trusted"*.

Si su organización bloquea alguna de estas opciones por directiva, el libro no
podrá registrar las funciones; hable con su administrador antes de continuar.

---

## 4. Calibrar y construir el libro

### 4.1 Generar la calibración que leen las fórmulas

Las fórmulas **nunca calibran**: leen `data/processed/latest_calibration.json`,
que hay que producir una vez (y cada vez que quiera una curva nueva):

```bash
poetry run python -m tes_pricer.interface.calibration_cache
```

Sin argumentos calibra el único día de mercado con fuente primaria que
contiene el repositorio, el 14 de agosto de 2026
(`tests/fixtures/e2e_20260814/`). Para otro día pase su propio archivo de
precios y de mercado:

```bash
poetry run python -m tes_pricer.interface.calibration_cache --date 2026-09-30 --prices ruta/a/tes_precios_2026-09-30.csv --market ruta/a/market_snapshot_2026-09-30.yaml
```

Se escriben dos archivos en `data/processed/`: `calibration_2026-09-30.json`
(se conserva) y `latest_calibration.json` (copia del más reciente). Ambos
están en `.gitignore`: son datos, no código.

### 4.2 Construir `excel/tes_toolkit.xlsm`

Requiere Excel instalado en la máquina que ejecuta el script, porque xlwings
escribe el libro a través de Excel (es la única forma de crear el gráfico
nativo y un `.xlsm` válido). Excel se abre en segundo plano y se cierra solo.

```bash
poetry run python excel/build_excel_toolkit.py --seed-cache
```

- `--seed-cache` ejecuta el paso 4.1 si `latest_calibration.json` no existe.
- `--interpreter RUTA` fija el Python que Excel lanzará. Por defecto es el
  mismo con el que ejecuta el script — que, con `poetry run`, es el del entorno
  virtual, así que normalmente no hace falta.
- `--template libro.xlsm` parte de una plantilla en vez de un libro en blanco.
- `--visible` muestra Excel mientras construye, útil para depurar.

El script escribe cuatro hojas:

| Hoja | Contenido |
| --- | --- |
| `xlwings.conf` | Ruta del intérprete (`Interpreter_Win`/`Interpreter_Mac`), `PYTHONPATH` apuntando a `src/`, y `UDF Modules = tes_pricer.interface.excel_bridge`. Es lo que el complemento lee al abrir el libro. |
| `Calculadora_Forward` | Monto en USD, fecha de vencimiento y fecha de valoración (opcional) en azul; `=FORWARD_USDCOP(B4,B5,B6)` en B8 y el monto en COP en B9. |
| `Curva_TES` | Vencimientos de 0,25 a 20 años en pasos de 0,25 (80 filas) con `=TASA_CERO_CUPON(A2)`… y un gráfico de la curva. |
| `Riesgo_TES` | Nemotécnico y nominal en COP; `=DV01_TES(B4,B5)` derrama las cuatro cifras en B8:E8. |

> **En macOS**, aunque las UDFs no funcionen, el script sí construye el libro
> (para llevarlo a Windows). macOS pedirá permiso para que Python controle
> Excel: *Configuración del Sistema → Privacidad y seguridad → Automatización*.
> Si el terminal desde el que ejecuta no tiene ese permiso, el script falla con
> `OSERROR: -1743 The user has declined permission`; concédalo y vuelva a
> ejecutar.

---

## 5. Abrir el libro por primera vez

1. Abra `excel/tes_toolkit.xlsm`. Pulse **Habilitar contenido** en la barra
   amarilla (macros) si aparece.
2. Vaya a la hoja `xlwings.conf` y compruebe que `Interpreter_Win` es la ruta
   de la sección 2 y que `PYTHONPATH` apunta a la carpeta `src` **de esta
   máquina**. Si clonó el proyecto en otra ruta, corrija ambas celdas (o vuelva
   a ejecutar `build_excel_toolkit.py --interpreter ...`).
3. En la pestaña **xlwings** de la cinta pulse **Import Functions**. Excel
   lanza el Python configurado, importa `tes_pricer.interface.excel_bridge` y
   registra `TASA_CERO_CUPON`, `FORWARD_USDCOP` y `DV01_TES`. Este paso hay que
   repetirlo solo si cambia la firma de una función en Python.
4. Recalcule todo con **Ctrl+Alt+F9**. Las celdas de `Curva_TES` deben
   llenarse con tasas; `Calculadora_Forward!B8` con la tasa forward.
5. Guarde. Los envoltorios VBA quedan dentro del `.xlsm`.

Para comprobar que todo está bien cableado, escriba en cualquier celda:

```text
=TASA_CERO_CUPON(5)
```

Debe devolver un decimal del orden de `0,11`–`0,13` para la calibración del
14-ago-2026.

---

## 6. Uso diario

- **Nueva curva**: ejecute el paso 4.1 con los archivos del día y recalcule
  (`Ctrl+Alt+F9`). No hace falta reiniciar Excel ni reimportar funciones: el
  puente detecta que `latest_calibration.json` cambió por su fecha de
  modificación y lo vuelve a leer una sola vez para todo el libro.
- **Curva de otro día**: `=TASA_CERO_CUPON(5; "2026-08-14")` lee
  `calibration_2026-08-14.json`. Si ese archivo no existe, la celda muestra un
  error que nombra el comando para generarlo.
- **Fechas**: las funciones aceptan texto `"AAAA-MM-DD"` o una celda con formato
  de fecha; las dos formas dan el mismo resultado.
- **Tasas**: todas las salidas son decimales (`0,1123` = 11,23 %). Dé formato
  de porcentaje a la celda; no multiplique por 100 dentro de la fórmula.
- **Identificadores de bono**: `DV01_TES` acepta el nemotécnico
  (`TFIT16300632`, sin distinguir mayúsculas) o un ISIN si `tes_referencia.yaml`
  lo registra. Hoy el archivo trae `isin: null` en los 16 TES porque ninguna
  fuente abierta lo publica; el identificador autoritativo es el nemotécnico.
  Un identificador desconocido produce un error que lista el universo válido.

---

## 7. Solución de problemas

| Síntoma | Causa probable | Qué hacer |
| --- | --- | --- |
| `#NAME?` en todas las fórmulas | Funciones no importadas, o macros bloqueadas, o Excel para Mac. | Sección 5 paso 3; *Habilitar contenido*; use Windows. |
| Al pulsar *Import Functions*: *"Programmatic access to Visual Basic Project is not trusted"* | Falta la opción del Centro de confianza. | Sección 3, punto 2. |
| *"Could not find Python interpreter"* / *"python.exe not found"* | `Interpreter_Win` apunta a una ruta inexistente. | Corrija `xlwings.conf` con la salida de `poetry env info --executable`. |
| *"ModuleNotFoundError: No module named 'tes_pricer'"* | `PYTHONPATH` no apunta a `src/`, o el entorno no tiene el proyecto instalado. | Corrija `PYTHONPATH`; o `poetry install` en ese entorno. |
| `#VALUE!` con mensaje *"no calibration snapshot at …"* | No se ha ejecutado la calibración. | Sección 4.1. |
| `#VALUE!` con mensaje *"… is not in the calibration universe …"* | Nemotécnico no incluido en la calibración cargada. | Use uno de los listados en el mensaje. |
| `#VALUE!` con *"unsupported calibration snapshot schema_version"* | El JSON lo escribió una versión distinta del proyecto. | Vuelva a ejecutar la sección 4.1. |
| Excel se cuelga varios segundos en cada recálculo | `Use UDF Server` en `False` lanza un Python por llamada. | Ponga `Use UDF Server` en `True` en `xlwings.conf` y reimporte. El puente cachea la calibración en memoria, así que con el servidor activo un recálculo completo es casi instantáneo. |
| Usa Anaconda/Miniconda | El intérprete de un entorno conda necesita su activación. | En `xlwings.conf` añada `Conda Path` (carpeta raíz de conda) y `Conda Env` (nombre del entorno) y deje `Interpreter_Win` en `python`. |
| En macOS: `OSERROR: -1743 The user has declined permission` al construir | Permiso de Automatización denegado. | *Privacidad y seguridad → Automatización*: permita que su terminal controle Microsoft Excel. |

Para ver el error completo de Python en vez de `#VALUE!`, ponga `Show Console`
en `True` en `xlwings.conf` y reimporte: se abrirá una consola con la traza.

---

## 8. Limitaciones que conviene conocer antes de usar los números

- **Curvas cortas de la calibración de ejemplo**: los pilares IBR y SOFR del
  snapshot de 14-ago-2026 son *supuestos de escenario*, no fixings publicados
  (`tests/fixtures/e2e_20260814/PROVENANCE.md`). El signo de los puntos forward
  es correcto; su nivel no es de mercado. Sustitúyalos con sus propios datos en
  el YAML de mercado.
- **`FORWARD_USDCOP` con `fecha_valoracion` distinta de la fecha de
  calibración** lee las curvas observadas ese día sin desplazarlas: es "la
  última curva disponible", no una curva del día de valoración. No se emite
  advertencia porque una celda no puede mostrarla.
- **Base de composición**: la curva NSS es de composición continua; las tasas
  que devuelve `TASA_CERO_CUPON` están en esa base. Bajo esa base la duración
  modificada de `DV01_TES` coincide con la de Macaulay (la relación
  `D_mod = D_mac / (1 + y)` es la forma de composición discreta); se calculan por
  caminos independientes y se reportan por separado.
- **Diagnósticos de la calibración** (RMSE, advertencias del optimizador) no
  se exponen en Excel: están en `latest_calibration.json`, bloque
  `diagnostics`. Revíselos antes de confiar en el tramo corto de la curva.
