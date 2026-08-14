# Country/region-scoped archives

EnerPlanET currently operates in four countries — Austria, Netherlands,
Germany, Czech Republic — not the whole of Europe. Downloading,
processing, and storing a whole-continent archive per provider per year
when only a fraction of it is ever queried is real, avoidable cost. This
page covers how to scope a run to a country set, what it actually saves,
and the two gotchas that make this easy to get subtly wrong.

!!! info "Two different mechanisms — do not confuse them"
    - **Server-side request scoping** (`ERA5_AREA` / `MERRA2_AREA`,
      this page's main subject): the *provider's own server* only ever
      sends back the requested region. Saves download bandwidth,
      transform time, AND storage.
    - **Post-hoc local cropping** (`weather geo crop`, see
      `src/weather/geo/crop.py`): downloads and processes the full
      configured area first, then crops the *result*. Saves storage
      only — not bandwidth or processing time. Useful for slicing an
      already-built archive into per-country files after the fact;
      not a substitute for scoping the request itself.

## WEATHER_REGION: automatic scoping for a single country

For the common case of scoping a run to **one** country, set
`WEATHER_REGION` (e.g. `WEATHER_REGION=germany`, any name from `weather
geo list`) instead of hand-computing `ERA5_AREA`/`MERRA2_AREA` and a
matching `WORK_DIR`. It derives both automatically:

- `ERA5_AREA`/`MERRA2_AREA` default to the region's bbox (via
  `weather.geo.countries.get_bbox`) whenever they aren't set explicitly.
- `EnvSettings.era5_work_dir()`/`merra2_work_dir()` default to
  `<provider>/<region>/` instead of the flat `<provider>/` root — the
  per-scope directory Gotcha 3 below requires, without needing a
  hand-crafted `WEATHER_ENV_FILE` copy.

An explicit `ERA5_AREA`/`MERRA2_AREA`/`ERA5_WORK_DIR`/`MERRA_WORK_DIR`
always wins over `WEATHER_REGION`, so nothing below (the manual
per-scope `.env` approach, and everything in Gotchas 1-3) stops working.

!!! warning "Single country only — not a replacement for the union workflow below"
    This does not replace the multi-country union this page's own worked
    example uses (the AT/NL/DE/CZ box further down) — there is no
    `WEATHER_REGION=germany,austria,...`. For a union of countries, or
    any bbox not in `weather geo list`, the manual `WEATHER_ENV_FILE`
    approach in the rest of this page is still how you do it.

**Does not apply to COSMO-REA6** — it has no `AREA` parameter (see
below) and always downloads the full European domain, so `WEATHER_REGION`
does not affect its `WORK_DIR`. Use `weather geo crop` (see the last
section on this page) to produce a genuinely region-scoped COSMO file.

## The mechanism

`ERA5_AREA` and `MERRA2_AREA` (`.env`, order `North,West,South,East`)
are not local filters. `ERA5_AREA` is passed directly into the CDS
`area=[N,W,S,E]` request parameter; `MERRA2_AREA` is converted into an
OPeNDAP grid-index constraint expression in the download URL itself
(`EnvSettings.merra2_area()` / `Merra2Downloader._bbox_indices`). Both
providers' own servers do the subsetting — nothing outside the box is
ever transferred.

**COSMO-REA6 has no equivalent setting.** There is no `COSMO_AREA` (or
similar) anywhere in the codebase, and none was added for this. COSMO-
REA6 isn't a global product being subset — DWD ships it as an
already-fixed European-domain regional reanalysis, so there's no
server-side region parameter to narrow. It stays whole-Europe-or-nothing
regardless of anything on this page, and it's the provider with by far
the largest footprint (see results below).

## Computing a bounding box for a country set

`src/weather/geo/countries.py` already has per-country boxes for 31
European countries (`BBox(north, west, south, east)`, degrees WGS84).
Union the ones you need:

```python
from weather.geo.countries import get_bbox

countries = ["germany", "netherlands", "austria", "czech_republic"]
boxes = [get_bbox(c) for c in countries]
north = max(b.north for b in boxes)
west = min(b.west for b in boxes)
south = min(b.south for b in boxes)
east = max(b.east for b in boxes)
print(f"{north},{west},{south},{east}")
```

For AT/NL/DE/CZ this gives **`54.983,3.358,46.372,18.859`** — roughly
8% of the area of the full Europe box (`72,-11,34,32`), since it's
bounded by Germany's north, Netherlands' west, Austria's south, and
Czech's east.

