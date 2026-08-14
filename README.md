# Weather

## Pipeline design intention

This pipeline is designed for server runs and not individual computer.
It produces either raw or processed data which can be used for
modules related to estimating renewable energy potentials, solar
gains in buildings, and demands of multiple energy-related sectors.
Currently, this pipeline standardizes three weather databases:

- `COSMO-REA6`
- `MERRA2`
- `ERA5-LAND`

<!-- markdownlint-disable MD013 -->
[![CI](https://github.com/UU-BUEM/weather/actions/workflows/ci.yml/badge.svg)](https://github.com/UU-BUEM/weather/actions/workflows/ci.yml)
[![Release](https://github.com/UU-BUEM/weather/actions/workflows/release.yml/badge.svg)](https://github.com/UU-BUEM/weather/actions/workflows/release.yml)
[![MkDocs](https://github.com/enerplanet/weather/actions/workflows/docs.yml/badge.svg?branch=enerplanet)](https://enerplanet.github.io/weather)
<!-- markdownlint-enable MD013 -->

Standalone weather processing repository for `UU-BUEM`.

This repository uses a provider-based architecture with a standard Python `src/` layout and
separate infrastructure folders for environment and container assets.

## Project Structure

```text
weather/
├── src/                           # Python package (src-layout standard)
│   └── weather/
│       ├── common/                # shared, provider-agnostic mechanics
│       │   ├── derived_attributes.py   # shared GHI/DHI/DNI/RH/WS formulas
│       │   ├── dni_reconstruction.py   # shared pvlib DIRINT/DISC decomposition
│       │   ├── geo_lookup.py           # nearest-cell lookup (COSMO's grid)
│       │   ├── cli_flags.py            # shared --cleanup/--resume/... flags
│       │   ├── solar_position.py  download.py  decompress.py  parallel.py
│       │   └── merge.py  percentile.py  net.py  validate.py  cleanup.py  env.py
│       ├── geo/                   # country bbox + NetCDF cropping (`weather geo`)
│       │   └── bbox.py  countries.py  crop.py  __init__.py
│       ├── providers/
│       │   ├── base.py  base_downloader.py  base_decompressor.py  base_percentile.py
│       │   ├── cosmo_rea6/        # config, download(er), decompress(or),
│       │   │                      # transform, export, pipeline, percentile_index
│       │   ├── merra2/            # same module roles as cosmo_rea6
│       │   └── era5_land/         # same module roles as cosmo_rea6
│       ├── tests/                 # pytest units + pipeline-runner CLI scripts
│       │   ├── test_derived_attributes.py  test_validation.py
│       │   ├── test_pipeline_integration.py  test_point_query.py
│       │   ├── test_geo_countries.py  compare_providers.py
│       │   ├── test_{cosmo,era5,merra2}_{one_month,one_year,multi_year}.py
│       │   └── (see src/weather/tests/README.md for the full catalog)
│       ├── __init__.py            # re-exports get_point_weather
│       ├── point_query.py         # get_point_weather(lat, lon, year, provider=...)
│       ├── __main__.py
│       ├── cli.py
│       ├── from_csv.py
│       ├── registry.py
│       └── settings.py
├── infrastructure/
│   ├── env/
│   │   └── weather_env.yml
│   └── container/
│       ├── Dockerfile
│       ├── docker-compose.yml
|       ├── entrypoint.sh
|       └── weather.def
├── scripts/
│   ├── build_container.sh
│   ├── common.sh  
│   ├── decompress.sh 
│   ├── download.sh 
│   ├── grb.sh build_container.sh
│   ├── run_pipeline_container.sh 
│   ├── run_pipeline.sh
│   └── setup_env.sh
├── meta.yaml           # Conda build recipe (at repo root)
├── pyproject.toml      # Package metadata & setuptools config
├── setup.ps1           # Windows PowerShell setup script
├── setup.bat           # Windows cmd.exe setup script
├── setup.sh            # For local Linux/macOS and HPC systems
├── .github/
│   ├── workflows/
│   │   ├── ci.yml      # Lint, type-check, test on push/PR
│   │   └── release.yml # Build & publish on v* tag
│   └── agents/
│       └── uu-buem-align.agent.md
├── .gitignore
├── LICENSE
├── README.md
└── CONTRIBUTING.md
```

## Provider Model

- `cosmo-rea6`: implemented (reference provider)
- `merra-2`: implemented
- `era5-land`: implemented

Naming recommendation:

- Keep `providers` as the folder name.
- Reason: this is the most common and industry-recognized term for pluggable
  data backends/sources; alternatives like `specific` are less explicit.
- If preferred, `sources` is a valid alternative, but `providers` is clearer
  for code architecture and extension.

Pipeline stages per provider:

- `download`
- `decompress`
- `transform`
- `final processing` (export)

Segregation rule:

- `src/weather/common/`: shared mechanics (e.g., HTTP/FTP download helpers,
  decompression primitives, retry/rate-limit/auth utilities).
- `src/weather/providers/<dataset>/`: dataset-specific definitions (variable
  lists, filenames/endpoints, transformations, derived fields, orchestration).

## Run Paths

For a source checkout, install the package in editable mode first:

```bash
conda env create -f infrastructure/env/weather_env.yml
conda activate weather_env
pip install -e .
```

Then use `python -m weather ...` or the `weather` console script:

```bash
python -m weather info
python -m weather validate
python -m weather run --provider cosmo-rea6 --months 1
```

If you install the package as a conda recipe, the `weather` command is
available directly:

```bash
weather info
weather validate
weather run --provider cosmo-rea6 --months 1
```

The pipeline for single-month, single-year, and multi-year runs is ready
for all three providers (`cosmo-rea6`, `era5-land`, `merra-2`). Each
provider has the same three CLI runners under `src/weather/tests/`:

```bash
python ./src/weather/tests/test_cosmo_one_month.py --year 2018 --month 1
python ./src/weather/tests/test_cosmo_one_year.py --year 2018 --ncores 80
python ./src/weather/tests/test_cosmo_multi_year.py --from-year 1995 --to-year 2018
```

(swap `cosmo` for `era5`/`merra2` for the other two providers). Common
flags: `--work-dir`, `--ncores`, `--skip-download`, `--resume` (skip
periods whose output already exists), `--cleanup` (delete intermediates
after a successful export — default is to keep everything unless the
provider's `*_CLEANUP` env var is set). Full reference: `--help`; see
`src/weather/tests/README.md` for the complete catalog.

Default provider can be set with:

```bash
export WEATHER_PROVIDER=cosmo-rea6
```

## Point-query API (for downstream consumers)

For a downstream package (e.g. `buem`) that just needs hourly weather at
one building/location — not the full download/transform pipeline —
`weather.get_point_weather()` reads an already-processed provider
archive directly:

```python
from weather import get_point_weather

df = get_point_weather(52.0, 5.0, 2018, provider="era5-land", use_case="solar")
# df: hourly DatetimeIndex, columns T (degC), GHI/DHI/DNI (W/m2)
```

Install just enough for this (no GRIB/download stack): `pip install
weather[pointquery,solar]`. Requires a provider archive that's already
been produced by a normal pipeline run for that `(provider, year)`.

## HTTP API (`weather serve`)

For a caller that can't import this package directly, `weather serve`
exposes the same point query over HTTP.

Interactive reference (Swagger UI, renders `docs/openapi/openapi.yaml`):
https://enerplanet.github.io/weather/openapi/

Full design, auth, and run instructions: `src/weather/api/README.md`.

## Country cropping (`weather geo`)

```bash
weather geo list
weather geo crop --input ERA5_LAND_2018_all_attrs.nc \
    --output ERA5_LAND_2018_netherlands.nc --country netherlands
```

Crops an already-exported ERA5-Land/MERRA-2 NetCDF to a country's
bounding box via `cdo sellonlatbox`. Not yet supported for COSMO-REA6
output on `cdo`-side cropping; see `CLAUDE.md` for the current status.

## Shell Script Paths

- Shared script config: `scripts/common.sh`
- Slurm full run: `scripts/run_pipeline.sh`
- Slurm container run: `scripts/run_pipeline_container.sh`
- Build container image: `scripts/build_container.sh`
- Create/update conda env: `scripts/setup_env.sh`

Default server paths used by scripts:

- Repository: `~/weather`
- Python source root: `~/weather/src`
- Data/work dir: `<repo>/data/cosmo_rea6` (or override in `.env`)

## Container and Environment Paths

- Conda environment file: `infrastructure/env/weather_env.yml`
- Dockerfile: `infrastructure/container/Dockerfile`
- Conda recipe: `meta.yaml`
- Apptainer definition: `infrastructure/container/weather.def`
- Compose file: `infrastructure/container/docker-compose.yml` (canonical;
  the root `docker-compose.yml` was removed to avoid duplication)

## Path Configuration (.env)

Create `.env` from `.env.example` to keep all runtime paths centralized.

```bash
cp .env.example .env
```

Key variables:

- `WEATHER_DATA_DIR` (default fallback: `<repo>/data`)
- `COSMO_WORK_DIR` (default fallback: `<WEATHER_DATA_DIR>/cosmo_rea6`)
- `WEATHER_PROVIDER` (default: `cosmo-rea6`)

Build examples:

```bash
# Docker
bash scripts/build_container.sh docker

# Apptainer (definition build)
bash scripts/build_container.sh def
```

## Notes

- All three providers (COSMO-REA6, ERA5-Land, MERRA-2) are implemented
  and share the same module roles / pipeline shape.
- New provider-specific modules should be added under `src/weather/providers/<provider_name>/`.
