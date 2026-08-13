# ──────────────────────────────────────────────────────────────────────────────
# Weather Pipeline — Docker Image  (COSMO-REA6 / multi-provider, deps-only)
# ──────────────────────────────────────────────────────────────────────────────
# Multi-stage build:
#   Stage 1 (builder) — create the conda environment
#   Stage 2 (runtime) — lean image with only the finished env + entrypoint
#
# Source code is BIND-MOUNTED at /app/src at runtime → no rebuild on code edits.
# Rebuild only when weather_env.yml changes (new/removed packages).
#
# ── Build ──────────────────────────────────────────────────────────────────
#   docker build -f infrastructure/container/Dockerfile \
#       -t weather-cosmo-rea6:latest .
#
# ── Single-year run ────────────────────────────────────────────────────────
#   docker run --rm \
#       -v "$(pwd)/src":/app/src:ro \
#       -v /data/cosmo_rea6:/data \
#       -e PIPELINE_MODE=single-year \
#       -e COSMO_YEAR=2005 \
#       -e COSMO_NCORES=4 \
#       weather-cosmo-rea6:latest
#
# ── Multi-year run ─────────────────────────────────────────────────────────
#   docker run --rm \
#       -v "$(pwd)/src":/app/src:ro \
#       -v /data/cosmo_rea6:/data \
#       -e PIPELINE_MODE=multi-year \
#       -e COSMO_FROM_YEAR=1995 \
#       -e COSMO_TO_YEAR=2018 \
#       -e COSMO_NCORES=4 \
#       weather-cosmo-rea6:latest
#
# ── Smoke test (no data required) ─────────────────────────────────────────
#   docker run --rm \
#       -v "$(pwd)/src":/app/src:ro \
#       -e PIPELINE_MODE=check \
#       weather-cosmo-rea6:latest
#
# ── docker-compose (recommended) ──────────────────────────────────────────
#   docker compose -f infrastructure/container/docker-compose.yml \
#       run cosmo-rea6-check
#
# ── Apptainer on HPC (Snellius) ────────────────────────────────────────────
#   docker push <registry>/weather-cosmo-rea6:latest
#   apptainer pull docker://<registry>/weather-cosmo-rea6:latest
# ──────────────────────────────────────────────────────────────────────────────

FROM continuumio/miniconda3:24.1.2-0 AS builder

# Install system tools for decompression
RUN apt-get update \
    && apt-get install -y --no-install-recommends lbzip2 pbzip2 \
    && rm -rf /var/lib/apt/lists/*

# Create the conda environment from the pinned spec file
COPY infrastructure/env/weather_env.yml /tmp/weather_env.yml
RUN conda env create -f /tmp/weather_env.yml \
    && conda clean -afy \
    && rm /tmp/weather_env.yml

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM continuumio/miniconda3:24.1.2-0

LABEL org.opencontainers.image.source="https://github.com/enerplanet/weather"
LABEL org.opencontainers.image.description="COSMO-REA6 weather pipeline — UU-BUEM"
LABEL org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends lbzip2 pbzip2 curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the pre-built conda environment from builder
COPY --from=builder /opt/conda/envs/weather_env /opt/conda/envs/weather_env

# gunicorn (WSGI server for PIPELINE_MODE=serve) is deliberately not in
# weather_env.yml itself -- it's Unix-only (fork()-based), same reasoning
# as buem's own api Dockerfile. Everything else `weather serve` needs
# (flask, pvlib, pyarrow, xarray) is already in weather_env.yml.
RUN /opt/conda/envs/weather_env/bin/pip install gunicorn

# Copy entrypoint into the image (not bind-mounted — it is container infrastructure)
COPY infrastructure/container/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Activate environment
ENV PATH=/opt/conda/envs/weather_env/bin:$PATH
ENV CONDA_DEFAULT_ENV=weather_env

# Source code is bind-mounted at /app/src at runtime
ENV PYTHONPATH=/app/src

# Pipeline defaults (overridden by -e or docker-compose environment:)
ENV PIPELINE_MODE=""
ENV COSMO_YEAR=2018
ENV COSMO_FROM_YEAR=1995
ENV COSMO_TO_YEAR=2018
ENV COSMO_NCORES=4
ENV COSMO_PARALLEL_YEARS=1
ENV COSMO_WORK_DIR=/data

# Prevent HDF5 file-locking deadlocks on GPFS / NFS file systems (HPC)
ENV HDF5_USE_FILE_LOCKING=FALSE

# weather serve (PIPELINE_MODE=serve) listens here; see entrypoint.sh
EXPOSE 8080

WORKDIR /app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Default: show pipeline configuration (overridden by PIPELINE_MODE)
CMD []
