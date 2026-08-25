# `weather fetch` — unified CLI guide

`weather fetch` is one command that covers what previously took three separate,
manual steps across scattered per-provider scripts: downloading a date range,
concatenating the resulting monthly files, and computing percentile
representative-year mosaics. It wraps existing, unmodified machinery — each
provider's own `run_pipeline()`, `weather.common.merge.NetCDFMerger`, each
provider's `*PercentileIndexer`, and `weather.geo` — behind one flag set that's
the same across all three providers (`cosmo-rea6`, `era5-land`, `merra-2`).

It does **not** replace anything: `test_<provider>_{one_month,one_year,multi_year}.py`,
the production bulk-run shell scripts (`scripts/run_era5_bulk.sh`,
`scripts/run_merra2_bulk.sh`), and `weather run` are all untouched and still work
exactly as before. `weather fetch` is an additive convenience layer for
interactive/ad-hoc use — for a scheduled, unattended, whole-archive production
run, prefer the bulk-run scripts documented in `docs/BULK_RUN_GUIDE_ERA5-LAND.md`
/ `docs/BULK_RUN_GUIDE_MERRA2.md`, which have their own resume/QA/logging
conventions this command does not attempt to duplicate.

--------------------------------------------------------------------

## Quick examples

```bash
# One month, one provider (default: cosmo-rea6, no concatenation)
weather fetch --range single-month --year 2018 --month 3

# One year of MERRA-2, cropped to the Netherlands, concatenated into
# a single annual file with an NL-tagged filename
weather fetch --provider merra-2 --range single-year --year 2023 \
    --country netherlands --concatenate all

# A multi-year ERA5-Land range, one concatenated file PER YEAR
weather fetch --provider era5-land --range multi-year \
    --from-year 2015 --to-year 2020 --concatenate per-year

# A full COSMO-REA6 year, then compute P10/P50/P90 percentile mosaics
# from the provider's whole historical archive
weather fetch --provider cosmo-rea6 --range single-year --year 2018 \
    --percentile --which-percentile p50
```

--------------------------------------------------------------------

## How dispatch works

`weather fetch` calls each provider's raw `run_pipeline(year, months=None, ...)`
function directly — **not** the `registry`-returned provider facades
(`CosmoREA6Provider`/`ERA5LandProvider`/`MERRA2Provider`). Every facade ends with
`return outputs[0] if outputs else Path()`, discarding every month but the
first; `--concatenate` needs the full `list[Path]` of monthly outputs, so the
facades are structurally unusable here. `registry.get_provider()` is still used
for `--provider` name/alias resolution and its existing error message — just not
for the pipeline call itself.

--------------------------------------------------------------------

## Flag reference

### `--provider NAME`

Weather provider. Available: `cosmo-rea6`, `era5-land`, `merra-2` (aliases:
`cosmo`, `era5`, `merra2`, resolved via the same `registry.get_provider()` every
other subcommand uses). Defaults to the `WEATHER_PROVIDER` env var, or
`cosmo-rea6` if that's unset.

### `--range {single-month,single-year,multi-year}` (required)

Selects which of `--year`/`--month`/`--from-year`/`--to-year` apply:

| `--range`      | Required flags      | Optional flags (fall back to provider config)                                     |
|----------------|---------------------|-----------------------------------------------------------------------------------|
| `single-month` | `--year`, `--month` | —                                                                                 |
| `single-year`  | —                   | `--year` (defaults to `EnvSettings.<p>_year()`)                                   |
| `multi-year`   | —                   | `--from-year`/`--to-year` (default to `EnvSettings.<p>_from_year()`/`_to_year()`) |

`--concatenate` is rejected outright for `single-month` — there's only one month,
nothing to merge.

### `--year YEAR` / `--month M` / `--from-year YEAR` / `--to-year YEAR`

Same meaning as the equivalent flags on `test_<provider>_{one_month,one_year,
multi_year}.py`. `--month` accepts `1`-`12`.

### `--ncores N`

Worker count, passed straight through to `run_pipeline(ncores=...)`. Defaults to
`None`, which lets each provider's own `run_pipeline()`/`EnvSettings` resolve its
usual default (94 for COSMO's CPU-bound decompress+transform, 6 for ERA5-Land's
I/O-bound CDS downloads, 8 for MERRA-2's I/O-bound OPeNDAP downloads — see the
"Providers at a glance" table in the repo's `CLAUDE.md`).

### `--work-dir DIR`

Overrides the provider's work directory (where `download/`, `decompress/`, and
`output/` live) for this invocation only, same mechanism `run_pipeline()`'s own
`work_dir` parameter already uses.

