//// The hub routes observations to site actors.
////
//// It holds every site's config (pushed by Django), starts a site actor the
//// first time a site sees activity, drops repeated `client_uuid`s and gateway
//// batches (rule 7), records every accepted point in the track store, and
//// decides which GPS fixes count as being on a site.

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
import polaris_tracker/site
import polaris_tracker/token

/// Observations dated further in the future than this are rejected.
const max_clock_skew_ms = 300_000

/// Remembered `client_uuid`s and batch ids are kept for this long.
const dedupe_window_ms = 172_800_000

/// The dedupe table is pruned once it holds this many entries.
const dedupe_prune_at = 200_000

pub type Message {
  ConfigureSite(site_id: Int, config: SiteConfig, reply: Subject(Nil))
  IngestPhone(
    foreman_id: Int,
    claims: token.Claims,
    observations: List(Observation),
    reply: Subject(IngestResult),
  )
  IngestGateway(
    gateway_id: String,
    site_id: Int,
    batch_id: String,
    sightings: List(GatewaySighting),
    reply: Subject(Result(IngestResult, GatewayError)),
  )
  FindSite(site_id: Int, reply: Subject(SiteStatus))
  CountSites(reply: Subject(Int))
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
    start_site: fn(site.Args) -> actor.StartResult(Subject(site.Message)),
    record_track: fn(Person, List(TrackPoint)) -> Nil,
    publish: fn(List(PresenceEvent)) -> Nil,
    clock: fn() -> Int,
    heartbeat_ms: Int,
  )
}

type State {
  State(
    args: Args,
    configs: Dict(Int, SiteConfig),
    running: Dict(Int, RunningSite),
    seen: Dict(String, Int),
  )
}

type RunningSite {
  RunningSite(subject: Subject(site.Message), pid: Pid)
}

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(1000, fn(self) {
      let selector =
        process.new_selector()
        |> process.select(self)
        |> process.select_monitors(SiteDown)
      State(args:, configs: dict.new(), running: dict.new(), seen: dict.new())
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

fn handle_message(state: State, message: Message) -> actor.Next(State, Message) {
  case message {
    ConfigureSite(site_id:, config:, reply:) -> {
      case dict.get(state.running, site_id) {
        Ok(running) -> process.send(running.subject, site.Configure(config))
        Error(Nil) -> Nil
      }
      process.send(reply, Nil)
      actor.continue(
        State(..state, configs: dict.insert(state.configs, site_id, config)),
      )
    }

    IngestPhone(foreman_id:, claims:, observations:, reply:) -> {
      let now = state.args.clock()
      let #(state, tally) =
        list.fold(observations, #(state, empty_tally()), fn(acc, observation) {
          let #(state, tally) = acc
          ingest_phone(state, foreman_id, claims, observation, now, tally)
        })
      process.send(reply, finish(tally))
      actor.continue(state)
    }

    IngestGateway(gateway_id:, site_id:, batch_id:, sightings:, reply:) -> {
      let #(state, result) =
        ingest_gateway(state, gateway_id, site_id, batch_id, sightings)
      process.send(reply, result)
      actor.continue(state)
    }

    FindSite(site_id:, reply:) -> {
      let status = case
        dict.get(state.running, site_id),
        dict.has_key(state.configs, site_id)
      {
        Ok(running), _ -> Running(running.subject)
        Error(Nil), True -> Idle
        Error(Nil), False -> NotConfigured
      }
      process.send(reply, status)
      actor.continue(state)
    }

    CountSites(reply:) -> {
      process.send(reply, dict.size(state.configs))
      actor.continue(state)
    }

    SiteDown(process.ProcessDown(pid:, ..)) -> {
      let running = dict.filter(state.running, fn(_, site) { site.pid != pid })
      actor.continue(State(..state, running:))
    }
    SiteDown(process.PortDown(..)) -> actor.continue(state)
  }
}

// ---- Phones -----------------------------------------------------------------

fn ingest_phone(
  state: State,
  foreman_id: Int,
  claims: token.Claims,
  observation: Observation,
  now: Int,
  tally: Tally,
) -> #(State, Tally) {
  let ref = domain.observation_ref(observation)
  let rejected = check_phone_observation(state, claims, observation, now)
  case rejected {
    Some(#(code, message)) -> #(state, reject(tally, ref, code, message))
    None -> {
      let key = "foreman:" <> int.to_string(foreman_id) <> ":" <> ref
      case remember(state, key, now) {
        #(state, False) -> #(
          state,
          Tally(..tally, duplicates: tally.duplicates + 1),
        )
        #(state, True) -> {
          let #(state, late) = case observation {
            GpsFix(..) ->
              ingest_gps(state, foreman_id, claims, observation, now)
            PhoneBle(..) -> ingest_phone_ble(state, observation, now)
          }
          #(state, accept(tally, late))
        }
      }
    }
  }
}

