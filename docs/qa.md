# COSMO-REA6 Pipeline — User Q&A

Frequently asked questions from users of this pipeline.
Technical questions specific to algorithms are answered here;
for methodology deep-dives see the linked companion documents.

---

## 1. What method is used for collecting a typical weather year?

### What does the pipeline do?

For each spatial cell `(i, j)` in the 824 × 848 COSMO-REA6 grid the
pipeline:

1. Computes the **annual cumulative GHI** (W·h/m²) for every year in the
   analysis period (e.g. 1995–2018 = 24 years, or 1995–2027 = 33 years).
2. Finds the P10 / P50 / P90 percentile of that cell's multi-year GHI
   distribution.
3. Selects the **actual calendar year** whose cumulative GHI is closest
   to that percentile target.
4. Copies the **full 8760-hour time series** for all variables from the
   selected year into the output file.

A different cell may map to a different representative year; the
`source_year(rlat, rlon)` variable in each output file records the origin
year per cell.

### How does it compare to the classic TMY method?

The classic **Typical Meteorological Year (TMY)** method, standardised in
ISO 15927-4 and described by Finkelstein & Schafer (1981), works
differently:

| Property | Classic TMY (Finkelstein-Schafer) | This pipeline (annual GHI rank) |
| --- | --- | --- |
| Selection unit | Individual calendar **months** | Whole **calendar years** |
| How selected | CDF comparison of hourly values vs long-term CDF | Argmin of annual cumulative GHI vs percentile target |
| Temporal consistency | ❌ Months stitched from different years | ✅ All 8760 h from the same physical year |
| Standard | ISO 15927-4, ASHRAE 169 | IEC 61724-1 annual rank principle |
| Variables used in ranking | Temperature, irradiance, humidity, wind | GHI only (primary energy driver) |
| Output length | 8760 h (stitched) | 8760 h (from one year; leap years truncated) |
| Intra-year correlation | ❌ Broken by month stitching | ✅ Preserved |
| Suitable for annual energy simulation | ✅ Yes | ✅ Yes |
| Suitable for hourly correlation analysis (e.g. GHI vs T) | ❌ No | ✅ Yes |

### Why is the annual rank approach better for this project?

The COSMO-REA6 dataset is used for **building energy and renewable energy
simulations** where intra-year correlations matter:

- A cloudy winter month has simultaneously low GHI *and* low outdoor
  temperature — important for heating load calculations.
- A hot dry summer has high GHI *and* high T — important for cooling load
  and PV yield.

In the classic TMY, January might come from year A and July from year B,
breaking this physical coupling.  In the annual-rank approach, all variables
for a given cell come from the same physical year, so every T–GHI–wind
correlation is preserved exactly as it occurred in nature.

### Pros and cons of each approach

#### Annual GHI rank (this pipeline)

| Pros | Cons |
| --- | --- |
| Temporal consistency — all variables from one year | N = 24–33; P10/P90 have ~3–4 years within range, some resolution uncertainty |
| Fast: `np.nanpercentile` over 33 scalars per cell, seconds total | Single ranking metric (GHI); years cold in GHI may not be cold in wind |
| Memory-efficient: metric_stack is ~100 MB for 33 years | Does not smooth out unusual intra-year sequences |
| Output directly usable in energy simulation software | |

#### Classic TMY (Finkelstein-Schafer)

| Pros | Cons |
| --- | --- |
| Multi-variable ranking (T, GHI, wind, humidity) | Months from different years → broken inter-variable correlations |
| Smoothed, "typical" feel month by month | 100× more computation (hourly CDF per cell × 12 months) |
| Widely accepted in standards (ISO 15927-4, ASHRAE 169) | Cannot represent extreme years (P10/P90) — only median |
| | ~200 GB RAM for 33 × 8760 × 824 × 848 hourly stacks |

See [percentile_methodology.md](percentile_methodology.md) for the full
mathematical description.

---

## 2. Is night masking required or helpful?

### What is night masking?

Night masking forces GHI, DHI, and DNI to exactly **0.0 W/m²** when the
solar zenith angle θ ≥ 90°, i.e. the sun is geometrically below the
horizon.

### Why is it needed at all?

COSMO-REA6's radiation parameterisation works at hourly resolution.  At the
exact sunset and sunrise hours, the model averages radiation over the full
hour — this can produce a small non-zero value for `SWDIRS_RAD` or
`SWDIFDS_RAD` even for an hour when the sun is technically below the horizon
for most of the timestep.  Night masking corrects these artefacts to exactly
zero.

