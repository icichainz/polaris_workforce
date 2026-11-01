"""The check-in examples of ``openapi.yaml`` (Wave 4 integration, found by the Flutter
agent): the setup hash is the real hash of the example badge, times are quoted in the
YAML, and the site's check-ins and the suggestions tell one story."""

import hashlib
import re
from pathlib import Path

import yaml

from apps.checkin.api import examples as cx
from apps.checkin.api.serializers import (
    CheckInListSerializer,
    CheckInSerializer,
    RollCallSuggestionsSerializer,
)
from apps.presence.api import examples as px

OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "api" / "openapi.yaml"


def test_the_setup_hash_is_the_sha256_of_the_example_badge():
    expected = hashlib.sha256(cx.DELALI_BADGE.encode("utf-8")).hexdigest()
    assert cx.SETUP["badges"][0]["payload_sha256"] == expected
    assert expected == "fec1b7ccaaa29a119b19b2850565412f63b92fc748ef294c78726a3f2358a2aa"
    assert cx.SETUP["badges"][0]["worker_id"] == cx.CHECKIN["worker_id"]


def test_site_checkins_and_suggestions_tell_one_story():
    listed = CheckInListSerializer(
        data={"site_id": 3, "date": "2026-10-31", "checkins": cx.SITE_CHECKINS}
    )
    assert listed.is_valid(), listed.errors
    for item in cx.SITE_CHECKINS:
        assert CheckInSerializer(data=item).is_valid()
    times = [item["at"] for item in cx.SITE_CHECKINS]
    assert times == sorted(times)  # oldest first, like the endpoint

    counted = [c for c in cx.SITE_CHECKINS if c["counted"] and c["worker_id"]]
    for suggestion in cx.SUGGESTIONS["suggestions"]:
        if "checkin" not in suggestion["sources"]:
            continue  # presence only: see the next test
        mine = [c for c in counted if c["worker_id"] == suggestion["worker_id"]]
        ins = [c["at"] for c in mine if c["direction"] == "in"]
        outs = [c["at"] for c in mine if c["direction"] == "out"]
        assert suggestion["checkin_count"] == len(mine)
        assert suggestion["first_in_at"] == (min(ins) if ins else None)
        assert suggestion["last_out_at"] == (max(outs) if outs else None)
        assert suggestion["arrival"] == (min(ins)[11:16] if ins else None)
        assert suggestion["departure"] == (max(outs)[11:16] if outs else None)
    yao = next(s for s in cx.SUGGESTIONS["suggestions"] if s["worker_id"] == cx.YAO_IN["worker_id"])
    assert (yao["arrival"], yao["departure"]) == ("07:05", "11:30")


def test_the_suggestions_show_the_presence_sessions_of_the_same_day():
    """1.5: every ``presence`` in the suggestions is a worker's row of the presence
    sessions example; a presence-only suggestion takes its times from it."""
    serializer = RollCallSuggestionsSerializer(data=cx.SUGGESTIONS)
    assert serializer.is_valid(), serializer.errors
    sessions = {
        row["subject"]["id"]: row
        for row in px.SESSIONS["sessions"]
        if row["subject"]["kind"] == "worker"
    }
    with_presence = [s for s in cx.SUGGESTIONS["suggestions"] if s["presence"]]
    assert {s["worker_id"] for s in with_presence} == set(sessions)
    for suggestion in cx.SUGGESTIONS["suggestions"]:
        presence = suggestion["presence"]
        assert ("presence" in suggestion["sources"]) == (presence is not None)
        if presence is None:
            continue
        row = sessions[suggestion["worker_id"]]
        assert presence["first_seen_at"] == row["first_seen_at"]
        assert presence["last_seen_at"] == row["last_seen_at"]
        assert (presence["open"], presence["zones"], presence["visits"]) == (
            row["open"],
            row["zones"],
            row["visits"],
        )
        assert presence["seen_from"] == row["first_seen_at"][11:16]
        assert presence["seen_until"] == ((row["last_seen_at"] or "")[11:16] or None)
        if suggestion["sources"] == ["presence"]:
            assert suggestion["checkin_count"] == 0
            assert (suggestion["arrival"], suggestion["departure"]) == (
                presence["seen_from"],
                presence["seen_until"],
            )
            assert suggestion["status"] == presence["status"]
    ids = [s["worker_id"] for s in cx.SUGGESTIONS["suggestions"]]
    assert ids == sorted(ids)


def test_every_time_in_openapi_yaml_is_quoted():
    text = OPENAPI.read_text(encoding="utf-8")
    plain = re.findall(r"(?::|-)\s([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)\s*$", text, re.M)
    assert plain == []
    loaded = yaml.safe_load(text)
    assert loaded["info"]["version"]
