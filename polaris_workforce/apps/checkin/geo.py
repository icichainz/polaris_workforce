"""Distances on the Earth's surface, for geofences."""

from __future__ import annotations

import math
from decimal import Decimal

EARTH_RADIUS_M = 6_371_008.8  # mean radius (IUGG)
# GPS on a cheap phone can be off by tens of metres. A fix counts as inside when the
# circle of its reported accuracy touches the geofence, but the accuracy given the
# benefit of the doubt is capped, so a fix "within 2 km" never counts as inside.
MAX_ACCURACY_ALLOWANCE_M = 50


def haversine_m(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in metres between two points given in decimal degrees."""
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    d_phi = phi2 - phi1
    d_lambda = math.radians(float(lng2) - float(lng1))
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def is_inside(distance_m: float, radius_m: int, accuracy_m: int | None = None) -> bool:
    """Inside a circle of ``radius_m``: ``distance − min(accuracy, 50 m) ≤ radius``."""
    allowance = min(accuracy_m or 0, MAX_ACCURACY_ALLOWANCE_M)
    return distance_m - allowance <= radius_m


def check(geofence, latitude: Decimal | None, longitude: Decimal | None, accuracy_m=None):
    """``(distance_m, inside)`` for a fix against a site's geofence; ``(None, None)``
    without a fix or a geofence."""
    if geofence is None or latitude is None or longitude is None:
        return None, None
    distance = haversine_m(geofence.latitude, geofence.longitude, latitude, longitude)
    return round(distance), is_inside(distance, geofence.radius_m, accuracy_m)
