import gleam/bit_array
import gleam/list
import gleam/option.{None, Some}
import polaris_tracker/config

fn env(pairs: List(#(String, String))) -> fn(String) -> Result(String, Nil) {
  fn(name) { list.key_find(pairs, name) }
}

pub fn secret_is_required_test() {
  let assert Error(_) = config.from_env(env([]))
}

pub fn debug_allows_dev_secret_test() {
  let assert Ok(config) = config.from_env(env([#("TRACKER_DEBUG", "1")]))
  assert config.port == 4100
  assert config.bind == "127.0.0.1"
  assert config.django_events_url == None
}

pub fn values_from_env_test() {
  let assert Ok(config) =
    config.from_env(
      env([
        #("TRACKER_SHARED_SECRET", "s3cret"),
        #("TRACKER_PORT", "9000"),
        #("DJANGO_EVENTS_URL", "http://localhost:8000/api/v1/presence/events/"),
        #("TRACK_RETENTION_DAYS", "14"),
      ]),
    )
  assert config.secret == bit_array.from_string("s3cret")
  assert config.port == 9000
  assert config.track_retention_days == 14
  assert config.django_events_url
    == Some("http://localhost:8000/api/v1/presence/events/")
}

pub fn bad_number_test() {
  let assert Error(_) =
    config.from_env(env([#("TRACKER_DEBUG", "1"), #("TRACKER_PORT", "abc")]))
}
