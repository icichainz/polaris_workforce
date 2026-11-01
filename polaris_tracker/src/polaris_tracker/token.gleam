//// JWT HS256 tokens. Django issues every token; the tracker verifies them and
//// mints only the short-lived service tokens it sends back to Django.
////
//// Claims, version 2 (`docs/implementation-plan.md` §14):
////
//// ```
//// { "sub": "...", "kind": "device|gateway|dashboard|service",
////   "company": <company id>,   // required for every kind except service
////   "sites": [<site id>, ...], // explicit; service tokens reach every site
////   "travel": true|false,      // may see off-site track points of `company`
////   "aud": "polaris-tracker" | "polaris-django", "exp": <unix seconds> }
//// ```
////
//// An absent `company`, `sites` or `travel` reads as `0`, `[]` or `false`.
//// `all_sites` (version 1) is no longer read. `mint` writes the keys in the
//// order above, as Django does: `company` only when it is not 0, `sites`
//// (sorted, without repeats) and `travel` always, except on service tokens,
//// which are just `sub`, `kind`, `aud` and `exp`. The encoding is the one a
//// Python standard-library mint produces with compact separators: header
//// `{"alg":"HS256","typ":"JWT"}`, base64url without padding. Tests check it
//// byte for byte against tokens from Django's test suite.

import gleam/bit_array
import gleam/bool
import gleam/crypto
import gleam/dynamic/decode
import gleam/int
import gleam/json
import gleam/list
import gleam/result
import gleam/string

/// Audience of tokens the tracker accepts.
pub const tracker_audience = "polaris-tracker"

/// Audience of tokens the tracker sends to Django.
pub const django_audience = "polaris-django"

pub type Kind {
  /// A foreman's phone. `sub` is `foreman:<user id>`.
  Device
  /// A fixed BLE gateway. `sub` is `gateway:<gateway id>`.
  Gateway
  /// An office user's page. `sub` is `user:<user id>`.
  Dashboard
  /// Server to server. `sub` is `service:django` or `service:tracker`.
  Service
}

pub type Claims {
  Claims(
    sub: String,
    kind: Kind,
    /// The Django company the token belongs to; `0` for service tokens.
    company: Int,
    /// The only sites the token reaches (service tokens reach every site).
    sites: List(Int),
    /// May see off-site GPS points (travel) of people in `company`.
    travel: Bool,
    audience: String,
    /// Unix seconds.
    expires_at: Int,
  )
}

pub type TokenError {
  Malformed
  UnsupportedAlgorithm
  BadSignature
  WrongAudience
  Expired
  /// Every kind except `service` must name its company.
  MissingCompany
}

pub fn verify(
  token: String,
  secret secret: BitArray,
  audience audience: String,
  now_seconds now_seconds: Int,
) -> Result(Claims, TokenError) {
  case string.split(token, ".") {
    [header, payload, signature] -> {
      use algorithm <- result.try(decode_segment(
        header,
        decode.at(["alg"], decode.string),
      ))
      use <- bool.guard(algorithm != "HS256", Error(UnsupportedAlgorithm))
      use signature <- result.try(
        bit_array.base64_url_decode(signature)
        |> result.replace_error(Malformed),
      )
      let expected = sign(header <> "." <> payload, secret)
      use <- bool.guard(
        !crypto.secure_compare(signature, expected),
        Error(BadSignature),
      )
      use claims <- result.try(decode_segment(payload, claims_decoder()))
      use <- bool.guard(claims.audience != audience, Error(WrongAudience))
      use <- bool.guard(claims.expires_at <= now_seconds, Error(Expired))
      use <- bool.guard(
        claims.kind != Service && claims.company <= 0,
        Error(MissingCompany),
      )
      Ok(claims)
    }
    _ -> Error(Malformed)
  }
}