### Could masking accidentally remove real diffuse radiation (twilight)?

Civil twilight (θ between 90°–96°) does carry a small amount of skylight
diffuse radiation.  For annual GHI totals this represents < 0.05% of the
annual total — negligible for any energy application.  No standard TMY or
energy-simulation workflow uses twilight diffuse in annual sums.

### "There is no fixed timing of night across Europe — how is this handled?"

Exactly right — and the code handles this correctly.  The solar zenith angle
`θ(t, i, j)` is computed per cell using the **geographic lat/lon** of each
of the 824 × 848 grid cells (2-D auxiliary coordinates written by cfgrib from
the COSMO-REA6 GRIB rotated-pole metadata).

So for every cell independently:

- Stockholm (59 °N) has solar noon at different times from Madrid (40 °N)
- A cell in northern Finland has 24-hour daylight in summer and 24-hour
  night in winter — the mask correctly assigns 0 only when θ ≥ 90° for
  *that specific cell at that specific hour*

The Spencer (1971) formula used in `compute_dni()` and the mask function in
`derived_attributes.py` both operate over the full 3-D `(time, rlat, rlon)`
array in a single vectorised NumPy/Dask pass.

### What if a cell shows high GHI during night hours?

That would indicate a data quality issue in DWD's COSMO-REA6 source files
for `SWDIRS_RAD` or `SWDIFDS_RAD` — not a code bug.  The night mask would
suppress it, but it warrants investigation of that year's source files.
The `_report_dni_outliers()` function in `test_cosmo_one_year.py` logs any cell
where DNI > 1400 W/m² (above the solar constant), which would catch the
most severe artefacts.

### Is night masking standard practice?

Yes.  pvlib, NREL SAM, SolarAnywhere, EnergyPlus TMY3 generation tools,
and all ASHRAE-compliant post-processing pipelines apply θ ≥ 90° masking
as the universal standard.

---

## 3. Why is `derived_attributes.py` in `common/` rather than `providers/cosmo_rea6/`?

The file covers **all three providers** — COSMO_REA6, MERRA2, and ERA5_LAND —
in a single registry.  Moving it into `cosmo_rea6/` would require MERRA-2
and ERA5-Land to import from a sibling provider's folder, creating an
inappropriate cross-provider dependency.  `common/` is the correct location
for code shared across providers.

The COSMO-REA6-specific transform operations (opening GRIBs, the Spencer
solar-position formula, the dask-chunked computation) live in
`providers/cosmo_rea6/transform.py`, which is where the physics is applied
at full gridded resolution.  `derived_attributes.py` provides the
**formula registry and provider-agnostic dispatch layer**; it is not a
duplicate of `transform.py`.

---

## 4. Why is the merge step separate from `test_cosmo_one_year.py`?

Annual merge (12 monthly NCs → 1 annual NC) takes ~10–30 minutes per year
and is only needed as input for `test_percentile.py`.  Keeping it separate
means:

- `test_cosmo_multi_year.py` can finish in ~30 min/year and free disk space
  incrementally.
- You can re-run merge independently if a single month is regenerated.
- Parallel-year runs are unaffected (each year manages its own monthly files
  independently).

Run merge after all monthly files are ready:

```bash
# Single year
python -m weather.common.merge \
    --input  /data/output/COSMO_REA6_2005_??_all_attrs.nc \
    --output /data/output/COSMO_REA6_2005_annual_all_attrs.nc

# All years (bash loop)
for year in $(seq 1995 2027); do
    python -m weather.common.merge \
        --input  "/data/output/COSMO_REA6_${year}_??_all_attrs.nc" \
        --output "/data/output/COSMO_REA6_${year}_annual_all_attrs.nc"
done
```

---

## 5. Which `.nc` file naming convention is used and why?

| File | Description |
| --- | --- |
| `COSMO_REA6_<YYYY>_<MM>_all_attrs.nc` | Monthly output from `test_cosmo_one_year.py` |
| `COSMO_REA6_<YYYY>_annual_all_attrs.nc` | Annual merged file from `weather.common.merge` |
| `COSMO_REA6_p10_representative.nc` | P10 representative year from `test_percentile.py` |
| `COSMO_REA6_p50_representative.nc` | P50 (median) representative year |
| `COSMO_REA6_p90_representative.nc` | P90 representative year |

