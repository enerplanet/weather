# weather

<!-- Badges point at enerplanet/weather (this fork), not UU-BUEM/weather
     (upstream): this fork currently releases its own versions, tagged
     directly on the enerplanet branch. Repoint at UU-BUEM/weather if this
     fork is ever merged back upstream. -->
[![CI](https://github.com/enerplanet/weather/actions/workflows/ci.yml/badge.svg)](https://github.com/enerplanet/weather/actions/workflows/ci.yml)&nbsp;&nbsp;&nbsp;[![Release](https://github.com/enerplanet/weather/actions/workflows/release.yml/badge.svg)](https://github.com/enerplanet/weather/actions/workflows/release.yml)

Standalone download/decompress/transform pipeline that standardises three
weather reanalysis products — `COSMO-REA6`, `ERA5-Land`, `MERRA-2` — into
one shared hourly schema (`T`, `GHI`, `DHI`, `DNI`, `RH`, `ALBEDO`,
`SNOWFALL`, `SNOW_DEPTH`, `WS_10M`, ...), for downstream energy modelling
(solar gains, heating/cooling demand, renewable potential).

!!! info "Not a regridding or bias-correction tool"
    Each provider keeps its own native grid and formula family — see
    [Cross-provider differences](provider_differences.md). Values are
    renamed to a shared schema, not unified or interpolated onto a common
    grid.

## Documentation

Start with **[Pipeline architecture](parallelization.md)** to understand the
three-phase pipeline before running anything, then **[FAQ](qa.md)** for the
questions that come up on a first run.

| Section | Description |
|---|---|
| [Getting started](parallelization.md) | Pipeline architecture, FAQ, debugging |
| [Provider guides](MERRA2_PIPELINE_GUIDE.md) | Per-provider run instructions, bulk multi-year jobs, disk/memory budgets |
| [Methodology](provider_differences.md) | Cross-provider differences, DNI/DHI decomposition, TMY percentile selection |
| [API reference](openapi/index.html) | Interactive Swagger UI for `weather serve`'s point-query HTTP API |
| [Contributing](git-push-workflow.md) | Branch and push conventions |

## Related resources

- Root [`README.md`](https://github.com/enerplanet/weather/blob/enerplanet/README.md) — quick-start installation and run instructions.
- [`src/weather/api/README.md`](https://github.com/enerplanet/weather/blob/enerplanet/src/weather/api/README.md) — HTTP API design, auth, and running `weather serve`.
- [`src/weather/tests/README.md`](https://github.com/enerplanet/weather/blob/enerplanet/src/weather/tests/README.md) — which test script to run and in what order.
- [`src/weather/common/README.md`](https://github.com/enerplanet/weather/blob/enerplanet/src/weather/common/README.md) — what each shared utility module does.
- [`src/weather/providers/README.md`](https://github.com/enerplanet/weather/blob/enerplanet/src/weather/providers/README.md) — provider pattern, how to add a new provider.

## Repository

[github.com/enerplanet/weather](https://github.com/enerplanet/weather) ·
[Issue tracker](https://github.com/enerplanet/weather/issues)
