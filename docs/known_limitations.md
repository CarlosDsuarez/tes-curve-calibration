# Known limitations

Limitations that are **decisions or measured facts**, not open bugs. Each entry
says what the limitation is, what it costs in basis points where that is
measurable, and what would have to change to lift it.

Anything here that is merely unimplemented is tracked as a `NotImplementedError`
in the source, not as an entry below.

---

## 1. QuantLib-Python compatibility

**Status: no limitation. Verified 2026-09-08 against QuantLib 1.43.**

Recorded because the question recurs, and because the answer changed: for
several releases after each new CPython, QuantLib-Python shipped no matching
wheel and a source build needed the QuantLib C++ library and SWIG. That gap is
gone. QuantLib now publishes **stable-ABI (`abi3`) wheels**:

```
quantlib-1.43-cp39-abi3-macosx_11_0_arm64.whl
quantlib-1.43-cp39-abi3-macosx_10_13_x86_64.whl
quantlib-1.43-cp39-abi3-manylinux_2_28_{x86_64,aarch64,i686}.whl
quantlib-1.43-cp39-abi3-musllinux_1_2_{x86_64,aarch64,i686}.whl
quantlib-1.43-cp39-abi3-win{32,_amd64}.whl
```

A `cp39-abi3` wheel is installable on **CPython 3.9 and every later version**,
3.13 and 3.14 included. There are additionally free-threaded `cp314t` wheels and
PyPy 3.11 wheels. So there is no version of Python this project could target on
which QuantLib-Python would have to be built from source.

Consequences for `pyproject.toml`:

* `requires-python` stays at `>=3.11,<3.13`. The `<3.13` ceiling is **not**
  QuantLib's - it predates these wheels and is owned by the rest of the
  dependency set. Raising it is a separate exercise in checking `xlwings`,
  `sodapy` and the numpy 2.x stubs, and it is not blocked by the referee.
* The `QuantLib = "^1.36"` test-group constraint is satisfied by 1.43.

Verified with:

```bash
python -m pip index versions QuantLib     # 1.43, 1.42.1, ... 1.34
python -c "import QuantLib as ql; print(ql.__version__)"   # 1.43
```

on CPython 3.12.13, macOS arm64.

---

## 2. QuantLib has no Colombian calendar

**Cost: none for the benchmark. Real for accrual on live TES.**

`QuantLib.Colombia` does not exist in 1.43, and neither does a Bogota variant;
`ql.NullCalendar()` is what
`tests/benchmark/test_nss_quantlib_benchmark.py` uses, with `ql.Unadjusted`
rolls.

For the benchmark this is the *correct* choice rather than a workaround. The
comparison runs on `ACT/365` year fractions measured straight from the snapshot
date, exactly as `tests/unit/test_calibration.py` computes them; a business-day
adjustment would move a maturity off that shared grid and turn a curve test into
a date-arithmetic test.

It becomes a real limitation the moment QuantLib is used as a referee for
*accrual and settlement* on live TES, where the Colombian holiday calendar -
including the Ley Emiliani Monday shift - moves coupon dates. The project's own
calendar lives behind `tes_pricer.math.day_count` (the `holidays` dependency);
a QuantLib-side comparison there needs a hand-built `ql.BespokeCalendar` seeded
from the same holiday set, not `NullCalendar`.

---

## 3. The calibration objective is yield-space, and it is biased on coupon bonds

**Cost: 68bp to 1338bp of spot rate, measured. This is the largest known
limitation in the numerical core.**

`tes_pricer.math.calibration.calibrate_nss` minimises **yield** residuals,
`z(tau_i) - y_i`. `QuantLib.FittedBondDiscountCurve` minimises **price**
residuals. On a coupon-paying bond these have different minimisers, because a
bond's yield to maturity is not the zero rate at its maturity - it is a
cash-flow-weighted blend of the whole curve up to that point.

Measured on the ten benchmark TES maturities, coupons 5.50%-7.50%, priced
exactly off a known NSS curve
(`test_coupon_bonds_expose_the_yield_space_objective_gap`):

| | result |
|---|---|
| QuantLib, price-space fit | recovers the true parameters, cost `1.2e-27` |
| ours, yield-space fit | in-sample RMSE **63.1bp**, curve **68bp** off at 15y and **1338bp** off at 0.5y |

Two distinct errors stack inside that number:

1. **The coupon blend.** The dominant term, and it grows as the coupon pulls the
   effective maturity away from `tau`. It is worst at the short end, where the
   fitted curve has the least data to anchor it.
