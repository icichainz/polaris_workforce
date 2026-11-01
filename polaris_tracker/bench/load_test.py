#!/usr/bin/env python3
"""Load test for the tracker: BLE sightings from gateways over real HTTP.

Standard library only (tokens are minted with hmac, like Django will). Not run by
`gleam test`. Start a tracker first, for example:

    TRACKER_DEBUG=1 TRACKER_DB_PATH=/tmp/load.sqlite3 gleam run

then:

    python3 bench/load_test.py --rate 500 --duration 60 --sites 10 --batch 1

The schedule is open loop: request `i` is due at `start + i / request_rate`
whatever happened to earlier ones, so a slow tracker shows up as latency
(measured from the due time, "queued") instead of as a lower send rate.
`service` latency is measured from the moment the request was written.
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import sqlite3
import statistics
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

DEV_SECRET = "dev-only-insecure-tracker-secret"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def dumps(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def mint(secret: bytes, sub, kind, company=0, sites=(), travel=False, ttl=7200):
    claims = {"sub": sub, "kind": kind}
    if company:
        claims["company"] = company
    if sites:
        claims["sites"] = list(sites)
    if travel:
        claims["travel"] = True
    claims |= {"aud": "polaris-tracker", "exp": int(time.time()) + ttl}
    header, payload = b64(dumps({"alg": "HS256", "typ": "JWT"})), b64(dumps(claims))
    signature = hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256)
    return f"{header}.{payload}.{b64(signature.digest())}"


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def percentile(sorted_values, p):
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


class Target:
    def __init__(self, url):
        parsed = urlparse(url)
        self.host, self.port = parsed.hostname, parsed.port or 80

    def connect(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=10)


def request(conn, method, path, token, body=None):
    headers = {"authorization": f"Bearer {token}", "content-type": "application/json"}
    conn.request(method, path, body=None if body is None else dumps(body), headers=headers)
    response = conn.getresponse()
    return response.status, response.read()


def configure(target, secret, sites, gateways_per_site, badges_per_site):
    service = mint(secret, "service:django", "service")
    conn = target.connect()
    plan = []
    for site in range(1, sites + 1):
        gateways = [f"GW-{site}-{g}" for g in range(1, gateways_per_site + 1)]
        badges = [f"BDG-{site}-{n}" for n in range(1, badges_per_site + 1)]
        config = {
            "tracking_enabled": True,
            "geofence": {"lat": 6.13 + site / 100, "lng": 1.22, "radius_m": 150},
            "zones": [{"id": f"Z{g}", "name": f"Zone {g}"} for g in range(1, gateways_per_site + 1)],
            "gateways": [{"id": gw, "zone_id": f"Z{i + 1}"} for i, gw in enumerate(gateways)],
            "badges": [{"badge_id": b, "worker_id": site * 1000 + i + 1} for i, b in enumerate(badges)],
            "rules": {"sightings_to_enter": 2, "arrival_window_seconds": 300,
                      "absence_to_leave_seconds": 900},
        }
        status, body = request(conn, "PUT", f"/internal/sites/{site}/config", service, config)
        if status != 204:
            raise SystemExit(f"config of site {site} failed: {status} {body[:200]!r}")
        # Each badge has a home gateway (zone), so workers do not flap between zones.
        for g, gw in enumerate(gateways):
            token = mint(secret, f"gateway:{gw}", "gateway", company=1, sites=[site])
            plan.append((site, gw, token, badges[g::gateways_per_site]))
    conn.close()
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:4100")
    parser.add_argument("--secret", default=DEV_SECRET)
    parser.add_argument("--rate", type=float, default=500, help="sightings per second")
    parser.add_argument("--duration", type=float, default=60, help="seconds")
    parser.add_argument("--batch", type=int, default=1, help="sightings per request")
    parser.add_argument("--sites", type=int, default=10)
    parser.add_argument("--gateways-per-site", type=int, default=2)
    parser.add_argument("--badges-per-site", type=int, default=50)
    parser.add_argument("--workers", type=int, default=32, help="client threads")
    parser.add_argument("--db", help="the tracker's TRACKER_DB_PATH: also report how far "
                        "the track store lagged behind ingest")
    args = parser.parse_args()

    target = Target(args.url)
    secret = args.secret.encode()
    plan = configure(target, secret, args.sites, args.gateways_per_site, args.badges_per_site)

    request_rate = args.rate / args.batch
    total = int(request_rate * args.duration)
    interval = 1 / request_rate
    lock = threading.Lock()
    next_index = [0]
    service_ms, queued_ms = [], []
    errors, accepted, rejected, duplicates = {}, [0], [0], [0]
    run = f"{int(time.time() * 1000):x}"
    start = time.perf_counter() + 0.5

    def worker():
        conn = target.connect()
        while True:
            with lock:
                i = next_index[0]
                next_index[0] += 1
            if i >= total:
                break
            due = start + i * interval
            delay = due - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            site, gw, token, badges = plan[i % len(plan)]
            stamp = now_rfc3339()
            body = {
                "batch_id": f"{run}-{i}",
                "sightings": [
                    {"badge_id": badges[(i // len(plan) * args.batch + k) % len(badges)],
                     "at": stamp, "rssi": -60 - k % 20}
                    for k in range(args.batch)
                ],
            }
            sent = time.perf_counter()
            try:
                status, raw = request(conn, "POST", "/v1/gateways/sightings", token, body)
                done = time.perf_counter()
                if status == 200:
                    result = json.loads(raw)
                    with lock:
                        accepted[0] += result["accepted"]
                        duplicates[0] += result["duplicates"]
                        rejected[0] += len(result["rejected"])
                else:
                    key = f"HTTP {status}"
                    with lock:
                        errors[key] = errors.get(key, 0) + 1
            except (OSError, http.client.HTTPException) as error:
                done = time.perf_counter()
                key = type(error).__name__
                with lock:
                    errors[key] = errors.get(key, 0) + 1
                conn.close()
                conn = target.connect()
            with lock:
                service_ms.append((done - sent) * 1000)
                queued_ms.append((done - due) * 1000)
        conn.close()

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - start

    if args.db:
        # Points reach SQLite asynchronously (hub -> track store): wait until the
        # row count stops moving.
        db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
        rows, settled = None, time.perf_counter()
        while True:
            count = db.execute("SELECT count(*) FROM track_points").fetchone()[0]
            if count == rows:
                break
            rows, settled = count, time.perf_counter()
            time.sleep(0.5)
        lag = max(0.0, settled - start - elapsed)
        print(f"store       {rows} rows in the file; caught up {lag:.1f} s after the last "
              f"request")
        db.close()

    conn = target.connect()
    conn.request("GET", "/health")
    health = conn.getresponse().read().decode()
    conn.close()

    service_ms.sort()
    queued_ms.sort()
    sent_sightings = len(service_ms) * args.batch
    print(f"target      {args.rate:.0f} sightings/s for {args.duration:.0f} s, "
          f"{args.batch} per request ({request_rate:.0f} req/s), {args.sites} sites, "
          f"{len(plan)} gateways, {args.workers} client threads")
    print(f"sent        {len(service_ms)} requests, {sent_sightings} sightings in {elapsed:.1f} s "
          f"= {sent_sightings / elapsed:.0f} sightings/s")
    print(f"accepted    {accepted[0]} sightings, duplicates {duplicates[0]}, "
          f"rejected {rejected[0]}")
    print(f"errors      {sum(errors.values())} {errors if errors else ''}")
    for name, values in (("service", service_ms), ("queued", queued_ms)):
        print(f"{name:<11} p50 {percentile(values, 50):.2f} ms  p95 {percentile(values, 95):.2f} ms  "
              f"p99 {percentile(values, 99):.2f} ms  max {values[-1]:.2f} ms  "
              f"mean {statistics.fmean(values):.2f} ms")
    print(f"health      {health}")


if __name__ == "__main__":
    main()
