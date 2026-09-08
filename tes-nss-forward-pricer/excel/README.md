# Excel front-end

`tes_toolkit.xlsm` is a **placeholder**, not yet a valid Excel workbook. It is
built in Phase 9 and is the only binary artefact tracked in this repository.

Build contract:

- The workbook must contain **no business logic**. Every number comes from an
  `xlwings` UDF exposed by `tes_pricer.interface.excel_bridge`.
- `xlwings addin install` must have been run once on the workstation.
- The `RunPython` calls must point at the project virtualenv, configured in the
  `xlwings.conf` sheet, so the workbook is portable across machines.
