"""Examples for the check-in endpoints, on the shared data set (``apps.api.examples``).

Site 3 (Lomé, UTC+0), foreman Kossi's phone; on 2026-10-31 Delali Agbo (63) scans in at
07:35 (late: late after 07:15), Afi Dosseh (51) at 07:02 (present) and Yao Mensah (44)
in at 07:05 and out at 11:30 (half day). The geofence is 150 m around the site gate.
"""

from apps.api import examples as ex

DELALI_BADGE = "PW1:K7Q2M4XR5TBN3WCA.D6HZP2LQ4V7NYE3S"
CHECKIN_UUID = "9d5e1f7a-3b2c-4e8d-a1f0-6c7b8a9d0e12"
GEOFENCE = {"latitude": 6.131944, "longitude": 1.222778, "radius_m": 150}

CREATE_BADGE = {
    "client_uuid": CHECKIN_UUID,
    "site_id": ex.SITE_ID,
    "direction": "in",
    "at": f"{ex.DATE}T07:35:10Z",
    "badge": DELALI_BADGE,
    "latitude": 6.132101,
    "longitude": 1.223012,
    "accuracy_m": 12,
}
CREATE_MANUAL = {
    "client_uuid": "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c81",
    "site_id": ex.SITE_ID,
    "direction": "out",
    "at": f"{ex.DATE}T11:30:00Z",
    "worker_id": ex.YAO["id"],
}

CHECKIN = {
    "id": 5121,
    "client_uuid": CHECKIN_UUID,
    "site_id": ex.SITE_ID,
    "worker_id": ex.DELALI["id"],
    "date": ex.DATE,
    "direction": "in",
    "at": f"{ex.DATE}T07:35:10Z",
    "received_at": f"{ex.DATE}T07:35:12Z",
    "source": "foreman_scan",
    "latitude": 6.132101,
    "longitude": 1.223012,
    "accuracy_m": 12,
    "distance_m": 32,
    "inside_geofence": True,
    "counted": True,
    "draft_updated": False,
    "flags": [],
    "photo_url": None,
}
AFI_IN = {
    **CHECKIN,
    "id": 5118,
    "client_uuid": "7f3e2d1c-0b9a-4c8d-b7e6-5f4a3b2c1d01",
    "worker_id": ex.AFI["id"],
    "at": f"{ex.DATE}T07:02:40Z",
    "received_at": f"{ex.DATE}T07:02:41Z",
    "source": "kiosk",
    "photo_url": "https://polaris.example.com/api/v1/files/checkins/5118/photo/",
}
UNKNOWN = {
    **CHECKIN,
    "id": 5119,
    "client_uuid": "2c4e6a8b-1d3f-4b5c-9e7a-0f1e2d3c4b02",
    "worker_id": None,
    "at": f"{ex.DATE}T07:03:15Z",
    "received_at": f"{ex.DATE}T07:03:16Z",
    "latitude": 6.141002,
    "longitude": 1.230113,
    "accuracy_m": 20,
    "distance_m": 1305,
    "inside_geofence": False,
    "counted": False,
    "flags": ["unknown_badge", "outside_geofence"],
}

SUGGESTIONS = {
    "site_id": ex.SITE_ID,
    "date": ex.DATE,
    "attendance_mode": "both",
    "late_after": "07:15",
    "day_status": "not_started",
    "suggestions": [
        {
            "worker_id": ex.YAO["id"],
            "status": "half_day",
            "arrival": "07:05",
            "departure": "11:30",
            "first_in_at": f"{ex.DATE}T07:05:02Z",
            "last_out_at": f"{ex.DATE}T11:30:00Z",
            "checkin_count": 2,
        },
        {
            "worker_id": ex.AFI["id"],
            "status": "present",
            "arrival": "07:02",
            "departure": None,
            "first_in_at": f"{ex.DATE}T07:02:40Z",
            "last_out_at": None,
            "checkin_count": 1,
        },
        {
            "worker_id": ex.DELALI["id"],
            "status": "late",
            "arrival": "07:35",
            "departure": None,
            "first_in_at": f"{ex.DATE}T07:35:10Z",
            "last_out_at": None,
            "checkin_count": 1,
        },
    ],
}

SETUP = {
    "site_id": ex.SITE_ID,
    "attendance_mode": "both",
    "late_after": "07:15",
    "kiosk": False,
    "geofence": GEOFENCE,
    "badges": [
        {
            "worker_id": ex.DELALI["id"],
            "payload_sha256": "3f0c9a5e2b7d41c8a6e9f1b0d4c7a2e5f8b1c4d7e0a3b6c9d2e5f8a1b4c7d0e3",
            "issued_at": "2026-10-20T09:00:00Z",
        },
    ],
}

DEVICE_KIOSK = {
    "attendance_mode": "both",
    "kiosk": {
        "site_id": ex.SITE_ID,
        "site_name": ex.SITE["name"],
        "since": "2026-10-30T06:30:00Z",
    },
}
DEVICE_PHONE = {"attendance_mode": "both", "kiosk": None}
