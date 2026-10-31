//// Delivery functions for the outbox: POST to Django, or log only.

import gleam/http
import gleam/http/request
import gleam/httpc
import gleam/int
import gleam/io
import gleam/json
import gleam/list
import gleam/result
import polaris_tracker/domain.{type PresenceEvent}
import polaris_tracker/token
import polaris_tracker/wire

/// Service tokens sent to Django live this long.
const service_token_seconds = 300

/// POSTs a batch to `POST /api/v1/presence/events/` with a short-lived
/// service token. Any 2xx counts as delivered.
pub fn deliver(
  url: String,
  secret: BitArray,
  clock: fn() -> Int,
) -> fn(List(PresenceEvent)) -> Result(Nil, String) {
  fn(events) {
    use req <- result.try(
      request.to(url) |> result.replace_error("invalid DJANGO_EVENTS_URL"),
    )
    let bearer =
      token.Claims(
        sub: "service:tracker",
        kind: token.Service,
        sites: [],
        all_sites: True,
        audience: token.django_audience,
        expires_at: clock() / 1000 + service_token_seconds,
      )
      |> token.mint(secret:)
    let req =
      req
      |> request.set_method(http.Post)
      |> request.set_header("content-type", "application/json")
      |> request.set_header("authorization", "Bearer " <> bearer)
      |> request.set_body(json.to_string(wire.presence_events_json(events)))
    case httpc.configure() |> httpc.timeout(5000) |> httpc.dispatch(req) {
      Ok(response) if response.status >= 200 && response.status < 300 -> Ok(Nil)
      Ok(response) ->
        Error("Django answered " <> int.to_string(response.status))
      Error(_) -> Error("Django is unreachable")
    }
  }
}

/// Used when `DJANGO_EVENTS_URL` is not set: prints each event as a JSON line.
pub fn log_only(events: List(PresenceEvent)) -> Result(Nil, String) {
  list.each(events, fn(event) {
    io.println(json.to_string(wire.presence_event_json(event)))
  })
  Ok(Nil)
}
