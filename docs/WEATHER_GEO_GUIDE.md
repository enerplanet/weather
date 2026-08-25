# `weather geo` — country bbox lookup and post-hoc NetCDF cropping

`weather geo` is two small, standalone subcommands: `list` (country bounding-box
lookup) and `crop` (crop an *already-exported* NetCDF file to a country's
extent via `cdo sellonlatbox`). Unlike `weather fetch --country`/`--bbox`
(see `docs/WEATHER_FETCH_GUIDE.md`), it does not download or process
anything — it operates on a file you already have, and works the same way
regardless of which provider produced it or how long ago.

Not wired into any provider's pipeline — it's a manual, run-when-you-want-it
post-processing step against an existing output file.

--------------------------------------------------------------------

## Quick examples

```bash
# See every recognised country name
weather geo list

# Crop an already-exported ERA5-Land file to the Netherlands
weather geo crop --input ERA5_LAND_2018_all_attrs.nc \
    --output ERA5_LAND_2018_netherlands.nc --country netherlands

# Same, for a COSMO-REA6 annual file
weather geo crop --input COSMO_REA6_2018_annual_all_attrs.nc \
    --output COSMO_REA6_2018_netherlands.nc --country netherlands
```

--------------------------------------------------------------------

## `weather geo list`

No flags. Prints every recognised country name, one per line (currently
~30 European countries — the same list `weather fetch --country` accepts).
Source of truth: `weather.geo.countries.COUNTRIES`.

## `weather geo crop`

| Flag        | Required | Meaning                                                    |
| ----------- | -------- | ---------------------------------------------------------- |
| `--input`   | Yes      | Path to an existing NetCDF file (any provider's output).   |
| `--output`  | Yes      | Destination path for the cropped file.                     |
| `--country` | Yes      | Country name, e.g. `netherlands` (see `weather geo list`). |

There is no `--bbox` option at the CLI level (unlike `weather fetch`) — only
a recognised country name. The underlying `weather.geo.crop.crop_netcdf()`
function does accept an arbitrary `BBox` directly if you need a custom
extent, but that's a Python-API-only path today, not exposed on the CLI.

Exit code 1 with an `Error: ...` message on failure (missing input file,
`cdo` not installed, or `cdo` itself failing — see below); exit 0 and
`Cropped: <output path>` on success.

--------------------------------------------------------------------

## Provider support

Works against real output from all three providers today:

| Provider   | Works | Notes                                                                                   |
| ---------- | :---: | --------------------------------------------------------------------------------------- |
| ERA5-Land  |  Yes  | Regular 1-D lat/lon grid, CDO's simplest case (gridtype=lonlat).                        |
| MERRA-2    |  Yes  | Same as ERA5-Land. Retroactively patched (552/552 files).                               |
| COSMO-REA6 |  Yes  | Rotated-pole, 2-D coords (gridtype=curvilinear). Retroactively patched (298/298 files). |

**This was broken for all three providers** until 2026-08-14 — not just
COSMO, and not because of any provider's actual grid geometry. The cause
was a missing CF `standard_name`/`units` pair on every provider's
`latitude`/`longitude` coordinates, which made CDO's grid-type detection
fail regardless of whether the underlying grid was simple (ERA5-Land/
MERRA-2) or rotated (COSMO). Full investigation, root cause, and fix:
[cdo_crop_cf_metadata.md](cdo_crop_cf_metadata.md).

**If a file predates 2026-08-14 and was never retroactively patched**,
`weather geo crop` against it will fail with:

```text
cdo    sellonlatbox (Abort): No processable variable found!
```

— see the investigation doc for the retroactive-patch approach (metadata-only,
no data recomputation) if you hit this against an older archive.

## Requires `cdo`

`weather geo crop` needs the `cdo` binary on `PATH` (`conda install
-c conda-forge cdo`, already listed in `infrastructure/env/weather_env.yml`).
No conda-forge win-64 build exists, so this does not work at all on Windows —
`RuntimeError: cdo is not installed or not on PATH` is expected there, not a
bug. COSMO-REA6 has a separate, CDO-free alternative for the *fresh-fetch*
case specifically (`weather fetch --country`'s local index-based crop, see
`providers/cosmo_rea6/crop.py`) — but that's a different mechanism for a
different scenario (see "Relationship to `weather fetch`" below), not a
substitute for `weather geo crop` against an existing file.

--------------------------------------------------------------------

## Relationship to `weather fetch --country`/`--bbox`

Two different tools for two different situations, not competing solutions:

- **`weather geo crop`** (this page): the data already exists as a full-domain
  export. Crops it directly, no re-downloading or re-processing.
- **`weather fetch --country`/`--bbox`** (see `docs/WEATHER_FETCH_GUIDE.md`):
  the data doesn't exist yet. Crops (or, for ERA5-Land/MERRA-2, server-side
  restricts) *during* the fetch, so the file that lands on disk was never
  full-domain to begin with.

Using the wrong one for a given situation wastes real time: re-running
`weather fetch --country` against data you already have full-domain means
re-downloading and re-transforming everything just to discard most of it;
`weather geo crop` has no way to fetch data you've never downloaded at all.

--------------------------------------------------------------------

## See also

- `README.md` — brief overview and the rest of the CLI surface.
- `docs/WEATHER_FETCH_GUIDE.md` — the fresh-fetch, download-time cropping
  path, and the full country/bbox area-scoping story.
- `docs/cdo_crop_cf_metadata.md` — the CDO grid-detection bug this tool hit
  until 2026-08-14: root cause, fix, and the retroactive archive patch.
- `src/weather/geo/` — `bbox.py`/`countries.py`/`crop.py`, the actual
  implementation.
