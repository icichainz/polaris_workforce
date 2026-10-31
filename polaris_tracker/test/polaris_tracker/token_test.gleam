import gleam/bit_array
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
    sites: [3, 4],
    all_sites: False,
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
    token.mint(token.Claims(..claims(), all_sites: True), secret: secret())
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
  assert token.can_access_site(token.Claims(..claims(), all_sites: True), 9)
}

pub fn subject_id_test() {
  assert token.subject_id(claims(), "foreman") == Ok(5)
  assert token.subject_id(claims(), "gateway") == Error(Nil)
}