A rectangular union isn't the same as the countries' actual shapes — it
also covers slivers of neighbours in between (Belgium, Switzerland,
Poland, etc.). That's expected and harmless; it's still far smaller than
all of Europe.

## Gotcha 1: shell-level env var overrides are silently ignored

`weather.settings`'s own docstring documents this priority order:

```text
1. Environment variable (highest).
2. .env file loaded once at import time.
3. Sensible defaults coded here.
```

**This is not what actually happens for any key `.env` already sets.**
`weather.common.env.load_repo_env()` calls
`load_dotenv(candidate, override=True)` — which unconditionally
overwrites the process environment with `.env`'s values for every key
`.env` defines. Since `.env` (copied from `.env.example`) explicitly
sets both `ERA5_AREA=72,-11,34,32` and `MERRA2_AREA=72,-11,34,32`, a
plain shell prefix does **not** work:

```bash
# Looks like it should work. Does not -- .env silently wins.
ERA5_AREA=54.983,3.358,46.372,18.859 \
    python src/weather/tests/test_era5_one_month.py --year 2018 --month 7
```

Confirmed by running exactly this: the resulting archive's lat/lon
range came back as the full Europe box, not the requested one, with no
error or warning — the override was just silently lost. `settings.py`'s
documented priority is wrong for this exact case.

**What actually works** — `WEATHER_ENV_FILE`, the one override
`load_repo_env()` checks *before* touching `.env` (so it isn't itself
subject to the same clobbering):

```bash
cp .env .env.4country
sed -i 's/^ERA5_AREA=.*/ERA5_AREA=54.983,3.358,46.372,18.859/' .env.4country
sed -i 's/^MERRA2_AREA=.*/MERRA2_AREA=54.983,3.358,46.372,18.859/' .env.4country

WEATHER_ENV_FILE=$(pwd)/.env.4country \
    python src/weather/tests/test_era5_one_month.py --year 2018 --month 7 --ncores 4
WEATHER_ENV_FILE=$(pwd)/.env.4country \
    python src/weather/tests/test_merra2_one_month.py --year 2018 --month 7 --ncores 4
```

Copy the full `.env` (not a fresh minimal file) so credentials
(`ERA5_CDS_KEY`, `EARTHDATA_USERNAME`/`PASSWORD`) travel with it —
`WEATHER_ENV_FILE` replaces which file is loaded, it doesn't merge with
the default `.env`.

## Gotcha 2: changing AREA between runs needs a clean download dir

Changing `ERA5_AREA`/`MERRA2_AREA` and re-running without clearing the
provider's `download/` directory for the affected month(s) first can
silently merge mismatched grids from two different runs. Hit this for
real: after fixing gotcha 1, a MERRA-2 retry that reused one stale
full-Europe-scoped daily file alongside 29 freshly 4-country-scoped
ones failed with:

```text
ValueError: Resulting object does not have monotonic global indexes along dimension lon
```

MERRA-2 downloads one file per day (30 files/month, x3 datasets) — a
partial clean leaves a mix of grids. ERA5-Land downloads one combined
file per month, so it's less exposed to *this* specific failure mode,
but `--skip-download` after an AREA change would still silently reuse a
stale-area GRIB with no error. Always fully clear the relevant
`download/` (and `output/`) files for the month(s) being re-run when
the AREA changes, not just the file(s) you think changed:

```bash
rm -f data/merra2/download/*202407* data/merra2/output/MERRA2_2024_07_all_attrs.nc
```

## Gotcha 3: never mix different-AREA archives in the same output directory

Found running the ERA5-Land 4-country test with June 2018's whole-Europe
file still sitting in the same `output/` directory. Boundary repair
scans the whole directory for a predecessor month to source the correct
first-hour value from. It found June, correctly detected the grid
mismatch (`GRID MISMATCH — current (86, 155) vs previous (381, 431)`),
and safely refused to use it — but then left July **entirely
unrepaired** instead of falling back to archive-start (NaN-blank)
treatment the way it would with no predecessor present at all.

This is not a benign warning. The resulting file's first hour held the
raw accumulated-total artifact — `GHI` mean **6950.7 W/m²**, physically
impossible, exactly the spike `point_query.py`'s own `RuntimeError`
guard exists to catch. A `get_point_weather()` call against it would
correctly refuse to serve it, so nothing downstream gets silently
poisoned — but the archive itself sat in a broken, needs-manual-fixing
state until this was caught and repaired by hand:

```bash
mkdir -p data/era5_land/output_4country
mv data/era5_land/output/ERA5_LAND_2018_07_all_attrs.nc data/era5_land/output_4country/
python -m weather.providers.era5_land.boundary_repair data/era5_land/output_4country
# -> "2018-07 (archive start): first stamp -> NaN" -- correct, once isolated
```

