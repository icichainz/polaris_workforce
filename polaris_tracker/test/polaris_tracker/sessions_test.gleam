import gleam/option.{None, Some}
import polaris_tracker/domain.{
  BlePoint, Foreman, FromGateway, FromPhone, GpsPoint, Person, SiteConfig,
  Worker,
}
import polaris_tracker/geo
import polaris_tracker/presence
import polaris_tracker/sessions.{Session}

const minute = 60_000

const t0 = 1_800_000_000_000

fn config() -> domain.SiteConfig {
  SiteConfig(
    tracking_enabled: True,
    geofence: Some(geo.Geofence(geo.LatLng(6.1319, 1.2228), 150.0)),
    zones: [],
    gateways: [],
    badges: [],
    rules: presence.Rules(
      sightings_to_enter: 2,
      arrival_window_ms: 5 * minute,
      absence_to_leave_ms: 15 * minute,
    ),
  )
}

fn gateway(at: Int, zone: String) -> domain.TrackPoint {
  BlePoint(at:, site_id: 3, zone_id: Some(zone), rssi: -60, source: FromGateway)
}

fn phone(at: Int) -> domain.TrackPoint {
  BlePoint(at:, site_id: 3, zone_id: None, rssi: -70, source: FromPhone)
}

fn gps(at: Int, lat: Float) -> domain.TrackPoint {
  gps_on(at, lat, None)
}

fn gps_on(
  at: Int,
  lat: Float,
  site_id: option.Option(Int),
) -> domain.TrackPoint {
  GpsPoint(
    at:,
    position: geo.LatLng(lat, 1.2228),
    accuracy_m: 10.0,
    site_id:,
    company: 1,
  )
}

pub fn visits_zones_and_gaps_test() {
  let worker = Person(Worker, 12)
  let foreman = Person(Foreman, 5)
  let points = [
    #(worker, gateway(t0, "A")),
    #(worker, gateway(t0 + minute, "A")),
    #(foreman, gps(t0 + minute, 6.132)),
    #(foreman, gps(t0 + 2 * minute, 6.1321)),
    #(worker, gateway(t0 + 3 * minute, "B")),
    #(worker, gateway(t0 + 4 * minute, "A")),
    // About 2 km away: the foreman left the geofence.
    #(foreman, gps(t0 + 10 * minute, 6.15)),
    // An hour of silence, then the worker is back.
    #(worker, gateway(t0 + 70 * minute, "B")),
    #(worker, gateway(t0 + 71 * minute, "B")),
    // A sighting on another site counts for nothing here.
    #(
      worker,
      BlePoint(
        at: t0 + 72 * minute,
        site_id: 4,
        zone_id: Some("A"),
        rssi: -60,
        source: FromGateway,
      ),
    ),
    // A single sighting is not a visit.
    #(Person(Worker, 13), gateway(t0, "A")),
    // Phone BLE has no zone.
    #(Person(Worker, 14), phone(t0)),
    #(Person(Worker, 14), phone(t0 + minute)),
  ]
  let found =
    sessions.recompute(
      points,
      site_id: 3,
      config: config(),
      until: t0 + 80 * minute,
    )
  assert found
    == [
      Session(worker, t0, Some(t0 + 4 * minute), ["A", "B"]),
      Session(Person(Worker, 14), t0, Some(t0 + minute), []),
      Session(foreman, t0 + minute, Some(t0 + 2 * minute), []),
      // Still within the absence window at `until`: open.
      Session(worker, t0 + 70 * minute, None, ["B"]),
    ]
}

pub fn without_a_geofence_gps_uses_the_stored_site_test() {
  let foreman = Person(Foreman, 5)
  let on_site = fn(at) { gps_on(at, 9.0, Some(3)) }
  let found =
    sessions.recompute(
      [
        #(foreman, on_site(t0)),
        #(foreman, on_site(t0 + minute)),
        #(foreman, gps(t0 + 2 * minute, 9.0)),
      ],
      site_id: 3,
      config: SiteConfig(..config(), geofence: None),
      until: t0 + 60 * minute,
    )
  assert found == [Session(foreman, t0, Some(t0 + minute), [])]
}