The `all_attrs` suffix distinguishes these from any single-attribute intermediate
files that may be produced during debugging.

---

## 6. Why does the pipeline store all 9 attributes together in one file?

COSMO-REA6 provides 9 attributes across separate GRIB files on disk.  Merging
all attributes into one monthly NetCDF:

- Reduces the number of files from 108/year (12 months × 9 attrs) to 12/year.
- Allows energy simulation tools (EnergyPlus, TRNSYS, OpenStudio) to read
  one file per month rather than 9.
- Keeps all variables on the same time axis and spatial grid, ensuring
  consistency for any downstream computation.

---

## 7. Can I process a subset of years?

Yes.  All entry-point scripts support `--from-year` / `--to-year`:

```bash
# Process only 2010–2015
python src/weather/tests/test_cosmo_multi_year.py \
    --from-year 2010 --to-year 2015 --ncores 94

# Single year
python src/weather/tests/test_cosmo_one_year.py --year 2005 --ncores 94
```

For `test_percentile.py` the same flags apply, but note that percentile
accuracy degrades with fewer years.  A minimum of ~10 years is recommended.

---

## 8. What happens if the run is interrupted?

Use `--resume` to restart without reprocessing completed months:

```bash
python src/weather/tests/test_cosmo_one_year.py \
    --year 2018 --ncores 94 \
    --skip-download --skip-decompress --resume
```

- `--resume`: skips Phase 3 for months whose output `.nc` already exists.
- `--skip-download`: bypasses Phase 1 (bz2 files already cleaned up or still present).
- `--skip-decompress`: bypasses Phase 2 (`.grb` files still on disk).