**Conclusion: different-AREA archives need different `WORK_DIR`s, full
stop — not just as a nice-to-have organizational choice, but because
mixing them silently breaks the boundary-repair mechanism.** A
per-country (or per-scope) directory layout isn't optional once more
than one AREA is ever used against the same provider over time.
`WEATHER_REGION` (see above) now gets this automatically for a single
country; a multi-country union like AT/NL/DE/CZ below still needs the
manual `WEATHER_ENV_FILE` approach this page describes.

## Results — whole Europe vs. AT/NL/DE/CZ, one month each

| Provider | Scope | Download size | Output size | Total time | Grid |
|---|---|---|---|---|---|
| ERA5-Land | Europe (72,-11,34,32) | 1408.4 MB | 1181.1 MB | 1427.8 s (~24 min, ~20 min CDS queue) | 381×431 |
| ERA5-Land | AT/NL/DE/CZ | 196.0 MB | 148.2 MB | 1638.4 s (~27 min, ~26.5 min CDS queue this run) | 86×155 |
| MERRA-2 | Europe (72,-11,34,32) | — | 116.4 MB | 62.3 s | 77×71 |
| MERRA-2 | AT/NL/DE/CZ | — | 11.6 MB | 17.4 s | 19×27 |
| COSMO-REA6 | Europe (fixed, no alternative) | — | 9.9 GB | 441.9 s | 824×848 |

MERRA-2: **~10x smaller, ~3.6x faster.** ERA5-Land: **~7-8x smaller**
(download and output both), matching the ~8% area ratio reasonably
closely in both cases.

**Total wall time was *not* faster for ERA5-Land this run (1638s vs
1428s) — CDS queue time dominates and varies run-to-run independent of
request size** (~20 min this session's June run, ~26.5 min this July
run, for requests of very different sizes). This matches
`BULK_RUN_GUIDE_ERA5-LAND.md`'s own conclusion: "you are download-bound,
not compute-bound," and the CDS queue itself isn't reduced by asking
for less area — only the transfer and processing time once your
request is actually served are. Country-scoping still wins decisively
on storage, and on transform time (a real, if secondary, contributor)
once the request lands — just not on total wall-clock for a single
ERA5-Land request against a queue whose wait time doesn't scale with
what you're asking for.

## Related: making `weather geo crop` work at all

Independently of the above, `weather geo crop`'s post-hoc cropping
(`crop_to_country()`) was found broken against real ERA5-Land/MERRA-2
output — CDO rejected both with `Unsupported grid type: generic`. Root
cause: the `latitude`/`longitude` coordinate variables written in
`providers/{merra2,era5_land}/transform.py` carried no CF attributes
(`standard_name`/`units`) despite the file's own `Conventions = "CF-1.8"`
claim. Fixed in both files (attrs added right after each
`assign_coords` call) — cropping now works.

**COSMO-REA6 was also fixed, and now crops correctly too.** Its
`providers/cosmo_rea6/export.py` had the same category of gap — 2-D
`latitude`/`longitude` coordinates with no CF attrs — plus one extra
wrinkle: a first attempt that also set an explicit `coordinates`
attribute on every data variable (mirroring the regular-grid providers'
`coordinates="latitude longitude"` convention) failed with `ValueError:
'coordinates' found in both attrs and encoding for variable 'U_10M'` —
xarray's own `to_netcdf()` already auto-populates that per variable via
`encoding` for any dataset with non-dimension coordinates, so setting it
by hand in `attrs` too collides. Fixed by adding only the coordinate
variables' own `standard_name`/`units`/`long_name` (`_add_cf_grid_attrs`
in `export.py`) and leaving the `coordinates` attribute to xarray.
Deliberately did not reconstruct the native rotated-pole `rlat`/`rlon`
axes or add a formal CF `grid_mapping` variable — bigger than a bbox
crop needs, and every rotation parameter
(`GRIB_latitudeOfSouthernPoleInDegrees` etc.) already survives on each
data variable's own GRIB_* attrs regardless.

Verified against real data, not just "no error": cropping COSMO-REA6's
June 2018 archive to Netherlands went from 9.9 GB to 101 MB
(`y×x`: 824×848 → 56×50), and a point-query against the cropped file
returned **values identical** to the same query against the original
uncropped file (T mean 16.84°C, GHI max 858.4 W/m², DNI max
760.9 W/m² — exact match) — confirms the crop subsets cleanly with zero
data corruption, not just that CDO stopped erroring.
