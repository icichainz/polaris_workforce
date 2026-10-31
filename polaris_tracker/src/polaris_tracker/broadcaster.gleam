//// Fans presence events out to live subscribers (dashboard SSE streams),
//// filtered by site. Subscribers are monitored and dropped when they exit.

import gleam/erlang/process.{type Name, type Pid, type Subject}
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import polaris_tracker/domain.{type PresenceEvent}

/// What a subscriber receives.
pub type Notice {
  Event(PresenceEvent)
  /// Sent to every subscriber periodically, so dead connections are noticed.
  KeepAlive
  /// The site's current state, sent by a stream to itself when it opens.
  Snapshot(json: String)
}

pub type Message {
  Subscribe(site_id: Int, subscriber: Subject(Notice))
  Publish(events: List(PresenceEvent))
  Ping
  SubscriberDown(process.Down)
}

type Subscriber {
  Subscriber(site_id: Int, subject: Subject(Notice), pid: Pid)
}

type State {
  State(
    subscribers: List(Subscriber),
    self: Subject(Message),
    keepalive_ms: Int,
  )
}

pub fn start(
  keepalive_ms: Int,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(1000, fn(self) {
      schedule_ping(self, keepalive_ms)
      let selector =
        process.new_selector()
        |> process.select(self)
        |> process.select_monitors(SubscriberDown)
      State(subscribers: [], self:, keepalive_ms:)
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
    Subscribe(site_id:, subscriber:) ->
      case process.subject_owner(subscriber) {
        Ok(pid) -> {
          let _monitor = process.monitor(pid)
          let subscriber = Subscriber(site_id:, subject: subscriber, pid:)
          actor.continue(
            State(..state, subscribers: [subscriber, ..state.subscribers]),
          )
        }
        Error(Nil) -> actor.continue(state)
      }

    Publish(events:) -> {
      list.each(events, fn(event) {
        list.each(state.subscribers, fn(subscriber) {
          case subscriber.site_id == event.site_id {
            True -> process.send(subscriber.subject, Event(event))
            False -> Nil
          }
        })
      })
      actor.continue(state)
    }

    Ping -> {
      schedule_ping(state.self, state.keepalive_ms)
      list.each(state.subscribers, fn(subscriber) {
        process.send(subscriber.subject, KeepAlive)
      })
      actor.continue(state)
    }

    SubscriberDown(process.ProcessDown(pid:, ..)) -> {
      let subscribers =
        list.filter(state.subscribers, fn(subscriber) { subscriber.pid != pid })
      actor.continue(State(..state, subscribers:))
    }
    SubscriberDown(process.PortDown(..)) -> actor.continue(state)
  }
}

fn schedule_ping(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Ping)
      Nil
    }
    False -> Nil
  }
}
