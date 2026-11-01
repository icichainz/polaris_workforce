//// The JSON endpoints, independent of the HTTP server so they can be tested
//// without sockets. The live stream (`/v1/sites/{id}/live`) is in `server`.
//// Contract: `docs/api/tracker-openapi.yaml`.

import gleam/bit_array
import gleam/bool
import gleam/dynamic/decode
import gleam/erlang/process.{type Subject}
import gleam/http
import gleam/http/request.{type Request}
import gleam/http/response.{type Response}
import gleam/int
import gleam/json.{type Json}
import gleam/list
import gleam/option.{type Option, None, Some}
import gleam/result
import polaris_tracker/broadcaster
import polaris_tracker/clock
import polaris_tracker/domain.{type TrackPoint, BlePoint, GpsPoint, Person}
import polaris_tracker/hub
import polaris_tracker/sessions
import polaris_tracker/site
import polaris_tracker/token
import polaris_tracker/track_store
import polaris_tracker/tz
import polaris_tracker/wire

pub const max_phone_batch = 500

pub const max_gateway_batch = 1000

const max_track_range_ms = 604_800_000

const call_timeout_ms = 5000

/// Reading a whole day of a busy site can take longer.
const day_query_timeout_ms = 60_000

pub type Context {
  Context(
    hub: Subject(hub.Message),
    tracks: Subject(track_store.Message),
    broadcaster: Subject(broadcaster.Message),
    secret: BitArray,
    cors_origin: Option(String),
    clock: fn() -> Int,
    /// Compiled IANA time zones (`/usr/share/zoneinfo`).
    zoneinfo_dir: String,
  )
}

pub fn handle(req: Request(BitArray), ctx: Context) -> Response(String) {
  case req.method, request.path_segments(req) {
    http.Options, _ -> response.new(204) |> response.set_body("")
    http.Get, ["health"] -> health(ctx)
    http.Post, ["v1", "observations"] -> post_observations(req, ctx)
    http.Post, ["v1", "gateways", "sightings"] ->
      post_gateway_sightings(req, ctx)
    http.Get, ["v1", "sites", site_id, "presence"] ->
      get_presence(req, ctx, site_id)
    http.Get, ["v1", "people", kind, id, "track"] ->
      get_track(req, ctx, kind, id)
    http.Put, ["internal", "sites", site_id, "config"] ->
      put_site_config(req, ctx, site_id)
    http.Get, ["internal", "sites", site_id, "sessions"] ->
      get_sessions(req, ctx, site_id)
    _, _ -> error(404, "not_found", "No such endpoint.")
  }
  |> with_cors(ctx.cors_origin)
}

// ---- Endpoints ----------------------------------------------------------------

