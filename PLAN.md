# Workflow Simplification Plan

A living plan for simplifying the `geoglows_ecflow` workflow. Branched off the
`rapid-to-river-route` work (PR #27), so all references below assume the
river-route codebase, not the RAPID `main`.

## Goals

1. **Reduce complexity** — eliminate duplication, simplify functions, separate
   inputs from logic.
2. **Make implicit explicit** — name constants and configuration, split mixed
   functions, document non-obvious behavior.
3. **Improve maintainability** — make different configurations easy to run and
   add a unit-test safety net.

## Scope

- **In scope:** `geoglows_ecflow/resources/*.py`,
  `geoglows_ecflow/workflow/builders/builder.py`,
  `geoglows_ecflow/workflow/parts/*`, and the `.ecf` task scripts.
- **Mostly frozen:** `geoglows_ecflow/workflow/comfies/*` — vendored ECMWF
  framework code (Apache 2.0, ~4,700 lines). Left untouched except for the
  minimal Python-3.12+ compatibility fixes noted below (the suite could not be
  imported at all without them).

## Decisions

- **Tests:** unit tests on the pure functions, plus in-memory
  suite-definition smoke tests (these were originally deferred but pulled
  forward to guard the builder refactor).
- **HRES member:** high-resolution is always ensemble member **52**, kept as a
  single named constant `HRES_ENSEMBLE_MEMBER` rather than a configurable value.

---

## Phase 1 — Pure cleanup (no behavior change) — DONE

- [x] `builder.py`: fix `Task("dimmy")` typo.
- [x] `helper_functions.py`: fix `create_logger` so file logging attaches.
- [x] `builder.py`: remove duplicate imports and the duplicated `nodes` import.
- [x] `builder.py`: remove read-but-unused config vars.
- [x] `builder.py`: fix stale docstring ("GLOFAS suite" → GEOGloWS).
- [x] `generate_esri_table.py`: fix `int or str` type hint.
- [x] Standardize the `argparse(nargs=1)` + `args.x[0]` antipattern.

## Phase 2 — Test harness + CI — DONE

- [x] Add `pytest` (+ `pytest-cov`) as a dev dependency.
- [x] Create `tests/` with fixtures.
- [x] Unit tests for the pure functions (ensemble parsing, VPU listing, date
      parsing, state-init lookback, forecast preprocess, return-period /
      exceedance, init-flow time index, zarr round-trip).
- [x] GitHub Actions workflow (`.github/workflows/tests.yml`). Now builds a
      conda env from `environment.yml` (ecflow has no PyPI wheel) on a
      Python **3.12–3.13** matrix and runs the whole suite.

## Phase 3 — Centralize duplication — DONE

- [x] Shared zarr-writing helper (`resources/zarr_io.py`).
- [x] `helper_functions.load_forecast_run` loader.
- [x] `RETURN_PERIODS` defined once and used for the ladders.
- [x] Standardized `logging` setup (`configure_logging`).

## Phase 4 — Make implicit explicit — MOSTLY DONE

- [x] `HRES_ENSEMBLE_MEMBER = 52` — used in `netcdf_to_zarr.py` and the
      `range(1, HRES_ENSEMBLE_MEMBER + 1)` ensemble loop in `builder.py`.
- [ ] `nco_calc.ecf` `grep -v ..._52.nc` (×3) — **decision pending**: wire an
      ecflow `%HRES_MEMBER%` variable vs. leave `52` + a comment. Highest risk:
      wrong wiring silently changes which member is excluded from the mean.
- [x] `EMOS_BASE != "12"` gate (×3) → `is_00z_cycle()` helper.
- [x] Magic numbers named: thickness ladder (`THICKNESS_THRESHOLDS`),
      stream-order (`MIN_STREAM_ORDER`), 10-day window (`FORECAST_WINDOW_DAYS`),
      `MEM` values (`ENS_TASK_MEM_MB` / `ARCHIVE_QINIT_MEM_MB`).
- [x] Timer offsets (`hours=7`/`hours=9`/`"14:15"`) — **resolved by deletion**:
      they lived only in the broken `rd`/research-mode branch, which has been
      removed (see follow-ups). Nothing to extract; revisit if `rd` returns.
- [x] Consolidate `self.config.get(...)` reads in `builder.py` into one
      documented block.

## Follow-ups (later tasks)

- ecFlow-**server** tests (building the def against a live server / `--dry`);
  the in-memory structural tests are done, this is the heavier version.
- README refresh (carried over from PR #27 review).
- **comfies is incompatible with ecflow 5.17+.** Its node wrappers set
  `Variable.parent` (`ooflow.py:1925`), which ecflow 5.17 made a read-only
  built-in, so building any suite raises `AttributeError`. Worked around by
  pinning `ecflow<5.17` in `environment.yml`; the real fix is to rename
  comfies' parent-tracking attribute so it no longer collides.
- **Research (`rd`) mode removed.** `mode='rd'` was the only path with
  `follow_osuite=False` and crashed unconditionally at `barrier_hh.ymd`
  (`barrier_hh` is a `NominalTime`, which has no `ymd`) — broken since the
  original 2024-07-25 authoring, so never usable. Removed the `rd` choice, the
  `follow_osuite`/`in_production`/`in_test` flags, the non-`follow_osuite`
  branch (the `+7h`/`+9h`/`14:15` run timers), and the dead crash line. `prod`
  and `test` are the remaining modes. If research mode is wanted again, it
  should be reintroduced correctly (with the intended barrier-repeat wiring).

---

## Current status

**Branch `workflow-simplification`** (fork `JakeGimenes`), open as a PR against
`rapid-to-river-route`. Phases 1–3 complete; Phase 4 complete except the
`nco_calc.ecf` decision. The timer constants are resolved by deletion (the
`rd`/research mode that owned them has been removed). **33 pytest tests pass** —
the resources tests run anywhere; the suite-definition tests require `ecflow`
(conda-forge).

The vendored `comfies` framework got the minimum Python-3.12+ compatibility
fixes needed to import it at all (`imp` → `importlib`, `pkg_resources` →
`packaging`); everything else in `comfies/*` is unchanged.

**Remaining actionable work:** the `nco_calc.ecf` `HRES_MEMBER` decision and the
README refresh.
