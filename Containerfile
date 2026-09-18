# x86_64 image for TrueNAS. Data (harness.db, tmdb_cache.db, movielens/ease_lam500.npz)
# lives on a mounted volume at /data; nothing secret is baked in.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir numpy scipy pandas requests

COPY harness/ harness/
COPY engine/ engine/

ENV RECOMMENDARR_DATA=/data \
    ENGINE_PORT=8090 \
    ENGINE_BUILD_HOUR=4 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Runs as an arbitrary uid (the stack uses 568:568); nothing outside /data is written.
RUN mkdir -p /data && chown 568:568 /data
USER 568:568
VOLUME ["/data"]
EXPOSE 8090
CMD ["python", "-m", "engine", "serve"]
