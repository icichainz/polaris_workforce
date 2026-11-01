//// Raw track points per person, kept for the retention period.
////
//// Prototype: in memory. Production keeps them in the tracker's own SQLite
//// file (never Django's), see `docs/presence-tracking.md` §9.

import gleam/dict.{type Dict}
import gleam/erlang/process.{type Name, type Subject}
import gleam/int
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import polaris_tracker/domain.{type Person, type TrackPoint}

pub type Message {
  Record(person: Person, points: List(TrackPoint))
  /// Points with `from <= at < to`, oldest first.
  Query(person: Person, from: Int, to: Int, reply: Subject(List(TrackPoint)))
  Prune
}

pub type Args {
  Args(retention_ms: Int, prune_every_ms: Int, clock: fn() -> Int)
}

type State {
  State(
    args: Args,
    /// Newest first; out-of-order points are sorted when queried.
    tracks: Dict(Person, List(TrackPoint)),
    self: Subject(Message),
  )
}

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(1000, fn(self) {
      schedule_prune(self, args.prune_every_ms)
      State(args:, tracks: dict.new(), self:)
      |> actor.initialised
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
  case message {
    Record(person:, points:) -> {
      let tracks =
        dict.upsert(state.tracks, person, fn(existing) {
          case existing {
            Some(existing) -> list.append(list.reverse(points), existing)
            None -> list.reverse(points)
          }
        })
      actor.continue(State(..state, tracks:))
    }

    Query(person:, from:, to:, reply:) -> {
      case dict.get(state.tracks, person) {
        Ok(points) -> points
        Error(Nil) -> []
      }
      |> list.filter(fn(point) {
        let at = domain.track_point_time(point)
        at >= from && at < to
      })
      |> list.sort(fn(a, b) {
        int.compare(domain.track_point_time(a), domain.track_point_time(b))
      })
      |> process.send(reply, _)
      actor.continue(state)
    }

    Prune -> {
      schedule_prune(state.self, state.args.prune_every_ms)
      let oldest = state.args.clock() - state.args.retention_ms
      let tracks =
        state.tracks
        |> dict.map_values(fn(_, points) {
          list.filter(points, fn(point) {
            domain.track_point_time(point) >= oldest
          })
        })
        |> dict.filter(fn(_, points) { points != [] })
      actor.continue(State(..state, tracks:))
    }
  }
}

fn schedule_prune(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Prune)
      Nil
    }
    False -> Nil
  }
}
