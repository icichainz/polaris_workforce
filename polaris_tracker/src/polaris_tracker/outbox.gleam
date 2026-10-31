//// Delivers presence events to Django in batches, oldest first, and keeps
//// them until Django accepts them. Django stores events idempotently by
//// `event_id`, so a retry after a lost response is harmless.
////
//// Prototype: the queue is in memory. Production keeps it in the tracker's
//// own SQLite file so a restart does not lose undelivered events.

import gleam/erlang/process.{type Name, type Subject}
import gleam/io
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/otp/actor
import polaris_tracker/domain.{type PresenceEvent}

pub type Message {
  Enqueue(events: List(PresenceEvent))
  Flush
  Pending(reply: Subject(Int))
}

pub type Args {
  Args(
    deliver: fn(List(PresenceEvent)) -> Result(Nil, String),
    /// 0 disables the timer (tests send `Flush` themselves).
    flush_every_ms: Int,
    batch_size: Int,
  )
}

type State {
  State(args: Args, queue: List(PresenceEvent), self: Subject(Message))
}

pub fn start(
  args: Args,
  name: Option(Name(Message)),
) -> actor.StartResult(Subject(Message)) {
  let builder =
    actor.new_with_initialiser(1000, fn(self) {
      schedule_flush(self, args.flush_every_ms)
      State(args:, queue: [], self:)
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

fn handle_message(state: State, message: Message) -> actor.Next(State, Message) {
  case message {
    Enqueue(events:) ->
      actor.continue(State(..state, queue: list.append(state.queue, events)))

    Flush -> {
      schedule_flush(state.self, state.args.flush_every_ms)
      let #(batch, rest) = list.split(state.queue, state.args.batch_size)
      case batch {
        [] -> actor.continue(state)
        _ ->
          case state.args.deliver(batch) {
            Ok(Nil) -> actor.continue(State(..state, queue: rest))
            Error(reason) -> {
              io.println_error(
                "outbox: delivery to Django failed, will retry: " <> reason,
              )
              actor.continue(state)
            }
          }
      }
    }

    Pending(reply:) -> {
      process.send(reply, list.length(state.queue))
      actor.continue(state)
    }
  }
}

fn schedule_flush(self: Subject(Message), every_ms: Int) -> Nil {
  case every_ms > 0 {
    True -> {
      process.send_after(self, every_ms, Flush)
      Nil
    }
    False -> Nil
  }
}