fn health(ctx: Context) -> Response(String) {
  let sites = process.call(ctx.hub, call_timeout_ms, hub.CountSites)
  json_response(
    200,
    json.object([#("status", json.string("ok")), #("sites", json.int(sites))]),
  )
}

fn post_observations(req: Request(BitArray), ctx: Context) -> Response(String) {
  use claims <- respond(authenticate(req, ctx, [token.Device]))
  use foreman_id <- respond(
    token.subject_id(claims, "foreman")
    |> result.replace_error(error(
      403,
      "forbidden",
      "Not a foreman device token.",
    )),
  )
  use observations <- respond(decode_body(req, wire.observations_decoder()))
  use <- bool.guard(
    list.length(observations) > max_phone_batch,
    batch_too_large(max_phone_batch),
  )
  let result =
    process.call(ctx.hub, call_timeout_ms, hub.IngestPhone(
      foreman_id,
      claims,
      observations,
      _,
    ))
  json_response(200, wire.ingest_result_json(result))
}

fn post_gateway_sightings(
  req: Request(BitArray),
  ctx: Context,
) -> Response(String) {
  use claims <- respond(authenticate(req, ctx, [token.Gateway]))
  use gateway_id <- respond(case token.subject(claims), claims.sites {
    Ok(#("gateway", gateway_id)), [_] -> Ok(gateway_id)
    _, _ ->
      Error(error(403, "forbidden", "Not a gateway token for exactly one site."))
  })
  let assert [site_id] = claims.sites
  use #(batch_id, sightings) <- respond(decode_body(
    req,
    wire.gateway_batch_decoder(),
  ))
  use <- bool.guard(
    list.length(sightings) > max_gateway_batch,
    batch_too_large(max_gateway_batch),
  )
  case
    process.call(ctx.hub, call_timeout_ms, hub.IngestGateway(
      gateway_id,
      site_id,
      batch_id,
      sightings,
      _,
    ))
  {
    Ok(result) -> json_response(200, wire.ingest_result_json(result))
    Error(hub.UnknownSite) ->
      error(404, "site_not_configured", "The site has no tracking config.")
    Error(hub.UnknownGateway) ->
      error(
        403,
        "unknown_gateway",
        "The gateway is not registered on the site.",
      )
  }
}

fn get_presence(
  req: Request(BitArray),
  ctx: Context,
  site_id: String,
) -> Response(String) {
  use claims <- respond(authenticate(req, ctx, [token.Device, token.Dashboard]))
  use site_id <- respond(authorize_site(claims, site_id))
  use snapshot <- respond(snapshot(ctx, site_id))
  json_response(200, snapshot)
}

fn get_track(
  req: Request(BitArray),
  ctx: Context,
  kind: String,
  id: String,
) -> Response(String) {
  use claims <- respond(authenticate(req, ctx, [token.Dashboard]))
  use person <- respond(
    {
      use kind <- result.try(domain.kind_from_string(kind))
      use id <- result.map(int.parse(id))
      Person(kind, id)
    }
    |> result.replace_error(error(404, "not_found", "No such person.")),
  )
  use #(from, to) <- respond(track_range(req))
  let points =
    process.call(ctx.tracks, call_timeout_ms, track_store.Query(
      person,
      from,
      to,
      _,
    ))
    |> list.filter(visible_to(claims, _))
  json_response(200, wire.track_json(person, from, to, points))
}

fn put_site_config(
  req: Request(BitArray),
  ctx: Context,
  site_id: String,
) -> Response(String) {
  use _claims <- respond(authenticate(req, ctx, [token.Service]))
  use site_id <- respond(parse_site_id(site_id))
  use config <- respond(decode_body(req, wire.site_config_decoder()))
  process.call(ctx.hub, call_timeout_ms, hub.ConfigureSite(site_id, config, _))
  response.new(204) |> response.set_body("")
}

/// `GET /internal/sites/{site_id}/sessions?date=YYYY-MM-DD&tz=…`: the day's
/// sessions recomputed from the stored track (`sessions.recompute`), for
/// Django's nightly reconciliation. The local day is defined in `tz`.
fn get_sessions(
  req: Request(BitArray),
  ctx: Context,
  site_id: String,
) -> Response(String) {
  use _claims <- respond(authenticate(req, ctx, [token.Service]))
  use site_id <- respond(parse_site_id(site_id))
  let query = request.get_query(req) |> result.unwrap([])
  use #(date_text, date) <- respond(
    {
      use text <- result.try(list.key_find(query, "date"))
      use date <- result.map(tz.parse_date(text))
      #(text, date)
    }
    |> result.replace_error(error(
      400,
      "invalid_date",
      "`date` must be a day as YYYY-MM-DD.",
    )),
  )
  use #(tz_text, zone) <- respond(
    {
      use text <- result.try(list.key_find(query, "tz"))
      use zone <- result.map(tz.load(text, ctx.zoneinfo_dir))
      #(text, zone)
    }
    |> result.replace_error(error(
      400,
      "invalid_tz",
      "`tz` must be an IANA time zone (Africa/Lome) or a UTC offset (+01:00).",
    )),
  )
  use config <- respond(
    process.call(ctx.hub, call_timeout_ms, hub.GetConfig(site_id, _))
    |> result.replace_error(error(
      404,
      "site_not_configured",
      "The site has no tracking config.",
    )),
  )
  let #(from_s, to_s) = tz.day_bounds(zone, date)
  let #(from, to) = #(from_s * 1000, to_s * 1000)
  let points =
    process.call(ctx.tracks, day_query_timeout_ms, track_store.SiteDay(
      site_id,
      from,
      to,
      _,
    ))
  // A visit still going on now stays open; on a past day, time runs on past
  // midnight long enough for the last visits to time out.
  let until = int.min(ctx.clock(), to + config.rules.absence_to_leave_ms)
  let found = sessions.recompute(points, site_id:, config:, until:)
  json_response(
    200,
    wire.sessions_json(site_id, date_text, tz_text, from, to, found),
  )
}

// ---- Shared with the live stream ---------------------------------------------

/// Checks the token, from the `Authorization` header or, for browsers'
/// `EventSource` which cannot set headers, the `access_token` query parameter.
pub fn authenticate_stream(
  req: Request(a),
  ctx: Context,
) -> Result(token.Claims, Response(String)) {
  let raw = case bearer(req) {
    Ok(raw) -> Ok(raw)
    Error(Nil) ->
      request.get_query(req) |> result.try(list.key_find(_, "access_token"))
  }
  verify(raw, ctx, [token.Dashboard])
}

/// Parses the site id and enforces site scoping (rule 8).
pub fn authorize_site(
  claims: token.Claims,
  site_id: String,
) -> Result(Int, Response(String)) {
  use site_id <- result.try(parse_site_id(site_id))
  case token.can_access_site(claims, site_id) {
    True -> Ok(site_id)
    False ->
      Error(error(403, "site_not_allowed", "This token cannot see the site."))
  }
}

