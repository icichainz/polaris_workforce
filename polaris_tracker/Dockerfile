# syntax=docker/dockerfile:1
# Polaris presence tracker (Gleam on the BEAM). Gleam 1.19.2, like the dev machine (see the
# README's "Gleam version"); the runtime uses the same image so the OTP version matches
# the one the code was compiled with.

FROM ghcr.io/gleam-lang/gleam:v1.19.2-erlang-alpine AS build
# sqlight builds esqlite's bundled SQLite (a NIF) from C.
RUN apk add --no-cache build-base
WORKDIR /src
COPY gleam.toml manifest.toml ./
RUN gleam deps download
COPY src ./src
RUN gleam export erlang-shipment

FROM ghcr.io/gleam-lang/gleam:v1.19.2-erlang-alpine
RUN addgroup -S app && adduser -S -G app app \
    && mkdir -p /data && chown app:app /data
COPY --from=build --chown=app:app /src/build/erlang-shipment /app
USER app
# Tracks and the outbox live in /data/tracker.sqlite3 (plus its -wal and -shm files):
# mount a volume there, or they are lost with the container.
ENV TRACKER_BIND=0.0.0.0 \
    TRACKER_PORT=4100 \
    TRACKER_DB_PATH=/data/tracker.sqlite3
VOLUME /data
EXPOSE 4100
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["run"]
