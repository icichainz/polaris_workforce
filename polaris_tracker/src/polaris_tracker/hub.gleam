//// Ingest: from validated observations to track points and site actors.
////
//// Ingest runs in the caller's process (each HTTP request has its own), not
//// in one shared actor: site configs, running site actors and dedupe keys
//// are in the shared `registry` (ETS). Each batch drops repeated
//// `client_uuid`s and gateway batch ids (rule 7), decides which GPS fixes
//// count as being on a site, hands its points to the track store in one
//// message, and sends the presence signals straight to the site actors.
////
//// The hub actor itself only does what must be serialized: starting a site's
//// actor the first time the site sees activity (so there is never two),
//// noticing when one stops, and pruning old dedupe keys.

import gleam/dict.{type Dict}
import gleam/erlang/process.{type Name, type Pid, type Subject}
import gleam/int
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import polaris_tracker/domain.{
  type GatewaySighting, type IngestResult, type Observation, type Person,
  type PresenceEvent, type SiteConfig, type TrackPoint, BlePoint, Foreman,
  FromGateway, FromPhone, GpsFix, GpsPoint, IngestResult, Person, PhoneBle,
  Rejection, Worker,
}
import polaris_tracker/geo
import polaris_tracker/presence
import polaris_tracker/registry.{type Registry}
import polaris_tracker/site
import polaris_tracker/token

/// Observations dated further in the future than this are rejected.
const max_clock_skew_ms = 300_000

/// Remembered `client_uuid`s and batch ids are kept at least this long.
pub const dedupe_window_ms = 172_800_000

const call_timeout_ms = 5000

pub type Message {
  /// Starts the site's actor unless it is running, and replies with it.
  EnsureRunning(
    site_id: Int,
    reply: Subject(Result(Subject(site.Message), Nil)),
  )
  /// Forgets dedupe keys older than the window.
  PruneSeen
  SiteDown(process.Down)
}

pub type SiteStatus {
  NotConfigured
  /// Configured, but nobody has been seen since the tracker started.
  Idle
  Running(site: Subject(site.Message))
}

pub type GatewayError {
  UnknownSite
  UnknownGateway
}

pub type Args {
  Args(
    registry: Registry,
    start_site: fn(site.Args) -> actor.StartResult(Subject(site.Message)),
    publish: fn(List(PresenceEvent)) -> Nil,
    clock: fn() -> Int,
    heartbeat_ms: Int,
    /// How often old dedupe keys are pruned; 0 disables the timer.
    prune_every_ms: Int,
  )
}

/// What the HTTP handlers hold: ingest runs in their own process.
pub type Hub {
  Hub(
    subject: Subject(Message),
    registry: Registry,
    /// Called once per ingested batch with every point it accepted.
    record_track: fn(List(#(Person, TrackPoint))) -> Nil,
    clock: fn() -> Int,
  )
}

type State {
  State(args: Args, monitored: Dict(Pid, Int), self: Subject(Message))
}

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(1000, fn(self) {
      // Site actors from before a restart are gone (they restart with us).
      registry.clear_running(args.registry)
      schedule_prune(self, args.prune_every_ms)
      let selector =
        process.new_selector()
        |> process.select(self)
        |> process.select_monitors(SiteDown)
      State(args:, monitored: dict.new(), self:)
      |> actor.initialised
      |> actor.selecting(selector)
      |> actor.returning(self)
      |> Ok
    })
    |> actor.on_message(handle_message)
  case name {
    Some(name) -> actor.named(builder, name)
    None -> builder
  }
  |> actor.start
}

fn handle_message(
  state: State,
  message: Message,
) -> actor.Next(State, Message) {
  let registry = state.args.registry
  case message {
    EnsureRunning(site_id:, reply:) ->
      case registry.get_running(registry, site_id) {
        Ok(running) -> {
          process.send(reply, Ok(running))
          actor.continue(state)
        }
        Error(Nil) -> {
          let #(state, started) = start_site(state, site_id)
          process.send(reply, started)
          actor.continue(state)
        }
      }

    PruneSeen -> {
      schedule_prune(state.self, state.args.prune_every_ms)
      let _ =
        registry.prune_seen(registry, state.args.clock() - dedupe_window_ms)
      actor.continue(state)
    }

    SiteDown(process.ProcessDown(pid:, ..)) ->
      case dict.get(state.monitored, pid) {
        Ok(site_id) -> {
          registry.remove_running(registry, site_id)
          actor.continue(
            State(..state, monitored: dict.delete(state.monitored, pid)),
          )
        }
        Error(Nil) -> actor.continue(state)
      }
    SiteDown(process.PortDown(..)) -> actor.continue(state)
  }
}

fn start_site(
  state: State,
  site_id: Int,
) -> #(State, Result(Subject(site.Message), Nil)) {
  case registry.get_config(state.args.registry, site_id) {
    Error(Nil) -> #(state, Error(Nil))
    Ok(config) -> {
      let args =
        site.Args(
          site_id:,
          config:,
          publish: state.args.publish,
          clock: state.args.clock,
          heartbeat_ms: state.args.heartbeat_ms,
        )
      case state.args.start_site(args) {
        Ok(started) -> {
          let _monitor = process.monitor(started.pid)
          registry.put_running(state.args.registry, site_id, started.data)
          let monitored = dict.insert(state.monitored, started.pid, site_id)
          #(State(..state, monitored:), Ok(started.data))
        }
        Error(_) -> #(state, Error(Nil))
      }
    }
  }
}

