# `weather geo crop` / CDO grid-metadata bug — investigation and fix

`weather geo crop` (`src/weather/geo/crop.py`, a thin wrapper around `cdo
sellonlatbox`) was silently broken against every provider's real exported
output, for as long as the tool has existed. This document is the
investigation record: how it was found, why it happened, the fix, and how
the already-existing archives were repaired.

---

## Symptom

`cdo sellonlatbox` aborted outright against real COSMO-REA6 and MERRA-2
output files:

```text
cdi    warning (set_coordinates_varids): Coordinates variable latitude can't be assigned!
cdi    warning (set_coordinates_varids): Coordinates variable longitude can't be assigned!
cdo    sellonlatbox (Warning): Unsupported grid type: generic
cdo    sellonlatbox (Abort): No processable variable found!
```

This was not a risk someone flagged in advance — the module's own
docstring previously asserted ERA5-Land/MERRA-2 "carry a regular,
CF-compliant lat/lon grid" and only COSMO was thought to be blocked
(by a *different*, already-fixed problem: COSMO's export not retaining
lat/lon coordinates at all). That assertion had never actually been
tested against a real `cdo` run. It turned out to be wrong for all three
providers.

## Root cause

Every provider's `transform.py` attaches `latitude`/`longitude` via
`xarray.Dataset.assign_coords(...)`:

```python
ds = ds.assign_coords(
    latitude=("y", lat_vals),   # or (("y", "x"), lat_2d) for COSMO's 2-D case
    longitude=("x", lon_vals),
)
```

`assign_coords` builds a **fresh** coordinate variable with no attributes.
Whatever `standard_name`/`units` metadata the original source coordinate
may have carried (ERA5-Land/MERRA-2 rename their original `latitude`/
`longitude` *dimensions* to `y`/`x` first, for cross-provider parity, then
re-stash the values as plain coordinates on the new dims — see
`era5_land/transform.py` and `merra2/transform.py`) is discarded in the
process. The result: every exported file's `latitude`/`longitude`
variables carried only `_FillValue`, nothing else.

This is invisible to xarray and `weather.point_query` — both match
coordinates by variable name, not by CF role, so `get_point_weather()`
and every internal tool kept working fine. It is **not** invisible to
CDO, which requires `standard_name`/`units` (or an equivalent recognised
alias) to identify a variable as a latitude/longitude coordinate at all.
Without it, CDO's grid-type detection falls back to `gridtype = generic`
— a bare index grid with no geographic meaning whatsoever — and
`sellonlatbox`, which only makes sense against real geographic
coordinates, has nothing to work with.

One thing that was *not* broken: the `coordinates` attribute linking each
data variable to its lat/lon (e.g. `T.attrs["coordinates"] = "latitude
longitude"`) was already present and correct in every provider's export.
Only the coordinate variables' own identifying attributes were missing.

## Fix

One shared helper, `common/cf_conventions.attach_cf_latlon_attrs(ds)`,
called from all three providers' `transform.py` immediately after their
respective `assign_coords(latitude=..., longitude=...)` call:

```python
ds["latitude"].attrs = {
    "standard_name": "latitude", "long_name": "latitude",
    "units": "degrees_north",
}
ds["longitude"].attrs = {
    "standard_name": "longitude", "long_name": "longitude",
    "units": "degrees_east",
}
```

Attribute assignment doesn't depend on dimensionality, so the same
function covers COSMO's 2-D auxiliary coordinates and ERA5-Land/MERRA-2's
1-D coordinates identically — no per-provider branching needed.

New exports get this automatically. Already-exported files needed a
separate, retroactive pass (below).

## Verification — before and after, against real files

Confirmed empirically at every step, not assumed:

1. **`cdo griddes` (metadata-only, no data read) before the fix**, against
   a real COSMO annual file: `gridtype = generic`, with the exact
   "Coordinates variable ... can't be assigned!" warnings above.
2. **`cdo sellonlatbox` before the fix**: hard abort, both against COSMO
   (2-D case) and MERRA-2 (1-D case) — confirming the bug affected the
   regular-grid providers too, not just COSMO's curvilinear grid.
3. **Patched a copy's attributes in place** (metadata only, via
   `netCDF4`'s `r+` mode — verified this does not touch any data array,
   only variable attributes) and re-ran both checks:
   - COSMO: `gridtype = curvilinear`, `sellonlatbox` produced a correctly
     bounded Netherlands crop (56×50 cells of the 824×848 domain, lat/lon
     extent matching the independently-computed correct answer from
     `providers/cosmo_rea6/crop.py`'s index-based approach).
   - MERRA-2: `gridtype = lonlat`, `sellonlatbox` produced a correctly
     bounded crop (5×6 cells of the 71×77 domain).
4. **Final confirmation against real, unmodified archive files** (not
   copies) after the retroactive patch below: `cdo sellonlatbox` against
   a real COSMO file (2005-06) and a real MERRA-2 file (2010-06), both
   producing correct output.

## Retroactive patch of existing archives

The fix only applies to files exported *after* it landed. Making
`weather geo crop` work against everything already on disk needed a
separate, one-time pass — metadata-only (the same `netCDF4` `r+`
attribute patch verified above), so no data was recomputed or rewritten,
just the two coordinate variables' attributes on each file.

Idempotent (skips any file that already has `standard_name` set, so
safe to re-run) and per-file try/except (one bad file can't abort the
whole run) — both properties mattered in practice: a real, unrelated
stale-lock conflict (an orphaned dask worker process, leftover from an
earlier same-session smoke test that was never torn down, holding a
read lock on one COSMO file) caused exactly one failure on the first
pass; per-file isolation meant the other 283 files patched cleanly
regardless, and the one conflicting file succeeded on retry once the
stale process was found and killed.

Results:

| Provider   | Files | Patched | Skipped (already done) | Failed |
| ---------- | ----: | ------: | ---------------------: | -----: |
| COSMO-REA6 |   298 |     284 |                     14 |      0 |
| MERRA-2    |   552 |     540 |                     12 |      0 |

ERA5-Land's existing archive has **not** yet been patched (its bulk
pipeline run was active in a separate session at the time this fix
landed — intentionally left untouched). New ERA5-Land exports already
get the fix automatically; the existing archive needs the same
retroactive pass once that run finishes.

## Scope note: `weather fetch --country`/`--bbox` vs. `weather geo crop`

This fix is unrelated to (and does not replace) COSMO's early-crop
feature (`providers/cosmo_rea6/crop.py`, wired into `weather fetch
--country`/`--bbox` for *fresh* pipeline runs, applied right after
decompress). That mechanism doesn't use CDO at all — it computes a
`(y, x)` index window directly from cfgrib-decoded real lat/lon values
and applies `.isel()`, entirely in Python/xarray. It remains the right
tool for a fresh, country-scoped COSMO fetch (and the only one that
works without CDO installed at all — CDO has no win-64 conda-forge
build, so it can't be tested on Windows dev, and was not even installed
on `sd26`'s conda environment until this investigation started, despite
being listed in `weather_env.yml`). `weather geo crop` is for cropping a
file that already exists, of any provider, without re-running the
pipeline — that's the tool this fix restores to working order.