See [parallelization.md §3.2](parallelization.md#32-interrupted-runs-skip-if-done-and---resume)
for the full resume logic.

---

## 9. How much disk space does a full run require?

The COSMO-REA6 dataset covers **1995–2019** (real production archive: 298
monthly files — DWD's actual coverage stops partway through 2019, not a
clean 24-year boundary).

**Updated 2026-08-14 with real measured numbers** (the previous version of
this table significantly understated the monthly/annual figures — it
predates several sessions' worth of added output variables, e.g.
`ALBEDO`/`T_DEW`/`SNOW_DEPTH`/kept `U_10M`/`V_10M`, which meaningfully grew
per-file size since the estimate was first written). Monthly and annual
sizes below are directly measured from the real archive on `sd26`
(average of 12 real 1995 monthly files; one full real 2018 annual merge).
The bz2/decompressed-GRIB figures are carried over from the original
estimate and have **not** been re-verified this session:

| Stage | Per month / year | Full archive (298 months / ~24.8 years) |
| --- | --- | --- |
| Downloaded bz2 (12 months × 9 attrs) | ~12 GB/year | ~288 GB (deleted after Phase 2; not re-verified) |
| Decompressed GRIB (12 months × 9 attrs) | ~84 GB/year peak | ~84 GB peak (deleted per month; not re-verified) |
| Monthly NetCDF output | ~9.3 GB/month | ~2.7 TB |
| Annual merged NetCDF | ~112 GB/year | ~2.7 TB (additive to monthly) |
| Representative files (3 × P10/P50/P90) | -- | ~14 GB (not re-verified) |

Final storage if every month **and** every year's annual merge are kept:
**~5.4+ TB**, not the ~230 GB this section previously claimed — the old
estimate's monthly/annual rows were roughly the size of a *single* month's
output, not a whole year's. Monthly files can be deleted after their
annual merge to recover the ~2.7 TB those files use, if only the annual
files are needed going forward. Peak disk usage during a single year's
processing (GRIBs + that year's monthly/annual output, before cleanup) is
larger than the old ~90 GB estimate too, given the corrected per-year
output size — budget accordingly rather than relying on that number
directly; it was not recomputed with the same rigor as the monthly/annual
figures above.

---

## 11. Why are `.grb` and `.idx` files not cleaned up after `--skip-decompress`?

### Symptom

After running:

```bash
python src/weather/tests/test_cosmo_one_year.py \
    --year 2005 --months 2 \
    --skip-download --skip-decompress --ncores 94
```

the decompressed `.grb` files remain on disk, and new `.idx` files have
appeared alongside them.

### Why the `.grb` files are not removed

Cleanup of decompressed GRIB files (CLEANUP B) is gated on the
`do_dc` (decompress) flag being `True`. Passing `--skip-decompress` sets
`do_dc = False`, which suppresses CLEANUP B for the entire run —
regardless of whether `--resume` is also passed.

`--resume` only controls whether the **transform** step (Phase 3) is
skipped for months whose output `.nc` already exists. It has no effect on
cleanup flags.

### Why `.idx` files appear

cfgrib writes an ECCODES index cache file (`.grb.idx` or `.idx`) next to
every GRIB file on first open. These are harmless binary index files used
to accelerate subsequent cfgrib reads of the same file. They can be left
in place or deleted manually:

```bash
find /data/soma/cosmo_rea6/decompress -name '*.idx' -delete
```

### How to clean up `.grb` files manually after a skip-decompress run

Once the monthly `.nc` has been written successfully, the source `.grb`
files for that month are no longer needed:

```bash
# Example: remove 2005-02 GRIB files
for attr in PS SWDIFDS_RAD SWDIRS_RAD T_2M U_10M V_10M H_SNOW SNOW_GSP SNOW_CON; do
    rm -f /data/soma/cosmo_rea6/decompress/${attr}/${attr}.2D.200502.grb
    rm -f /data/soma/cosmo_rea6/decompress/${attr}/${attr}.2D.200502.grb.idx
done
```

---

## 10. Why NetCDF-4 (`.nc`) and not Zarr?

### What is Zarr?

Zarr is a cloud-native chunked array format designed for object storage
(S3, Azure Blob, GCS).  It stores chunks as individual files or objects,
enabling highly parallel reads from distributed storage.

### Why does this pipeline use NetCDF-4?

| Criterion | NetCDF-4 (`.nc`) | Zarr |
| --- | --- | --- |
| Single-file portability | ✅ One `.nc` per month | ❌ Directory tree or object prefix |
| Interoperability | ✅ EnergyPlus, TRNSYS, MATLAB, R, ArcGIS, CDO, NCO | ⚠️ Python/Dask-centric; limited native support in BEM tools |
| HPC GPFS performance | ✅ dask-chunked I/O via xarray; no small-file overhead | ❌ Many small chunk files → metadata storms on GPFS |
| DWD source format alignment | ✅ CF-1.x metadata preserved natively | ⚠️ Requires explicit attribute mapping |
| Compression | ✅ zlib per-variable (complevel=1, float32) | ✅ Any codec (Blosc, Zstd …) |
| Cloud-native parallel reads | ⚠️ Sequential time-slice reads preferred | ✅ Designed for parallel chunk reads from object storage |
| BuEM integration (see below) | ✅ Direct | ⚠️ Requires zarr→NetCDF conversion or zarr-aware loader |

### Why does it matter for BuEM solar-gains calculations?

The BuEM module reads per-cell hourly radiation (GHI, DHI, DNI) from the
representative-year files to calculate **solar gains through building
envelopes** (ISO 13790 / EN ISO 52016 methodology).

Key requirements that favour NetCDF-4:

- **Sequential time access**: BuEM iterates hour-by-hour through the year
  for each building cell.  NetCDF-4 with contiguous or chunked-by-time HDF5
  layout gives efficient sequential reads — exactly the access pattern used
  by `xr.open_dataset(..., chunks={"time": 168})`.
- **Single-file handoff**: BuEM expects one file per representative year
  (P10/P50/P90).  NetCDF-4 delivers this directly; Zarr would require
  packaging the chunk directory as a zip store first.
- **CF conventions**: GHI/DHI/DNI variables carry CF-1.x `units`,
  `long_name`, and `grid_mapping` attributes automatically preserved by
  xarray's NetCDF backend.  BuEM can read these metadata without any
  translation layer.
- **Toolchain compatibility**: Post-processing tools used alongside BuEM
  (CDO for regridding, NCO for variable extraction, Paraview for
  visualisation) all have native NetCDF support; Zarr support is absent or
  experimental in most of these.

### When would Zarr be a better choice?

Zarr becomes preferable if the workflow moves to **cloud object storage**
(e.g. AWS S3, Azure Blob) where many concurrent readers each access
different spatial chunks.  In that scenario, xarray can read Zarr stores
directly and the GPFS small-file overhead disappears.  For the current
HPC-local workflow, NetCDF-4 is the better fit.
