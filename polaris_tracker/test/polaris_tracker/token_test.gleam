import gleam/bit_array
import gleam/crypto
import gleam/string
import polaris_tracker/token

const now = 1_800_000_000

fn secret() -> BitArray {
  bit_array.from_string("test-secret")
}

fn claims() -> token.Claims {
  token.Claims(
    sub: "foreman:5",
    kind: token.Device,
    company: 7,
    sites: [3, 4],
    travel: False,
    audience: token.tracker_audience,
    expires_at: now + 60,
  )
}

fn verify(raw: String) -> Result(token.Claims, token.TokenError) {
  token.verify(
    raw,
    secret: secret(),
    audience: token.tracker_audience,
    now_seconds: now,
  )
}

pub fn round_trip_test() {
  assert verify(token.mint(claims(), secret: secret())) == Ok(claims())
}

pub fn wrong_secret_test() {
  let raw = token.mint(claims(), secret: bit_array.from_string("other"))
  assert verify(raw) == Error(token.BadSignature)
}

pub fn tampered_payload_test() {
  let assert [header, _, signature] =
    string.split(token.mint(claims(), secret: secret()), ".")
  let forged =
    token.mint(token.Claims(..claims(), sites: [3, 4, 9]), secret: secret())
  let assert [_, payload, _] = string.split(forged, ".")
  assert verify(header <> "." <> payload <> "." <> signature)
    == Error(token.BadSignature)
}

pub fn expired_test() {
  let raw =
    token.mint(token.Claims(..claims(), expires_at: now), secret: secret())
  assert verify(raw) == Error(token.Expired)
}

pub fn wrong_audience_test() {
  let raw =
    token.mint(
      token.Claims(..claims(), audience: token.django_audience),
      secret: secret(),
    )
  assert verify(raw) == Error(token.WrongAudience)
}

pub fn alg_none_is_rejected_test() {
  let header = bit_array.base64_url_encode(<<"{\"alg\":\"none\"}":utf8>>, False)
  let assert [_, payload, _] =
    string.split(token.mint(claims(), secret: secret()), ".")
  assert verify(header <> "." <> payload <> ".")
    == Error(token.UnsupportedAlgorithm)
}

pub fn garbage_test() {
  assert verify("not-a-token") == Error(token.Malformed)
}

pub fn site_scoping_test() {
  assert token.can_access_site(claims(), 3)
  assert !token.can_access_site(claims(), 9)
  // Only `sites` counts: travel does not widen site access.
  assert !token.can_access_site(token.Claims(..claims(), travel: True), 9)
  let service = token.Claims(..claims(), kind: token.Service, company: 0)
  assert token.can_access_site(service, 9)
}

/// A version 1 claim is ignored: `all_sites` grants nothing any more.
pub fn all_sites_is_not_read_test() {
  let raw =
    python_mint(
      "{\"sub\":\"user:1\",\"kind\":\"dashboard\",\"company\":7,"
      <> "\"sites\":[],\"all_sites\":true,\"aud\":\"polaris-tracker\","
      <> "\"exp\":1800000060}",
    )
  let assert Ok(claims) = verify(raw)
  assert claims.sites == []
  assert !token.can_access_site(claims, 3)
}

pub fn travel_is_per_company_test() {
  let admin = token.Claims(..claims(), kind: token.Dashboard, travel: True)
  assert token.can_see_travel(admin, 7)
  assert !token.can_see_travel(admin, 8)
  assert !token.can_see_travel(token.Claims(..admin, travel: False), 7)
  // A service token has no company, so it sees no company's travel.
  let service =
    token.Claims(..admin, kind: token.Service, company: 0, travel: True)
  assert !token.can_see_travel(service, 0)
}

pub fn company_is_required_except_for_service_test() {
  let raw = token.mint(token.Claims(..claims(), company: 0), secret: secret())
  assert verify(raw) == Error(token.MissingCompany)
  let service =
    token.Claims(..claims(), kind: token.Service, company: 0, sites: [])
  assert verify(token.mint(service, secret: secret())) == Ok(service)
}

// ---- Compatibility with a Python standard-library mint ---------------------
//
// Vectors from `base64`, `hashlib`, `hmac` and
// `json.dumps(..., separators=(",", ":"))`, secret `test-secret`.

const python_device = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmb3JlbWFuOjUiLCJraW5kIjoiZGV2aWNlIiwiY29tcGFueSI6Nywic2l0ZXMiOlszLDRdLCJ0cmF2ZWwiOnRydWUsImF1ZCI6InBvbGFyaXMtdHJhY2tlciIsImV4cCI6MTgwMDAwMDA2MH0.Fum_hqYaTWHH03QSa-w20W38fEdjKgi6mRMevhtiwCU"

const python_service = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzZXJ2aWNlOnRyYWNrZXIiLCJraW5kIjoic2VydmljZSIsImF1ZCI6InBvbGFyaXMtZGphbmdvIiwiZXhwIjoxODAwMDAwMzAwfQ.JDl4G181VuyXyZ1kx1nBPSuVOpAtiDnBkCmlmMyZjiU"

/// The same, with `json.dumps`'s default separators (spaces after `,` and
/// `:`): still accepted, since the signature covers the bytes as sent.
const python_spaced = "eyJhbGciOiAiSFMyNTYiLCAidHlwIjogIkpXVCJ9.eyJzdWIiOiAidXNlcjo5IiwgImtpbmQiOiAiZGFzaGJvYXJkIiwgImNvbXBhbnkiOiA3LCAic2l0ZXMiOiBbM10sICJ0cmF2ZWwiOiBmYWxzZSwgImF1ZCI6ICJwb2xhcmlzLXRyYWNrZXIiLCAiZXhwIjogMTgwMDAwMDA2MH0.jPG_ZU5ER2JisQ3qLjAKpq_5IyixT0biOwtH4iBTjtM"

pub fn python_token_verifies_test() {
  assert verify(python_device) == Ok(token.Claims(..claims(), travel: True))
  let assert Ok(spaced) = verify(python_spaced)
  assert spaced.kind == token.Dashboard
  assert spaced.company == 7
  assert spaced.sites == [3]
}

pub fn mint_is_byte_compatible_with_python_test() {
  assert token.mint(token.Claims(..claims(), travel: True), secret: secret())
    == python_device
}

pub fn service_token_for_django_test() {
  let raw = token.service_token_for_django(secret: secret(), now_seconds: now)
  assert raw == python_service
  let assert [header, ..] = string.split(raw, ".")
  // {"alg":"HS256","typ":"JWT"}
  assert header == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
  let assert Ok(claims) =
    token.verify(
      raw,
      secret: secret(),
      audience: token.django_audience,
      now_seconds: now,
    )
  assert claims.sub == "service:tracker"
  assert claims.kind == token.Service
  assert claims.expires_at - now <= 300
}

fn python_mint(payload: String) -> String {
  let encode = fn(text) {
    bit_array.base64_url_encode(bit_array.from_string(text), False)
  }
  let input =
    encode("{\"alg\":\"HS256\",\"typ\":\"JWT\"}") <> "." <> encode(payload)
  let signature =
    crypto.hmac(bit_array.from_string(input), crypto.Sha256, secret())
  input <> "." <> bit_array.base64_url_encode(signature, False)
}

pub fn subject_id_test() {
  assert token.subject_id(claims(), "foreman") == Ok(5)
  assert token.subject_id(claims(), "gateway") == Error(Nil)
}
