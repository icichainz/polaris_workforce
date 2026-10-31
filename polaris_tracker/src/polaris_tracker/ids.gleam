import gleam/bit_array
import gleam/crypto
import gleam/string

/// A random version 4 UUID, lower case with dashes.
pub fn uuid_v4() -> String {
  let assert <<
    a:bits-size(48),
    _:size(4),
    b:bits-size(12),
    _:size(2),
    c:bits-size(62),
  >> = crypto.strong_random_bytes(16)
  let hex =
    <<a:bits, 4:size(4), b:bits, 2:size(2), c:bits>>
    |> bit_array.base16_encode
    |> string.lowercase
  string.slice(hex, 0, 8)
  <> "-"
  <> string.slice(hex, 8, 4)
  <> "-"
  <> string.slice(hex, 12, 4)
  <> "-"
  <> string.slice(hex, 16, 4)
  <> "-"
  <> string.slice(hex, 20, 12)
}