2. **The compounding basis.** `nss_yield` returns a *continuously* compounded
   rate; `bond_pricing` quotes `ytm` as a nominal annual rate compounded
   `frequency` times a year, which at `frequency == 1` is the Colombian street
   effective annual rate. Feeding an effective-annual yield straight into a
   continuous-basis residual costs `y - ln(1 + y)`: **38.2bp at a 9% level**.
   The conversion is `z = ln(1 + y)`, and `calibrate_nss` does not apply it.

Neither is a defect in `tes_pricer.math.nss_model`. The curve mathematics is
proved identical to QuantLib's to `6e-12` bp by
`test_nss_matches_quantlib_svensson_curve`, on the one universe where the two
objectives coincide - zero-coupon bonds, whose single cash flow makes
`price = 100 exp(-z tau)` and `ytm = z(tau)` true at the same time.

**To lift it:** the `.. todo::` already written into the `calibration` module
docstring - fit price residuals weighted by inverse modified duration, which
needs `bond_pricing.generate_cashflow_schedule` and
`bond_pricing.price_from_discount_factors` (both Phase 4 stubs). When that
lands, `test_coupon_bonds_expose_the_yield_space_objective_gap` is designed to
**fail**, and the fix is to promote the coupon universe into the strict
benchmark rather than to delete the test.

---

## 4. The objective is not convex, so parameters are weaker evidence than curves

**Cost: bounded below 1bp on exact data. Budgeted at 5bp on real quotes.**

Two correct implementations can settle in different basins and draw the same
curve. Measured on the same exact zero-coupon universe, with QuantLib's default
Simplex against QuantLib's own Levenberg-Marquardt
(`test_two_optimisers_find_different_parameters_but_the_same_curve`):

| | `lambda1` | `lambda2` | worst spot gap |
|---|---|---|---|
| Simplex | 2.0000 | 8.0000 | - |
| Levenberg-Marquardt | 1.9569 | **171.89** | **0.107bp** |

A 21-fold disagreement on `lambda2` that is worth a tenth of a basis point on
the curve. This is why the acceptance criterion in the benchmark is written on
**spot rates**, not on raw parameters; the parameter assertions only apply where
the fit is exactly identified.

The two tolerances that follow from it, both named constants in the benchmark
module:

* `SYNTHETIC_SPOT_TOLERANCE = 1e-4` (**1bp**) on noiseless synthetic data, where
  every basin that fits is the same curve. The observed gap is `6e-12` bp, so
  the headroom is six orders of magnitude - a failure here is a real
  discrepancy, never optimiser noise.
* `REAL_DATA_SPOT_TOLERANCE = 5e-4` (**5bp**) once the comparison runs on real
  quotes, where the basins are merely close rather than identical.

**This relaxation is expected behaviour, not a bug** - and it is also not a
knob. If a comparison exceeds its tolerance the discrepancy is to be audited,
in this order: the decay convention (`kappa` versus `lambda`, see below), the
compounding basis, and the sign of each loading. Widening a tolerance to make a
test pass would discard exactly the signal the benchmark exists to produce.

---

## 5. QuantLib parameterises the Svensson decays as rates, not time constants

**Cost: 76bp if unconverted. Handled, and pinned by a test.**

`QuantLib.SvenssonFitting` carries `x[4]` and `x[5]` as decay **rates**
`kappa = 1 / lambda`; `tes_pricer.math.nss_model.NSSParams` carries decay
**time constants** `lambda` in years. Everything else regroups exactly:
QuantLib's

```text
z(t) = x0 + (x1 + x2) (1 - e^-kt)/(kt) - x2 e^-kt
          + x3 ((1 - e^-k1 t)/(k1 t) - e^-k1 t)
```

is our `beta0 + beta1 L1 + beta2 (L1 - e^-x1) + beta3 (L2 - e^-x2)`, and both
sides form `DF = exp(-z t)`, so the compounding basis agrees with no conversion.

Reading `x[4]` as a lambda moves the 15-year point by **76bp**.
`test_quantlib_parameterises_the_decay_as_kappa_not_lambda` pins this with no
optimiser and no bond data - it compares two closed-form curves - so the
convention cannot drift silently.

---

## 6. `pandas` 3.x and `numpy` 2.5 in the benchmark environment

The benchmark environment resolved to `pandas 3.0.5` / `numpy 2.5.3`, both
newer than the `^2.2` / `^2.1` floors in `pyproject.toml`. Nothing in the
benchmark path touches pandas, and the constraints are floors rather than pins,
so this is recorded only so a later `pandas` 3 migration finding is not mistaken
for a regression introduced here.
