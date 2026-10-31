import polaris_tracker/geo.{
  type LatLng, Geofence, Inside, LatLng, Outside, Uncertain,
}

// About 111 m per 0.001° of latitude.
const center = LatLng(lat: 6.1319, lng: 1.2228)

fn north_by(degrees: Float) -> LatLng {
  LatLng(lat: center.lat +. degrees, lng: center.lng)
}

fn fence() -> geo.Geofence {
  Geofence(center:, radius_m: 150.0)
}

pub fn distance_is_about_111_m_per_thousandth_degree_test() {
  let d = geo.distance_m(center, north_by(0.001))
  assert d >. 110.0 && d <. 112.0
}

pub fn distance_to_self_is_zero_test() {
  assert geo.distance_m(center, center) == 0.0
}

pub fn precise_fix_inside_test() {
  assert geo.classify(fence(), north_by(0.001), accuracy_m: 10.0) == Inside
}

pub fn far_fix_outside_even_when_vague_test() {
  assert geo.classify(fence(), north_by(0.01), accuracy_m: 300.0) == Outside
}

pub fn fix_near_edge_is_uncertain_test() {
  // ~167 m from the centre, but the fix could be 40 m off.
  assert geo.classify(fence(), north_by(0.0015), accuracy_m: 40.0) == Uncertain
}

pub fn vague_fix_cannot_place_inside_test() {
  assert geo.classify(fence(), center, accuracy_m: 250.0) == Uncertain
}

pub fn validity_test() {
  assert geo.is_valid(center)
  assert !geo.is_valid(LatLng(lat: 91.0, lng: 0.0))
  assert !geo.is_valid(LatLng(lat: 0.0, lng: -181.0))
}