pub fn mint(claims: Claims, secret secret: BitArray) -> String {
  let header =
    json.object([#("alg", json.string("HS256")), #("typ", json.string("JWT"))])
    |> encode_segment
  let optional = fn(present, field) {
    case present {
      True -> [field]
      False -> []
    }
  }
  let payload =
    [
      [
        #("sub", json.string(claims.sub)),
        #("kind", json.string(kind_to_string(claims.kind))),
      ],
      optional(claims.company != 0, #("company", json.int(claims.company))),
      optional(claims.kind != Service, #(
        "sites",
        claims.sites
          |> list.unique
          |> list.sort(int.compare)
          |> json.array(json.int),
      )),
      optional(claims.kind != Service, #("travel", json.bool(claims.travel))),
      [
        #("aud", json.string(claims.audience)),
        #("exp", json.int(claims.expires_at)),
      ],
    ]
    |> list.flatten
    |> json.object
    |> encode_segment
  let signing_input = header <> "." <> payload
  signing_input
  <> "."
  <> bit_array.base64_url_encode(sign(signing_input, secret), False)
}

/// The token the tracker sends to Django (`POST /api/v1/presence/events/`):
/// `kind = service`, `sub = service:tracker`, `aud = polaris-django`, valid
/// for `service_token_seconds`, no company.
pub fn service_token_for_django(
  secret secret: BitArray,
  now_seconds now_seconds: Int,
) -> String {
  Claims(
    sub: "service:tracker",
    kind: Service,
    company: 0,
    sites: [],
    travel: False,
    audience: django_audience,
    expires_at: now_seconds + service_token_seconds,
  )
  |> mint(secret:)
}

/// Lifetime of the service tokens the tracker mints (at most 5 minutes).
pub const service_token_seconds = 300

/// Whether the token reaches this site (rule 8: site scoping). Only the
/// explicit `sites` list counts; service tokens reach every site.
pub fn can_access_site(claims: Claims, site_id: Int) -> Bool {
  claims.kind == Service || list.contains(claims.sites, site_id)
}

/// Whether the token may see off-site GPS points recorded for `company`:
/// only `travel` tokens of that same company.
pub fn can_see_travel(claims: Claims, company: Int) -> Bool {
  claims.travel && claims.company > 0 && claims.company == company
}

/// Splits `sub` into its prefix and identifier, for example
/// `"foreman:5"` → `#("foreman", "5")`.
pub fn subject(claims: Claims) -> Result(#(String, String), Nil) {
  string.split_once(claims.sub, ":")
}

/// The numeric identifier in `sub`, when it has the expected prefix.
pub fn subject_id(claims: Claims, prefix: String) -> Result(Int, Nil) {
  case subject(claims) {
    Ok(#(found, id)) if found == prefix -> int.parse(id)
    _ -> Error(Nil)
  }
}

pub fn kind_to_string(kind: Kind) -> String {
  case kind {
    Device -> "device"
    Gateway -> "gateway"
    Dashboard -> "dashboard"
    Service -> "service"
  }
}

pub fn describe_error(error: TokenError) -> String {
  case error {
    Malformed -> "The token is not a valid JWT."
    UnsupportedAlgorithm -> "Only HS256 tokens are accepted."
    BadSignature -> "The token signature is invalid."
    WrongAudience -> "The token was not issued for this service."
    Expired -> "The token has expired."
    MissingCompany -> "The token does not name its company."
  }
}

fn claims_decoder() -> decode.Decoder(Claims) {
  use sub <- decode.field("sub", decode.string)
  use kind <- decode.field("kind", kind_decoder())
  use company <- decode.optional_field("company", 0, decode.int)
  use sites <- decode.optional_field("sites", [], decode.list(decode.int))
  use travel <- decode.optional_field("travel", False, decode.bool)
  use audience <- decode.field("aud", decode.string)
  use expires_at <- decode.field("exp", decode.int)
  decode.success(Claims(
    sub:,
    kind:,
    company:,
    sites:,
    travel:,
    audience:,
    expires_at:,
  ))
}

fn kind_decoder() -> decode.Decoder(Kind) {
  use text <- decode.then(decode.string)
  case text {
    "device" -> decode.success(Device)
    "gateway" -> decode.success(Gateway)
    "dashboard" -> decode.success(Dashboard)
    "service" -> decode.success(Service)
    _ -> decode.failure(Device, "token kind")
  }
}

fn sign(input: String, secret: BitArray) -> BitArray {
  crypto.hmac(bit_array.from_string(input), crypto.Sha256, secret)
}

fn encode_segment(value: json.Json) -> String {
  value
  |> json.to_string
  |> bit_array.from_string
  |> bit_array.base64_url_encode(False)
}

fn decode_segment(
  segment: String,
  decoder: decode.Decoder(a),
) -> Result(a, TokenError) {
  use bits <- result.try(
    bit_array.base64_url_decode(segment) |> result.replace_error(Malformed),
  )
  use text <- result.try(
    bit_array.to_string(bits) |> result.replace_error(Malformed),
  )
  json.parse(text, decoder) |> result.replace_error(Malformed)
}
