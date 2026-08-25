"""NetCDF-4 monthly-to-annual merge utilities.

``NetCDFMerger`` concatenates monthly NetCDF-4 files along the time
dimension using ``xarray.open_mfdataset`` + ``to_netcdf``, chunked so
peak RAM stays bounded regardless of total file size (real COSMO-REA6
monthly files are ~9-11 GB each; a naive concatenation would try to hold
a full year -- 100+ GB -- in memory at once).

Typical post-processing usage (after all monthly NC files are ready)::

    from weather.common.merge import NetCDFMerger
    from pathlib import Path

    monthly = sorted(
        Path("/data/output").glob("COSMO_REA6_2005_??_all_attrs.nc")
    )
    annual = Path("/data/output/COSMO_REA6_2005_annual_all_attrs.nc")
    NetCDFMerger.merge(
        monthly_paths=[str(p) for p in monthly],
        output_path=annual,
    )

Or use the CLI convenience script::

    python -m weather.common.merge \\
        --input  /data/output/COSMO_REA6_2005_??_all_attrs.nc \\
        --output /data/output/COSMO_REA6_2005_annual_all_attrs.nc

History
-------
Originally hand-rolled directly against h5py (no xarray/dask), for
performance on large files over GPFS/NFS. Replaced 2026-08 after real
end-to-end testing (the first time anything had actually re-opened a
merged file with the standard toolchain, not just run the merge and
trust its own docstring) surfaced two independent, serious bugs in that
implementation:

1. **Unreadable output.** The hand-rolled version never established
   HDF5 dimension-scale relationships (``make_scale``/``attach_scale``)
   for the merged datasets -- valid generic HDF5, but not valid
   NetCDF-4: ``netCDF4``/``xarray`` (which is to say, every other
   consumer in this codebase) failed to open the result at all with
   ``AttributeError: 'NoneType' object has no attribute 'dimensions'``.
2. **Silently wrong timestamps.** Real per-provider monthly files encode
   ``time`` as "minutes since <that month's own start>" -- i.e. every
   month's raw values reset to 0. The hand-rolled version appended raw
   values verbatim under the FIRST file's ``units`` attribute, so every
   month after the first decoded to nonsense, overlapping dates -- no
   error, just corrupted data.

``xr.open_mfdataset`` avoids both by construction: it decodes each
file's ``time`` values against that file's own ``units`` *before*
concatenating (real ``datetime64`` throughout, not raw per-file-relative
integers), and ``to_netcdf`` always writes correct, standard dimension
scales. Chunking preserves the original bounded-memory design goal via
dask instead of hand-rolled slicing. See ``.claude/open.md`` /
``docs/debugging.md`` for the full writeup and the real repro that found
both bugs.

A follow-up pass (also 2026-08) fixed the chunking itself: the original
``chunks={"time": _COPY_CHUNK}`` left every OTHER dimension completely
unchunked, forcing every read to pull the FULL spatial extent per
time-block -- real chunk inspection (``h5py.Dataset.chunks`` on actual
COSMO-REA6 output) found the on-disk chunks are genuinely 3-D, not
time-only. A real, full-scale, unsliced before/after measurement (2 real
COSMO-REA6 months, ~18.4 GB combined) confirmed adding spatial chunking
+ capping dask's worker threads cuts wall time by 31% (51.4 -> 35.5 min)
at the cost of somewhat higher peak memory (12.3 -> 15.5 GB -- trivial on
the target server's 1 TB RAM). See :meth:`NetCDFMerger._compute_chunks`
and the ``_SPATIAL_CHUNK_DIVISOR``/``_MAX_MERGE_WORKERS`` class
attributes for the mechanism, and ``.claude/open.md``'s ``## unified_cli``
entry for the full investigation (including why the chunk spec is
computed from each dataset's own real dimension sizes rather than
hardcoded -- spatial dimension NAMES differ by provider).
"""

