"""OpenAPI examples of the presence endpoints, on the contract's data set (site 3,
foreman 7, workers 63 and 58, 2026-10-31; see ``apps/api/examples.py``)."""

TRACKING_TOKEN = {
    "token": (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJmb3JlbWFuOjciLCJraW5kIjoiZGV2aWNlIiwi"
        "Y29tcGFueSI6MSwic2l0ZXMiOlszXSwidHJhdmVsIjpmYWxzZSwiYXVkIjoicG9sYXJpcy10cmFja2VyIiwi"
        "ZXhwIjoxNzkzNDY4MDAwfQ.c2lnbmF0dXJl"
    ),
    "expires_at": "2026-10-31T18:00:00Z",
    "tracker_url": "https://tracker.example.com",
    "sites": [3],
    "settings": {
        "gps_interval_seconds": 60,
        "tracking_hours": {"start": "06:00", "end": "19:00"},
        "retention_days": 30,
    },
}

# ``details`` of ``403 consent_required``: what the phone's notice shows.
NOTICE = {
    "text_version": "2026-11-01",
    "tracking_hours": {"start": "06:00", "end": "19:00"},
    "gps_interval_seconds": 60,
    "retention_days": 30,
}

CONSENT_REQUEST = {
    "client_uuid": "0d9b6f3e-7a7c-4f43-9c1e-5b0a4d6f2e11",
    "accepted": True,
    "text_version": "2026-11-01",
}
CONSENT = {"accepted": True, "at": "2026-10-31T06:01:12Z", "text_version": "2026-11-01"}

EVENT_BATCH = {
    "events": [
        {
            "event_id": "1b4e28ba-2fa1-41d2-883f-0016d3cca427",
            "site_id": 3,
            "subject": {"kind": "worker", "id": 63},
            "type": "entered",
            "at": "2026-10-31T07:02:40Z",
            "zone_id": "A",
            "from_zone_id": None,
            "reason": None,
        },
        {
            "event_id": "6fa459ea-ee8a-4ca4-894e-db77e160355e",
            "site_id": 3,
            "subject": {"kind": "worker", "id": 63},
            "type": "moved_zone",
            "at": "2026-10-31T07:10:05Z",
            "zone_id": "B",
            "from_zone_id": "A",
            "reason": None,
        },
        {
            "event_id": "0b8ae8a3-50d6-4d3c-a3f0-5b0d2b8c6c41",
            "site_id": 3,
            "subject": {"kind": "worker", "id": 63},
            "type": "left",
            "at": "2026-10-31T16:48:21Z",
            "zone_id": "B",
            "from_zone_id": None,
            "reason": "timeout",
        },
    ]
}
INGEST_RESULT = {"stored": 3, "duplicates": 0, "ignored": 0, "rejected": 0}

SESSIONS = {
    "site_id": 3,
    "date": "2026-10-31",
    "sessions": [
        {
            "subject": {"kind": "worker", "id": 63},
            "first_seen_at": "2026-10-31T07:02:40Z",
            "last_seen_at": "2026-10-31T16:48:21Z",
            "open": False,
            "zones": ["A", "B"],
            "visits": 1,
        },
        {
            "subject": {"kind": "foreman", "id": 7},
            "first_seen_at": "2026-10-31T07:05:00Z",
            "last_seen_at": None,
            "open": True,
            "zones": [],
            "visits": 1,
        },
        {
            "subject": {"kind": "worker", "id": 58},
            "first_seen_at": "2026-10-31T07:31:10Z",
            "last_seen_at": None,
            "open": True,
            "zones": ["A"],
            "visits": 2,
        },
    ],
}