/// Returns the rejection code and message, if any.
fn check_phone_observation(
  state: State,
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
        dict.get(state.configs, site_id)
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

/// Stores the fix and feeds the sites it says something about. Returns
/// whether it was too old to change live presence.
fn ingest_gps(
  state: State,
  foreman_id: Int,
  claims: token.Claims,
  observation: Observation,
  now: Int,
) -> #(State, Bool) {
  let assert GpsFix(at:, position:, accuracy_m:, ..) = observation
  let person = Person(Foreman, foreman_id)
  let placements =
    tracked_sites(state, claims)
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
  state.args.record_track(person, [
    GpsPoint(
      at:,
      position:,
      accuracy_m:,
      site_id: case inside {
        Ok(#(site_id, _)) -> Some(site_id)
        Error(Nil) -> None
      },
      company: claims.company,
    ),
  ])

  // A fix outside a site only matters to a site that is already running.
  list.each(placements, fn(placement) {
    case placement, dict.get(state.running, placement.0) {
      #(_, _, geo.Outside), Ok(running) ->
        process.send(
          running.subject,
          site.Observe(person, presence.SeenOutside(at)),
        )
      _, _ -> Nil
    }
  })

  case inside {
    Ok(#(site_id, config)) ->
      case is_late(config, at, now) {
        True -> #(state, True)
        False -> #(
          observe(state, site_id, person, presence.Seen(zone: None, at:)),
          False,
        )
      }
    Error(Nil) -> #(state, False)
  }
}

fn ingest_phone_ble(
  state: State,
  observation: Observation,
  now: Int,
) -> #(State, Bool) {
  let assert PhoneBle(at:, site_id:, badge_id:, rssi:, ..) = observation
  // Checked in `check_phone_observation`.
  let assert Ok(config) = dict.get(state.configs, site_id)
  let assert Ok(worker_id) = domain.find_worker(config, badge_id)
  let person = Person(Worker, worker_id)
  state.args.record_track(person, [
    BlePoint(at:, site_id:, zone_id: None, rssi:, source: FromPhone),
  ])
  case is_late(config, at, now) {
    True -> #(state, True)
    False -> #(
      observe(state, site_id, person, presence.Seen(zone: None, at:)),
      False,
    )
  }
}

/// The sites a device token may report on, with tracking switched on.
fn tracked_sites(state: State, claims: token.Claims) -> List(#(Int, SiteConfig)) {
  state.configs
  |> dict.to_list
  |> list.filter(fn(entry) {
    let #(site_id, config) = entry
    config.tracking_enabled && token.can_access_site(claims, site_id)
  })
}

// ---- Gateways ---------------------------------------------------------------