from __future__ import annotations

import argparse
import logging
import os
import tempfile
from pathlib import Path


class NetCDFMerger:
    """Merge monthly NetCDF-4 files into one annual file via xarray/dask.

    All methods are class-level; instantiation is not required::

        NetCDFMerger.merge(monthly_paths, output_path)
    """

    _TIME_DIM: str = "time"
    _TIME_CHUNK: int = 168  # timesteps per dask chunk (≈ 1 week)

    # Spatial chunking: a fixed time-only chunk leaves every OTHER
    # dimension unchunked, forcing dask to read the full spatial extent
    # per time-block -- confirmed via real on-disk chunk inspection
    # (COSMO's own per-file auto-chunking is 3-D, not time-only) and a
    # real full-scale before/after measurement (2 real COSMO months,
    # unsliced): 51.4 min -> 35.5 min (-31%) from adding spatial chunking
    # + capping worker threads alone, no chunk-shape change beyond that.
    # Computed generically from each dataset's own dimension sizes at
    # merge time (see _compute_chunks) rather than hardcoded, since the
    # spatial dimension NAMES differ by provider (cosmo-rea6: y/x;
    # era5-land/merra-2: latitude/longitude) and COSMO's own per-file
    # on-disk chunk shape isn't even consistent month-to-month (varies
    # with each month's length) -- a fixed divisor of each dim's real
    # size is the one thing that generalizes across all of that.
    _SPATIAL_CHUNK_DIVISOR: int = 4
    _MIN_SPATIAL_CHUNK: int = 50  # don't over-fragment small/coarse grids

    # Real full-scale measurement: 16 threads matched 48/default(cpu_count)
    # threads' wall time within noise (240-256s on a 120-hour slice) while
    # using far less peak memory (3.3GB vs 13GB at 96 threads) -- this
    # workload is decompression/IO-bound, not thread-count-bound, past a
    # fairly low ceiling. Scoped via a `with dask.config.set(...):` block
    # around just this merge, not a global override, so it can't leak into
    # any other dask usage in the same process.
    _MAX_MERGE_WORKERS: int = 16

    # Matches this repo's own uniform export convention (all three
    # providers' export.py: zlib complevel=1, float32) -- confirmed
    # already consistent across cosmo-rea6/era5-land/merra-2 output, so
    # this is hardcoded rather than introspected per source file.
    _ENCODING = {"zlib": True, "complevel": 1, "dtype": "float32"}

    @classmethod
    def _compute_chunks(cls, reference_path: str) -> dict[str, int]:
        """Build a ``{dim: chunk_size}`` spec from *reference_path*'s own
        real dimension sizes -- generic across providers' different
        spatial dimension names (see the class docstring above).

        A cheap metadata-only open (no ``chunks=``, so nothing beyond
        small coordinate arrays is read eagerly) just to read
        ``.sizes``, closed immediately after.
        """
        import xarray as xr

        chunks: dict[str, int] = {cls._TIME_DIM: cls._TIME_CHUNK}
        with xr.open_dataset(reference_path) as ref:
            for dim, size in ref.sizes.items():
                dim = str(dim)
                if dim == cls._TIME_DIM:
                    continue
                target = max(cls._MIN_SPATIAL_CHUNK, size // cls._SPATIAL_CHUNK_DIVISOR)
                chunks[dim] = min(target, size)
        return chunks

    @classmethod
    def merge(
        cls,
        monthly_paths: list[str],
        output_path: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        """Concatenate monthly NetCDF-4 files along time using xarray/dask.

        Parameters
        ----------
        monthly_paths:
            Sorted list of monthly NC file paths (Jan … Dec).
        output_path:
            Destination annual NetCDF file (created or overwritten).
            Written atomically (temp file in the same directory, then
            renamed on success) so an interrupted merge never leaves a
            truncated file sitting at the final name.
        logger:
            Progress logger.  Falls back to module logger if omitted.

        Raises
        ------
        ImportError
            If xarray/dask are not installed in the current environment.
        RuntimeError
            If *monthly_paths* is empty or any source file is missing.
        """
        try:
            import dask
            import xarray as xr
        except ImportError as exc:
            raise ImportError(
                "xarray (and dask) are required for NetCDFMerger.merge(). "
                "Install via: conda install -c conda-forge xarray dask"
            ) from exc

        log = logger or logging.getLogger(__name__)
        time_dim = cls._TIME_DIM
        output_path = Path(output_path)

        if not monthly_paths:
            raise RuntimeError("No source files given to merge.")
        missing = [p for p in monthly_paths if not Path(p).exists()]
        if missing:
            raise RuntimeError(
                f"Missing {len(missing)} source file(s): "
                + ", ".join(str(p) for p in missing[:5])
                + (" …" if len(missing) > 5 else "")
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".part",
        )
        os.close(tmp_fd)

        chunks = cls._compute_chunks(monthly_paths[0])
        log.info(
            "  Opening %d monthly files (chunks: %s)",
            len(monthly_paths), chunks,
        )
        try:
            # combine="nested": files are already sorted by the caller
            # (Jan..Dec) -- no coordinate-based reordering/matching
            # needed. compat="override" + data_vars/coords="minimal":
            # skip cross-file equality-checking of static coords
            # (lat/lon/y/x don't change month-to-month within one merge;
            # checking would mean comparing potentially large 2-D COSMO
            # grids 12 times for no benefit).
            #
            # dask.config.set(...) is scoped to just this block (not a
            # bare/global call) so the worker cap can't leak into any
            # other dask usage in the same process -- see
            # _MAX_MERGE_WORKERS above for why 16.
            with (
                dask.config.set(
                    scheduler="threads", num_workers=cls._MAX_MERGE_WORKERS,
                ),
                xr.open_mfdataset(
                    monthly_paths,
                    combine="nested",
                    concat_dim=time_dim,
                    chunks=chunks,
                    data_vars="minimal",
                    coords="minimal",
                    compat="override",
                ) as ds,
            ):
                log.info(
                    "  Data variables: %s",
                    ", ".join(sorted(str(v) for v in ds.data_vars)),
                )
                encoding = {name: cls._ENCODING for name in ds.data_vars}
                log.info("  Writing merged dataset -> %s", output_path)
                ds.to_netcdf(tmp_path, encoding=encoding, engine="netcdf4")
                n_timesteps = ds.sizes[time_dim]

            Path(tmp_path).replace(output_path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

        log.info(
            "  merge complete: %d timesteps → %s  (%.1f MB)",
            n_timesteps,
            output_path.name,
            output_path.stat().st_size / (1024 * 1024),
        )


# ---------------------------------------------------------------------------
# CLI convenience entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Merge monthly COSMO-REA6 NetCDF-4 files into one annual "
            "file using xarray/dask."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--input", nargs="+", required=True, metavar="FILE",
        help=(
            "Monthly NC files to merge (glob or explicit list). "
            "Files are sorted before merging."
        ),
    )
    p.add_argument(
        "--output", required=True, metavar="FILE",
        help="Destination annual NC file path.",
    )
    return p.parse_args()


def _main() -> None:
    import glob  # noqa: PLC0415
    import sys  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    args = _parse_args()

    # Expand any globs (shell may not expand on Windows)
    paths: list[str] = []
    for pattern in args.input:
        expanded = sorted(glob.glob(pattern))
        paths.extend(expanded if expanded else [pattern])
    paths = sorted(paths)

    if not paths:
        sys.exit("No input files found matching the given pattern(s).")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    logging.getLogger(__name__).info(
        "Merging %d monthly files → %s", len(paths), output,
    )
    NetCDFMerger.merge(monthly_paths=paths, output_path=output)


if __name__ == "__main__":
    _main()
