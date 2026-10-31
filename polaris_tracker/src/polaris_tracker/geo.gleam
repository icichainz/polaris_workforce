//// Distances and geofence checks for GPS fixes.

pub type LatLng {
  LatLng(lat: Float, lng: Float)
}

/// A circular site boundary.
pub type Geofence {
  Geofence(center: LatLng, radius_m: Float)
}

/// Where a GPS fix puts someone relative to a geofence.
pub type Placement {
  Inside
  Outside
  /// Too close to the edge for the fix's accuracy, or the fix is too vague.
  Uncertain
}

const earth_radius_m = 6_371_000.0

/// Fixes less precise than this cannot place someone inside a site.
pub const max_useful_accuracy_m = 100.0

/// Great-circle distance in metres (haversine formula).
pub fn distance_m(from a: LatLng, to b: LatLng) -> Float {
  let lat1 = radians(a.lat)
  let lat2 = radians(b.lat)
  let half_dlat = radians(b.lat -. a.lat) /. 2.0
  let half_dlng = radians(b.lng -. a.lng) /. 2.0
  let h =
    sin(half_dlat)
    *. sin(half_dlat)
    +. cos(lat1)
    *. cos(lat2)
    *. sin(half_dlng)
    *. sin(half_dlng)
  2.0 *. earth_radius_m *. atan2(sqrt(h), sqrt(1.0 -. h))
}

/// Inside only when the fix is within the radius and precise enough; outside
/// only when even the fix's error margin cannot reach the boundary.
pub fn classify(
  fence: Geofence,
  point: LatLng,
  accuracy_m accuracy_m: Float,
) -> Placement {
  let distance = distance_m(fence.center, point)
  let clearly_outside = distance >. fence.radius_m +. accuracy_m
  let inside =
    distance <=. fence.radius_m && accuracy_m <=. max_useful_accuracy_m
  case clearly_outside, inside {
    True, _ -> Outside
    False, True -> Inside
    False, False -> Uncertain
  }
}

pub fn is_valid(point: LatLng) -> Bool {
  point.lat >=. -90.0
  && point.lat <=. 90.0
  && point.lng >=. -180.0
  && point.lng <=. 180.0
}

fn radians(degrees: Float) -> Float {
  degrees *. pi() /. 180.0
}

@external(erlang, "math", "pi")
fn pi() -> Float

@external(erlang, "math", "sin")
fn sin(x: Float) -> Float

@external(erlang, "math", "cos")
fn cos(x: Float) -> Float

@external(erlang, "math", "atan2")
fn atan2(y: Float, x: Float) -> Float

@external(erlang, "math", "sqrt")
fn sqrt(x: Float) -> Float