### `--complevel N`

NetCDF zlib compression level, 1-9. Default `1`, matching every provider's own
`export.py` default.

### `--skip-download`

Skip the download phase, reusing already-downloaded files under the work
directory. Shared flag from `common/cli_flags.add_skip_download_flag`.

### `--skip-decompress`

**cosmo-rea6 only.** Skip the bz2-decompress phase, reusing already-decompressed
GRIBs. Rejected with an error for `era5-land`/`merra-2` (they have no separate
decompress phase — CDS/OPeNDAP already hand back usable NetCDF).

### `--skip-dni`

**cosmo-rea6 only.** Skip the experimental per-cell DNI computation. Rejected
for the other two providers.

### `--night-mask`

**era5-land only.** Enables Spencer solar-zenith-angle night-masking of GHI
(positive polarity — this is `run_pipeline(night_mask=True)`; the default is
`False`, matching `test_era5_one_month.py`'s own flag). Rejected for the other
two providers.

### `--resume`

Skip periods whose output already exists. Shared flag from
`common/cli_flags.add_resume_flag`. See "Resume + region tags" below for the
one caveat specific to `--country`/`--bbox` runs.

### `--cleanup`

Delete downloaded/decompressed intermediates after a successful export. Default
`None` (not `False`) — when omitted, each provider's own `*_CLEANUP` env setting
(`COSMO_CLEANUP`/`ERA5_CLEANUP`/`MERRA_CLEANUP`) decides, exactly as it does for
every other entrypoint in this repo (see `CLAUDE.md`'s "one centralized default
per cross-cutting knob" convention). Passing `--cleanup` forces cleanup on for
this run regardless of the env setting.

### `--concatenate {none,per-year,all}`

- `none` (default) — no merging; the monthly files are left as-is.
- `per-year` — one merged file per calendar year fetched.
- `all` — one merged file spanning the entire requested range.

For `--range single-year`, `per-year` and `all` produce the same result (only
one year exists either way) — this is documented behavior, not a bug, and both
are accepted interchangeably in that case. Not valid for `--range single-month`.

Concatenation reuses `weather.common.merge.NetCDFMerger.merge()` unmodified —
the same `xarray`/`dask`-based merge used by `merge.py`'s own CLI and
`infrastructure/container/entrypoint.sh`'s `merge` pipeline mode. See that
module's docstring for the merge algorithm itself (dimension-scale-correct,
per-file CF time-units-aware, dask-chunked for bounded memory).

**Output filenames:**

| Scope                             | Filename pattern                                                                                                 |
|-----------------------------------|------------------------------------------------------------------------------------------------------------------|
| Per-year (or single-year)         | `<PREFIX>_<YYYY>_annual_all_attrs.nc`                                                                            |
| Multi-year `--concatenate all`    | `<PREFIX>_<from_year>_<to_year>_all_attrs.nc`                                                                    |
| Either, with `--country`/`--bbox` | `<PREFIX>_<TAG>_...` -- the region tag is inserted right after the prefix (see "Country and bbox scoping" below) |

`<PREFIX>` is `COSMO_REA6`, `ERA5_LAND`, or `MERRA2`.

### `--output PATH`

Overrides the destination path of the concatenated file. Only valid when
`--concatenate` produces exactly **one** file: `single-year` (either mode) or
`multi-year --concatenate all`. Rejected with an error for
`multi-year --concatenate per-year`, since that produces multiple files and
there's no single path to redirect them to.

### `--percentile`

After fetching, runs the provider's `*PercentileIndexer`
(`CosmoRea6PercentileIndexer` / era5-land's equivalent / `Merra2PercentileIndexer`)
against the provider's **entire** historical `output/` folder — not just the
files this invocation fetched — writing P10/P50/P90 representative-year mosaics
to `<output_dir>/percentile/`. This is the same indexer every provider's own
`percentile_index.py --help` documents; `weather fetch --percentile` is a
convenience wrapper, not a different algorithm.

### `--which-percentile {p10,p50,p90} [...]`

Filters what the final summary line *reports* as relevant. The indexer always
computes and writes all three percentiles in one fused pass — there's no way to
compute only a subset without changing the shared indexer itself — so this flag
never skips or deletes output; it only narrows the CLI's own printed summary.
Default: report all three.

### `--country NAME` / `--bbox N,W,S,E`

Available for all three providers, via two different mechanisms depending on
whether the provider has a server-side area-subsetting endpoint at all (see
"Two different mechanisms" below).

- `--country NAME` looks up a bounding box from `weather.geo.countries.COUNTRIES`
  (run `weather geo list` for the full set — currently ~30 European countries).
  The output filename gets that country's ISO 3166-1 alpha-2 code inserted right
  after the provider prefix, e.g. `MERRA2_NL_2023_01_all_attrs.nc`.
- `--bbox "N,W,S,E"` crops to an arbitrary custom box (degrees, comma-separated,
  parsed by `BBox.parse()`). Since no short code exists for an arbitrary box,
  the filename gets a `CUSTOM-<hash>` tag instead (a short deterministic hash
  of the bbox values, e.g. `ERA5_LAND_CUSTOM-3afb6f_2018_03_all_attrs.nc`) —
  not the bare literal `CUSTOM`, so two different `--bbox` values for the same
  year/month never collide on the same filename.

#### Two different mechanisms

- **era5-land / merra-2**: a **real, pre-download, server-side** area
  restriction, not a post-hoc crop. Works by setting `ERA5_AREA`/`MERRA2_AREA`
  in the process environment for the duration of the fetch (the same env var
  each provider's `get_config()` already reads on every call, never cached at
  import — the same mechanism `run_pipeline()`'s own `work_dir` override
  already relies on), so CDS and OPeNDAP genuinely only transfer the cropped
  region. Download itself is smaller, not just the output file.
- **cosmo-rea6**: DWD has no server-side area-subsetting endpoint at all —
  download and decompress always fetch/expand the whole fixed domain, exactly
  as without `--country`/`--bbox`. The crop instead happens **locally, right
  after decompress, before transform** (`providers/cosmo_rea6/crop.py`):
  every attribute is cropped to the bbox's `(y, x)` index window before any
  derived-field formula (GHI, DHI, DNI, solar position, ...) runs, so the
  expensive part of the pipeline only processes the requested area. Real
  end-to-end measurement (Netherlands, 2018-01): the crop narrows the domain
  to about 0.4% of its cell count (56×50 of 824×848 cells). The index window
  is computed once (any single opened attribute has the same fixed grid) and
  reused for every other attribute in that month.

  cfgrib decodes genuine per-cell WGS84 `latitude`/`longitude` as 2-D
  auxiliary coordinates the moment it opens a **decompressed** GRIB (this
  requires the bz2 wrapper to already be gone — eccodes has no
  transparent-bz2 read path, so the earliest a crop can structurally happen
  is right after decompress, never right after download).

  Because COSMO-REA6's native grid is rotated relative to true lat/lon, the
  cropped region's own lat/lon *extent* comes out somewhat larger than the
  requested bbox (an axis-aligned index rectangle over a rotated grid can't
  align exactly with an axis-aligned WGS84 box — real measurement saw up to
  ~0.5° of extra margin at the edges). This is expected: the crop always
  contains at least the requested area, never less, with some surrounding
  margin. See `crop.py`'s module docstring for the full explanation.

It does not touch or call `weather geo crop` (the standalone post-hoc
NetCDF-cropping tool — see below) at all, for either mechanism.

#### Resume + region tags

`run_pipeline()`'s own `--resume` only checks the **untagged** output path,
which no longer exists once a fetch has already renamed it with a region tag.
To avoid silently redoing already-finished country-scoped work on every re-run,
`weather fetch --resume` (with `--country`/`--bbox` active) checks for the
**tagged** filenames itself before calling `run_pipeline()` at all, and skips
the call entirely if every expected tagged file for that year already exists.

This check is **all-or-nothing per year**: if only *some* of the requested
months are already tagged-done, the whole year re-runs (needlessly redoing the
finished months too, since their untagged path was renamed away and
`run_pipeline()`'s own per-month resume can't see the tagged name either). Full
per-month resume awareness for tagged runs would need `run_pipeline()` to accept
an explicit remaining-months subset — out of scope for now. The common case (a
tagged year is either fully done or not started) is handled correctly.

#### Region-scoped download caching (era5-land / merra-2)

Fixed 2026-08-15: a real, confirmed silent-wrong-data bug, found while
investigating a report that MERRA-2's downloaded raw files "have the same
names irrespective of the region selected." Before the fix, the raw
download-phase filename (`download_dir/ERA5_LAND_<YYYY>_<MM>_all_attrs.grib`,
`download_dir/MERRA2_<collection>_<YYYYMMDD>.nc4`) carried no area/region
identifier at all, while its *content* was server-side cropped to whatever
`ERA5_AREA`/`MERRA2_AREA` was set to — so a `netherlands` fetch followed by a
`germany` fetch for the same year/month against the same `work_dir` silently
reused the Netherlands-cropped raw file for the Germany request (the
completeness check is existence-only for both providers, since neither's
remote source can report a size before processing), producing an output file
labeled Germany but actually populated with Netherlands data. No error,
warning, or overwrite — the second fetch simply skipped downloading anything.

Two mechanisms now prevent this, both active for every `--country`/`--bbox`
fetch:

1. **Region-tagged download filenames** — `local_path()` in both providers'
   `downloader.py` inserts the region tag right after the provider prefix
   (e.g. `ERA5_LAND_NL_2018_01_all_attrs.grib`), the exact same convention
   already used for output files. Two different regions for the same
   year/month now resolve to disjoint download paths, so they simply can't
   collide. Untagged (no `--country`/`--bbox`) fetches keep byte-identical
   filenames to before this feature existed.
2. **Content-key sidecar** (`base_downloader.py`'s `content_key()` hook) — a
   defense-in-depth backstop for callers that bypass `weather fetch`
   entirely, e.g. the documented `export ERA5_AREA=...` bulk-run workflow
   (`docs/BULK_RUN_GUIDE_ERA5-LAND.md`). Each download writes a small
   `<file>.area` sidecar recording the area actually used; a later request
   against the same path with a *different* area is detected as a mismatch
   and forces a real re-fetch instead of reusing stale content. A **missing**
   sidecar is always trusted (existing behavior, unchanged) — this is what
   makes the fix zero-migration-risk for every already-completed real
   archive (no sidecars exist yet, so nothing changes for them until a
   second, differently-scoped run actually happens against the same path).
   COSMO-REA6 never overrides `content_key()` (DWD always serves the whole
   domain regardless of any crop request, so this class of bug can't occur
   for it) — a hard no-op, verified in `tests/test_base_downloader.py`.

--------------------------------------------------------------------

## Relationship to `weather geo crop`

`weather geo crop` (see `README.md`'s "Country cropping" section) is a
**separate, standalone, post-hoc** tool: it crops an *already-exported* NetCDF
file via `cdo sellonlatbox`, run manually after the fact, against any output
file regardless of how it was produced. `weather fetch --country`/`--bbox` is
the opposite: it crops (or, for era5-land/merra-2, server-side restricts)
*during* the fetch itself, so the file that lands on disk was never
full-domain to begin with. The two remain independently useful, not
redundant: `weather geo crop` is still the only option for archives that were
already fetched full-domain before this flag existed (any provider, including
already-completed COSMO-REA6 archives), and it works standalone without
re-running any part of the pipeline.

--------------------------------------------------------------------

## Full flag/provider compatibility matrix

| Flag                 | cosmo-rea6 | era5-land | merra-2 |
|----------------------|------------|-----------|---------|
| `--skip-decompress`  | Yes        | No        | No      |
| `--skip-dni`         | Yes        | No        | No      |
| `--night-mask`       | No         | Yes       | No      |
| `--country`/`--bbox` | Yes*       | Yes       | Yes     |

\* cosmo-rea6: local post-decompress crop, not server-side subsetting —
download/decompress still fetch the whole domain either way. See "Two
different mechanisms" above.

All other flags (`--ncores`, `--work-dir`, `--complevel`, `--skip-download`,
`--resume`, `--cleanup`, `--concatenate`, `--output`, `--percentile`,
`--which-percentile`) apply to all three providers identically. Passing a
provider-restricted flag to an unsupported provider is a validated error
(`weather fetch` exits with a message naming the flag and the provider), not a
silent no-op.

--------------------------------------------------------------------

## See also

- `README.md` — brief overview and the rest of the CLI surface
  (`info`/`validate`/`run`/`geo`/`serve`).
- `docs/BULK_RUN_GUIDE_ERA5-LAND.md` / `docs/BULK_RUN_GUIDE_MERRA2.md` —
  production, unattended, whole-archive run guidance (`weather fetch` is not a
  substitute for these).
- `src/weather/common/merge.py` — the concatenation implementation
  `--concatenate` calls.
- `docs/WEATHER_GEO_GUIDE.md` — the standalone `weather geo crop` tool's own
  full guide (flags, provider support, requires `cdo`).
- `src/weather/geo/` — the country/bbox lookup and standalone `weather geo crop`
  tool.