fn ingest_gateway(
  state: State,
  gateway_id: String,
  site_id: Int,
  batch_id: String,
  sightings: List(GatewaySighting),
) -> #(State, Result(IngestResult, GatewayError)) {
  let now = state.args.clock()
  case dict.get(state.configs, site_id) {
    Error(Nil) -> #(state, Error(UnknownSite))
    Ok(config) ->
      case domain.find_gateway(config, gateway_id) {
        Error(Nil) -> #(state, Error(UnknownGateway))
        Ok(gateway) -> {
          let key = "gateway:" <> gateway_id <> ":" <> batch_id
          case remember(state, key, now) {
            #(state, False) -> #(
              state,
              Ok(
                IngestResult(
                  accepted: 0,
                  duplicates: list.length(sightings),
                  late: 0,
                  rejected: [],
                ),
              ),
            )
            #(state, True) -> {
              let #(state, tally) =
                list.fold(sightings, #(state, empty_tally()), fn(acc, sighting) {
                  let #(state, tally) = acc
                  ingest_sighting(
                    state,
                    site_id,
                    config,
                    gateway,
                    sighting,
                    now,
                    tally,
                  )
                })
              #(state, Ok(finish(tally)))
            }
          }
        }
      }
  }
}

fn ingest_sighting(
  state: State,
  site_id: Int,
  config: SiteConfig,
  gateway: domain.Gateway,
  sighting: GatewaySighting,
  now: Int,
  tally: Tally,
) -> #(State, Tally) {
  let ref = sighting.badge_id
  case
    sighting.at > now + max_clock_skew_ms,
    config.tracking_enabled,
    domain.find_worker(config, sighting.badge_id)
  {
    True, _, _ -> #(
      state,
      reject(
        tally,
        ref,
        "future_timestamp",
        "The sighting is dated in the future.",
      ),
    )
    False, False, _ -> #(
      state,
      reject(tally, ref, "tracking_disabled", "Tracking is off for this site."),
    )
    False, True, Error(Nil) -> #(
      state,
      reject(
        tally,
        ref,
        "unknown_badge",
        "The badge is not assigned on this site.",
      ),
    )
    False, True, Ok(worker_id) -> {
      let person = Person(Worker, worker_id)
      state.args.record_track(person, [
        BlePoint(
          at: sighting.at,
          site_id:,
          zone_id: gateway.zone_id,
          rssi: sighting.rssi,
          source: FromGateway,
        ),
      ])
      case is_late(config, sighting.at, now) {
        True -> #(state, accept(tally, True))
        False -> #(
          observe(
            state,
            site_id,
            person,
            presence.Seen(zone: gateway.zone_id, at: sighting.at),
          ),
          accept(tally, False),
        )
      }
    }
  }
}

// ---- Helpers ----------------------------------------------------------------

/// Sends a signal to a site actor, starting it if needed.
fn observe(
  state: State,
  site_id: Int,
  person: Person,
  signal: presence.Signal,
) -> State {
  let #(state, subject) = ensure_running(state, site_id)
  case subject {
    Some(subject) -> process.send(subject, site.Observe(person, signal))
    // The site actor could not start; the point is still in the track.
    None -> Nil
  }
  state
}

fn ensure_running(
  state: State,
  site_id: Int,
) -> #(State, Option(Subject(site.Message))) {
  case dict.get(state.running, site_id), dict.get(state.configs, site_id) {
    Ok(running), _ -> #(state, Some(running.subject))
    Error(Nil), Error(Nil) -> #(state, None)
    Error(Nil), Ok(config) -> {
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
          let running =
            dict.insert(
              state.running,
              site_id,
              RunningSite(subject: started.data, pid: started.pid),
            )
          #(State(..state, running:), Some(started.data))
        }
        Error(_) -> #(state, None)
      }
    }
  }
}

/// Late data would reopen visits that are already closed, so it only goes to
/// the track; the nightly replay corrects sessions from there.
fn is_late(config: SiteConfig, at: Int, now: Int) -> Bool {
  now - at > config.rules.absence_to_leave_ms
}

/// Records a dedupe key. Returns `False` when it was already known.
fn remember(state: State, key: String, now: Int) -> #(State, Bool) {
  case dict.has_key(state.seen, key) {
    True -> #(state, False)
    False -> {
      let seen = case dict.size(state.seen) >= dedupe_prune_at {
        True ->
          dict.filter(state.seen, fn(_, at) { now - at < dedupe_window_ms })
        False -> state.seen
      }
      #(State(..state, seen: dict.insert(seen, key, now)), True)
    }
  }
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