fn schedule_prune(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, PruneSeen)
      Nil
    }
    False -> Nil
  }
}

// ---- Sites ------------------------------------------------------------------

/// Stores a site's config; a running site actor uses it at once.
pub fn configure_site(hub: Hub, site_id: Int, config: SiteConfig) -> Nil {
  registry.put_config(hub.registry, site_id, config)
  case registry.get_running(hub.registry, site_id) {
    Ok(running) -> process.send(running, site.Configure(config))
    Error(Nil) -> Nil
  }
}

pub fn find_site(hub: Hub, site_id: Int) -> SiteStatus {
  case
    registry.get_running(hub.registry, site_id),
    registry.get_config(hub.registry, site_id)
  {
    Ok(running), _ -> Running(running)
    Error(Nil), Ok(_) -> Idle
    Error(Nil), Error(Nil) -> NotConfigured
  }
}

/// The site's current config, as last pushed by Django.
pub fn get_config(hub: Hub, site_id: Int) -> Result(SiteConfig, Nil) {
  registry.get_config(hub.registry, site_id)
}

pub fn count_sites(hub: Hub) -> Int {
  registry.count_configs(hub.registry)
}

// ---- Phones -----------------------------------------------------------------

/// A batch from a foreman's phone, ingested in the caller's process.
pub fn ingest_phone(
  hub: Hub,
  foreman_id: Int,
  claims: token.Claims,
  observations: List(Observation),
) -> IngestResult {
  let now = hub.clock()
  let #(tally, points) =
    list.fold(observations, #(empty_tally(), []), fn(acc, observation) {
      let #(tally, points) = acc
      ingest_phone_item(
        hub,
        foreman_id,
        claims,
        observation,
        now,
        tally,
        points,
      )
    })
  record(hub, points)
  finish(tally)
}

fn ingest_phone_item(
  hub: Hub,
  foreman_id: Int,
  claims: token.Claims,
  observation: Observation,
  now: Int,
  tally: Tally,
  points: Points,
) -> #(Tally, Points) {
  let ref = domain.observation_ref(observation)
  case check_phone_observation(hub, claims, observation, now) {
    Some(#(code, message)) -> #(reject(tally, ref, code, message), points)
    None -> {
      let key = "foreman:" <> int.to_string(foreman_id) <> ":" <> ref
      case registry.remember(hub.registry, key, now) {
        False -> #(Tally(..tally, duplicates: tally.duplicates + 1), points)
        True -> {
          let #(point, late) = case observation {
            GpsFix(..) -> ingest_gps(hub, foreman_id, claims, observation, now)
            PhoneBle(..) -> ingest_phone_ble(hub, observation, now)
          }
          #(accept(tally, late), [point, ..points])
        }
      }
    }
  }
}

