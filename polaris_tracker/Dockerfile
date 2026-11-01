# syntax=docker/dockerfile:1
# Polaris presence tracker (Gleam on the BEAM). Pinned to Gleam 1.12 like CI (see the
# README's "Gleam version"); the runtime uses the same image so the OTP version matches
# the one the code was compiled with.

FROM ghcr.io/gleam-lang/gleam:v1.12.0-erlang-alpine AS build
WORKDIR /src
COPY gleam.toml manifest.toml ./
RUN gleam deps download
COPY src ./src
RUN gleam export erlang-shipment

FROM ghcr.io/gleam-lang/gleam:v1.12.0-erlang-alpine
RUN addgroup -S app && adduser -S -G app app
COPY --from=build --chown=app:app /src/build/erlang-shipment /app
USER app
ENV TRACKER_BIND=0.0.0.0 \
    TRACKER_PORT=4100
EXPOSE 4100
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["run"]
