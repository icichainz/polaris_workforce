//// One actor per site: the presence state of every person seen there.
////
//// Site actors run under a factory supervisor, so a crash only loses that
//// site's live state; the hub starts a fresh actor on the next observation.

import gleam/dict.{type Dict}
import gleam/erlang/process.{type Subject}
import gleam/list
import gleam/otp/actor
import gleam/result
import gleam/string
import polaris_tracker/domain.{
  type Person, type PresenceEvent, type SiteConfig, PresenceEvent,
}
import polaris_tracker/ids
import polaris_tracker/presence.{type Presence}

pub type Message {
  Observe(person: Person, signal: presence.Signal)
  Configure(config: SiteConfig)
  /// Let time run until `now`, closing visits that went silent.
  AdvanceTo(now: Int)
  /// The actor's own timer; becomes `AdvanceTo(clock())`.
  Heartbeat
  Snapshot(reply: Subject(List(#(Person, Presence))))
}

pub type Args {
  Args(
    site_id: Int,
    config: SiteConfig,
    publish: fn(List(PresenceEvent)) -> Nil,
    clock: fn() -> Int,
    /// 0 disables the timer (tests drive time with `AdvanceTo`).
    heartbeat_ms: Int,
  )
}

type State {
  State(
    args: Args,
    config: SiteConfig,
    people: Dict(Person, Presence),
    self: Subject(Message),
  )
}

pub fn start(args: Args) -> actor.StartResult(Subject(Message)) {
  actor.new_with_initialiser(1000, fn(self) {
    schedule_heartbeat(self, args.heartbeat_ms)
    State(args:, config: args.config, people: dict.new(), self:)
    |> actor.initialised
    |> actor.returning(self)
    |> Ok
  })
  |> actor.on_message(handle_message)
  |> actor.start
}

fn handle_message(state: State, message: Message) -> actor.Next(State, Message) {
  case message {
    Observe(person:, signal:) -> actor.continue(apply(state, person, signal))
    Configure(config:) -> actor.continue(State(..state, config:))
    AdvanceTo(now:) -> actor.continue(advance(state, now))
    Heartbeat -> {
      schedule_heartbeat(state.self, state.args.heartbeat_ms)
      actor.continue(advance(state, state.args.clock()))
    }
    Snapshot(reply:) -> {
      state.people
      |> dict.to_list
      |> list.sort(fn(a, b) {
        string.compare(
          domain.person_to_string(a.0),
          domain.person_to_string(b.0),
        )
      })
      |> process.send(reply, _)
      actor.continue(state)
    }
  }
}

fn apply(state: State, person: Person, signal: presence.Signal) -> State {
  let current = dict.get(state.people, person) |> result.unwrap(presence.Away)
  let #(next, changes) = presence.step(current, signal, state.config.rules)
  publish(state, person, changes)
  let people = case next {
    presence.Away -> dict.delete(state.people, person)
    _ -> dict.insert(state.people, person, next)
  }
  State(..state, people:)
}

fn advance(state: State, now: Int) -> State {
  state.people
  |> dict.keys
  |> list.fold(state, fn(state, person) {
    apply(state, person, presence.Tick(now))
  })
}

fn publish(state: State, person: Person, changes: List(presence.Change)) -> Nil {
  case changes {
    [] -> Nil
    _ ->
      changes
      |> list.map(fn(change) {
        PresenceEvent(
          event_id: ids.uuid_v4(),
          site_id: state.args.site_id,
          person:,
          change:,
        )
      })
      |> state.args.publish
  }
}

fn schedule_heartbeat(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Heartbeat)
      Nil
    }
    False -> Nil
  }
}