/// Returns the rejection code and message, if any.
fn check_phone_observation(
  hub: Hub,
  claims: token.Claims,
  observation: Observation,
  now: Int,
) -> Option(#(String, String)) {
  case observation {
    _ if observation.at > now + max_clock_skew_ms ->
      Some(#("future_timestamp", "The observation is dated in the future."))
    GpsFix(..) -> None
    PhoneBle(site_id:, badge_id:, ..) ->
      case
        token.can_access_site(claims, site_id),
        registry.get_config(hub.registry, site_id)
      {
        False, _ ->
          Some(#("site_not_allowed", "This device is not assigned to the site."))
        True, Error(Nil) ->
          Some(#("site_not_configured", "The site has no tracking config."))
        True, Ok(config) if !config.tracking_enabled ->
          Some(#("tracking_disabled", "Tracking is off for this site."))
        True, Ok(config) ->
          case domain.find_worker(config, badge_id) {
            Ok(_) -> None
            Error(Nil) ->
              Some(#("unknown_badge", "The badge is not assigned on this site."))
          }
      }
  }
}

/// Feeds the sites the fix says something about. Returns the point to store
/// and whether it was too old to change live presence.
fn ingest_gps(
  hub: Hub,
  foreman_id: Int,
  claims: token.Claims,
  observation: Observation,
  now: Int,
) -> #(#(Person, TrackPoint), Bool) {
  let assert GpsFix(at:, position:, accuracy_m:, ..) = observation
  let person = Person(Foreman, foreman_id)
  let placements =
    tracked_sites(hub, claims)
    |> list.filter_map(fn(entry) {
      let #(site_id, config) = entry
      case config.geofence {
        Some(fence) ->
          Ok(#(site_id, config, geo.classify(fence, position, accuracy_m:)))
        None -> Error(Nil)
      }
    })
  let inside =
    list.find_map(placements, fn(placement) {
      case placement {
        #(site_id, config, geo.Inside) -> Ok(#(site_id, config))
        _ -> Error(Nil)
      }
    })
  let point =
    GpsPoint(
      at:,
      position:,
      accuracy_m:,
      site_id: case inside {
        Ok(#(site_id, _)) -> Some(site_id)
        Error(Nil) -> None
      },
      company: claims.company,
    )

  // A fix outside a site only matters to a site that is already running.
  list.each(placements, fn(placement) {
    case placement, registry.get_running(hub.registry, placement.0) {
      #(_, _, geo.Outside), Ok(running) ->
        process.send(running, site.Observe(person, presence.SeenOutside(at)))
      _, _ -> Nil
    }
  })

  let late = case inside {
    Ok(#(site_id, config)) ->
      case is_late(config, at, now) {
        True -> True
        False -> {
          observe(hub, site_id, person, presence.Seen(zone: None, at:))
          False
        }
      }
    Error(Nil) -> False
  }
  #(#(person, point), late)
}

fn ingest_phone_ble(
  hub: Hub,
  observation: Observation,
  now: Int,
) -> #(#(Person, TrackPoint), Bool) {
  let assert PhoneBle(at:, site_id:, badge_id:, rssi:, ..) = observation
  // Checked in `check_phone_observation`.
  let assert Ok(config) = registry.get_config(hub.registry, site_id)
  let assert Ok(worker_id) = domain.find_worker(config, badge_id)
  let person = Person(Worker, worker_id)
  let point = BlePoint(at:, site_id:, zone_id: None, rssi:, source: FromPhone)
  let late = case is_late(config, at, now) {
    True -> True
    False -> {
      observe(hub, site_id, person, presence.Seen(zone: None, at:))
      False
    }
  }
  #(#(person, point), late)
}

/// The sites in the device token, with tracking switched on.
fn tracked_sites(hub: Hub, claims: token.Claims) -> List(#(Int, SiteConfig)) {
  claims.sites
  |> list.unique
  |> list.filter_map(fn(site_id) {
    case registry.get_config(hub.registry, site_id) {
      Ok(config) if config.tracking_enabled -> Ok(#(site_id, config))
      _ -> Error(Nil)
    }
  })
}

// ---- Gateways ---------------------------------------------------------------

/// A batch from a fixed gateway, ingested in the caller's process.
pub fn ingest_gateway(
  hub: Hub,
  gateway_id: String,
  site_id: Int,
  batch_id: String,
  sightings: List(GatewaySighting),
) -> Result(IngestResult, GatewayError) {
  let now = hub.clock()
  case registry.get_config(hub.registry, site_id) {
    Error(Nil) -> Error(UnknownSite)
    Ok(config) ->
      case domain.find_gateway(config, gateway_id) {
        Error(Nil) -> Error(UnknownGateway)
        Ok(gateway) -> {
          let key = "gateway:" <> gateway_id <> ":" <> batch_id
          case registry.remember(hub.registry, key, now) {
            False ->
              Ok(
                IngestResult(
                  accepted: 0,
                  duplicates: list.length(sightings),
                  late: 0,
                  rejected: [],
                ),
              )
            True -> {
              let #(tally, points) =
                list.fold(sightings, #(empty_tally(), []), fn(acc, sighting) {
                  let #(tally, points) = acc
                  ingest_sighting(
                    hub,
                    site_id,
                    config,
                    gateway,
                    sighting,
                    now,
                    tally,
                    points,
                  )
                })
              record(hub, points)
              Ok(finish(tally))
            }
          }
        }
      }
  }
}

fn ingest_sighting(
  hub: Hub,
  site_id: Int,
  config: SiteConfig,
  gateway: domain.Gateway,
  sighting: GatewaySighting,
  now: Int,
  tally: Tally,
  points: Points,
) -> #(Tally, Points) {
  let ref = sighting.badge_id
  case
    sighting.at > now + max_clock_skew_ms,
    config.tracking_enabled,
    domain.find_worker(config, sighting.badge_id)
  {
    True, _, _ -> #(
      reject(
        tally,
        ref,
        "future_timestamp",
        "The sighting is dated in the future.",
      ),
      points,
    )
    False, False, _ -> #(
      reject(tally, ref, "tracking_disabled", "Tracking is off for this site."),
      points,
    )
    False, True, Error(Nil) -> #(
      reject(
        tally,
        ref,
        "unknown_badge",
        "The badge is not assigned on this site.",
      ),
      points,
    )
    False, True, Ok(worker_id) -> {
      let person = Person(Worker, worker_id)
      let point =
        BlePoint(
          at: sighting.at,
          site_id:,
          zone_id: gateway.zone_id,
          rssi: sighting.rssi,
          source: FromGateway,
        )
      let late = is_late(config, sighting.at, now)
      case late {
        True -> Nil
        False ->
          observe(
            hub,
            site_id,
            person,
            presence.Seen(zone: gateway.zone_id, at: sighting.at),
          )
      }
      #(accept(tally, late), [#(person, point), ..points])
    }
  }
}

// ---- Helpers ----------------------------------------------------------------

/// Points of the batch being ingested, newest first.
type Points =
  List(#(Person, TrackPoint))

/// Hands the batch's points to the track store in one message (one
/// transaction there).
fn record(hub: Hub, points: Points) -> Nil {
  case points {
    [] -> Nil
    _ -> hub.record_track(list.reverse(points))
  }
}

/// Sends a signal to a site actor, asking the hub to start it if needed.
fn observe(
  hub: Hub,
  site_id: Int,
  person: Person,
  signal: presence.Signal,
) -> Nil {
  let running = case registry.get_running(hub.registry, site_id) {
    Ok(running) -> Ok(running)
    Error(Nil) ->
      process.call(hub.subject, call_timeout_ms, EnsureRunning(site_id, _))
  }
  case running {
    Ok(running) -> process.send(running, site.Observe(person, signal))
    // The site actor could not start; the point is still in the track.
    Error(Nil) -> Nil
  }
}

/// Late data would reopen visits that are already closed, so it only goes to
/// the track; the nightly replay corrects sessions from there.
fn is_late(config: SiteConfig, at: Int, now: Int) -> Bool {
  now - at > config.rules.absence_to_leave_ms
}

type Tally {
  Tally(
    accepted: Int,
    duplicates: Int,
    late: Int,
    rejected: List(domain.Rejection),
  )
}

fn empty_tally() -> Tally {
  Tally(accepted: 0, duplicates: 0, late: 0, rejected: [])
}

fn accept(tally: Tally, late: Bool) -> Tally {
  let late = case late {
    True -> tally.late + 1
    False -> tally.late
  }
  Tally(..tally, accepted: tally.accepted + 1, late:)
}

fn reject(tally: Tally, ref: String, code: String, message: String) -> Tally {
  Tally(..tally, rejected: [Rejection(ref:, code:, message:), ..tally.rejected])
}

fn finish(tally: Tally) -> IngestResult {
  IngestResult(
    accepted: tally.accepted,
    duplicates: tally.duplicates,
    late: tally.late,
    rejected: list.reverse(tally.rejected),
  )
}