/// The current presence on a site, as JSON.
pub fn snapshot(ctx: Context, site_id: Int) -> Result(Json, Response(String)) {
  let people = case
    process.call(ctx.hub, call_timeout_ms, hub.FindSite(site_id, _))
  {
    hub.NotConfigured -> Error(Nil)
    hub.Idle -> Ok([])
    hub.Running(subject) ->
      Ok(process.call(subject, call_timeout_ms, site.Snapshot))
  }
  case people {
    Ok(people) -> Ok(wire.snapshot_json(site_id, ctx.clock(), people))
    Error(Nil) ->
      Error(error(
        404,
        "site_not_configured",
        "The site has no tracking config.",
      ))
  }
}

pub fn error(status: Int, code: String, message: String) -> Response(String) {
  json_response(status, wire.error_json(code, message, json.object([])))
}

pub fn with_cors(
  response: Response(body),
  origin: Option(String),
) -> Response(body) {
  case origin {
    None -> response
    Some(origin) ->
      response
      |> response.set_header("access-control-allow-origin", origin)
      |> response.set_header(
        "access-control-allow-headers",
        "authorization, content-type",
      )
      |> response.set_header(
        "access-control-allow-methods",
        "GET, POST, PUT, OPTIONS",
      )
      |> response.set_header("vary", "origin")
  }
}

// ---- Helpers ------------------------------------------------------------------

fn respond(
  result: Result(a, Response(String)),
  next: fn(a) -> Response(String),
) -> Response(String) {
  case result {
    Ok(value) -> next(value)
    Error(response) -> response
  }
}

fn authenticate(
  req: Request(a),
  ctx: Context,
  kinds: List(token.Kind),
) -> Result(token.Claims, Response(String)) {
  verify(bearer(req), ctx, kinds)
}

fn bearer(req: Request(a)) -> Result(String, Nil) {
  case request.get_header(req, "authorization") {
    Ok("Bearer " <> raw) -> Ok(raw)
    _ -> Error(Nil)
  }
}

fn verify(
  raw: Result(String, Nil),
  ctx: Context,
  kinds: List(token.Kind),
) -> Result(token.Claims, Response(String)) {
  use raw <- result.try(
    raw
    |> result.replace_error(error(
      401,
      "unauthenticated",
      "A bearer token is required.",
    )),
  )
  use claims <- result.try(
    token.verify(
      raw,
      secret: ctx.secret,
      audience: token.tracker_audience,
      now_seconds: ctx.clock() / 1000,
    )
    |> result.map_error(fn(reason) {
      error(401, "invalid_token", token.describe_error(reason))
    }),
  )
  case list.contains(kinds, claims.kind) {
    True -> Ok(claims)
    False ->
      Error(error(403, "forbidden", "This token cannot use this endpoint."))
  }
}

fn parse_site_id(text: String) -> Result(Int, Response(String)) {
  int.parse(text)
  |> result.replace_error(error(404, "not_found", "No such site."))
}

fn decode_body(
  req: Request(BitArray),
  decoder: decode.Decoder(a),
) -> Result(a, Response(String)) {
  use text <- result.try(
    bit_array.to_string(req.body)
    |> result.replace_error(error(
      400,
      "invalid_body",
      "The body must be UTF-8 JSON.",
    )),
  )
  json.parse(text, decoder)
  |> result.map_error(fn(reason) {
    case reason {
      json.UnableToDecode(errors) ->
        json_response(
          400,
          wire.error_json(
            "invalid_body",
            "The body does not match the schema.",
            wire.decode_errors_json(errors),
          ),
        )
      _ -> error(400, "invalid_json", "The body is not valid JSON.")
    }
  })
}

fn track_range(
  req: Request(BitArray),
) -> Result(#(Int, Int), Response(String)) {
  let query = request.get_query(req) |> result.unwrap([])
  let parse = fn(name) {
    list.key_find(query, name) |> result.try(clock.parse_rfc3339)
  }
  case parse("from"), parse("to") {
    Ok(from), Ok(to) if to > from && to - from <= max_track_range_ms ->
      Ok(#(from, to))
    Ok(_), Ok(_) ->
      Error(error(
        400,
        "invalid_range",
        "`to` must be after `from`, at most 7 days later.",
      ))
    _, _ ->
      Error(error(
        400,
        "invalid_range",
        "`from` and `to` must be RFC 3339 timestamps.",
      ))
  }
}

/// Points on a site follow site scoping; off-site travel is only visible to
/// `travel` tokens of the company whose device recorded it.
fn visible_to(claims: token.Claims, point: TrackPoint) -> Bool {
  case point {
    GpsPoint(site_id: Some(site_id), ..) ->
      token.can_access_site(claims, site_id)
    GpsPoint(site_id: None, company:, ..) ->
      token.can_see_travel(claims, company)
    BlePoint(site_id:, ..) -> token.can_access_site(claims, site_id)
  }
}

fn batch_too_large(limit: Int) -> Response(String) {
  error(
    413,
    "batch_too_large",
    "At most " <> int.to_string(limit) <> " items per request.",
  )
}

fn json_response(status: Int, body: Json) -> Response(String) {
  response.new(status)
  |> response.set_header("content-type", "application/json")
  |> response.set_body(json.to_string(body))
}
