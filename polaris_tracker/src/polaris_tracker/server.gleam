//// The mist HTTP layer: reads bodies for `api`, and serves the live stream.

import gleam/bytes_tree
import gleam/erlang/process
import gleam/http
import gleam/http/request.{type Request}
import gleam/http/response.{type Response}
import gleam/json
import gleam/otp/actor
import gleam/result
import gleam/string_tree
import mist.{type Connection, type ResponseData}
import polaris_tracker/api
import polaris_tracker/broadcaster
import polaris_tracker/wire

/// Large enough for 1,000 gateway sightings.
const max_body_bytes = 1_000_000

/// Browsers wait this long before reconnecting a dropped stream.
const reconnect_ms = 3000

pub fn handler(
  ctx: api.Context,
) -> fn(Request(Connection)) -> Response(ResponseData) {
  fn(req: Request(Connection)) {
    case req.method, request.path_segments(req) {
      http.Get, ["v1", "sites", site_id, "live"] -> live(req, ctx, site_id)
      _, _ ->
        case mist.read_body(req, max_body_bytes) {
          Ok(req) -> api.handle(req, ctx) |> to_mist
          Error(mist.ExcessBody) ->
            api.error(413, "body_too_large", "The body is larger than 1 MB.")
            |> api.with_cors(ctx.cors_origin)
            |> to_mist
          Error(mist.MalformedBody) ->
            api.error(400, "invalid_body", "The body could not be read.")
            |> api.with_cors(ctx.cors_origin)
            |> to_mist
        }
    }
  }
}

/// `GET /v1/sites/{site_id}/live`: a `snapshot` event, then one `presence`
/// event per change, and a `ping` every keep-alive period.
fn live(
  req: Request(Connection),
  ctx: api.Context,
  site_id: String,
) -> Response(ResponseData) {
  let checked = {
    use claims <- result.try(api.authenticate_stream(req, ctx))
    use site_id <- result.try(api.authorize_site(claims, site_id))
    // Fails early with 404 when the site is not configured.
    use _ <- result.map(api.snapshot(ctx, site_id))
    site_id
  }
  case checked {
    Error(response) -> response |> api.with_cors(ctx.cors_origin) |> to_mist
    Ok(site_id) ->
      mist.server_sent_events(
        request: req,
        initial_response: response.new(200) |> api.with_cors(ctx.cors_origin),
        init: fn(self) {
          // Subscribe before reading the snapshot so no change falls between.
          process.send(ctx.broadcaster, broadcaster.Subscribe(site_id, self))
          case api.snapshot(ctx, site_id) {
            Ok(snapshot) ->
              process.send(self, broadcaster.Snapshot(json.to_string(snapshot)))
            Error(_) -> Nil
          }
          Ok(actor.initialised(Nil))
        },
        loop: fn(state, notice, connection) {
          let event = case notice {
            broadcaster.Snapshot(data) ->
              mist.event(string_tree.from_string(data))
              |> mist.event_name("snapshot")
              |> mist.event_retry(reconnect_ms)
            broadcaster.Event(event) ->
              mist.event(json.to_string_tree(wire.presence_event_json(event)))
              |> mist.event_name("presence")
              |> mist.event_id(event.event_id)
            broadcaster.KeepAlive ->
              mist.event(string_tree.from_string("{}"))
              |> mist.event_name("ping")
          }
          case mist.send_event(connection, event) {
            Ok(Nil) -> actor.continue(state)
            // The client went away.
            Error(Nil) -> actor.stop()
          }
        },
      )
  }
}

fn to_mist(response: Response(String)) -> Response(ResponseData) {
  response.map(response, fn(body) { mist.Bytes(bytes_tree.from_string(body)) })
}
