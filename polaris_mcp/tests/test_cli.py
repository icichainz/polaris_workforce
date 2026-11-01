"""The ``polaris`` CLI against a fake Polaris office API (``httpx.MockTransport``)."""

import json

import httpx
import pytest

from polaris_mcp import cli
from polaris_mcp.client import Settings

X = {"code": "XOF", "decimals": 0}


def m(amount):
    return {"amount": amount, "display": f"{amount:,} XOF".replace(",", " ")}


ME = {
    "user": {"id": 7, "name": "Afua Mensah", "role": "site_manager"},
    "company": {
        "name": "Polaris Construction",
        "currency": X,
        "time_zone": "UTC",
        "today": "2025-09-02",
    },
    "token": {"name": "laptop", "can_approve": True, "expires_at": "2025-12-01T00:00:00Z"},
    "sites": [{"id": 3, "name": "Alpha Court", "status": "active"}],
}
WORKER = {"id": 9, "name": "Yao Mensah", "number": "W-0009", "trade": "Mason"}
COUNTS = {"expected": 2, "present": 1, "absent": 1, "half_day": 0, "late": 0, "unmarked": 0}
DAY = {
    "day_id": 11,
    "site": {"id": 3, "name": "Alpha Court"},
    "date": "2025-09-02",
    "status": "submitted",
    "foreman": "Kossi Amegah",
    "submitted_at": "2025-09-02T07:44+00:00",
    "approved_at": None,
    "approved_by": "",
    "rejection_note": "",
    "foreman_note": "",
    "counts": COUNTS,
    "flags": ["1 changed after submission"],
    "records": [
        {
            "worker": WORKER,
            "status": "present",
            "arrival": "07:20",
            "departure": "17:00",
            "note": "",
            "corrected": True,
            "changes": [
                {
                    "field": "Arrival",
                    "old": "07:40",
                    "new": "07:20",
                    "reason": "Gate log",
                    "at": "09:10",
                }
            ],
            "paid_note": "",
        }
    ],
    "may_decide": True,
}


class FakePolaris:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.approved = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers["Authorization"] != "Bearer pol_test":
            return httpx.Response(
                401,
                json={"code": "not_authenticated", "message": "Invalid or revoked agent token."},
            )
        path = request.url.path.removeprefix("/api/v1/office/")
        if path == "me/":
            return httpx.Response(200, json=ME)
        if path == "approvals/":
            return httpx.Response(200, json={"days": [{**DAY, "day_id": 11}]})
        if path == "sites/3/days/2025-09-01/":
            return httpx.Response(200, json=DAY)
        if path == "sites/3/cash-sheets/2025-09-02/":
            line = {
                "worker": WORKER,
                "units": "1.00",
                "gross": m(6000),
                "advance_deduction": m(0),
                "net": m(6000),
                "paid": m(0),
                "is_paid": False,
            }
            return httpx.Response(
                200,
                json={
                    "site": DAY["site"],
                    "date": "2025-09-02",
                    "status": "not_open",
                    "day_status": "submitted",
                    "currency": X,
                    "cash_issued": m(0),
                    "total_to_pay": m(6000),
                    "paid_total": m(0),
                    "left_to_pay": m(6000),
                    "cash_in_hand": m(0),
                    "line_count": 1,
                    "paid_count": 0,
                    "lines": [line],
                    "absent": [],
                },
            )
        if path == "workers/":
            return httpx.Response(200, json={"workers": [{**WORKER, "site": DAY["site"]}]})
        if path == "days/11/decision-preview/":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "decision": body["decision"],
                    "note": body["note"],
                    "day": DAY,
                    "confirmation": "signed-c1",
                    "expires_at": "2025-09-02T10:10:00Z",
                },
            )
        if path == "decisions/":
            assert json.loads(request.content) == {"confirmation": "signed-c1"}
            self.approved = True
            return httpx.Response(
                200, json={"decision": "approve", "day": {**DAY, "status": "approved"}}
            )
        return httpx.Response(
            403,
            json={
                "code": "permission_denied",
                "message": "You do not have permission to perform this action.",
            },
        )


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("POLARIS_CONFIG", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("POLARIS_URL", "https://polaris.example")
    monkeypatch.setenv("POLARIS_TOKEN", "pol_test")
    return FakePolaris()


def run(fake, *argv):
    return cli.main(list(argv), transport=httpx.MockTransport(fake))


def test_whoami_and_today_style_reads(fake, capsys):
    assert run(fake, "whoami") == 0
    out = capsys.readouterr().out
    assert "Afua Mensah (site manager) · Polaris Construction" in out
    assert "read and approve roll calls" in out and "Alpha Court" in out

    assert run(fake, "approvals") == 0
    out = capsys.readouterr().out
    assert "DAY" in out and "Alpha Court" in out and "1 changed after submission" in out


def test_day_resolves_site_names_and_relative_dates(fake, capsys):
    assert run(fake, "day", "alpha", "yesterday") == 0
    out = capsys.readouterr().out
    assert fake.requests[-1].url.path.endswith("/sites/3/days/2025-09-01/")
    assert "Alpha Court · 2025-09-02 · submitted (day 11)" in out
    assert "present *" in out
    assert "Yao Mensah: Arrival 07:40 → 07:20 — “Gate log” (09:10)" in out


def test_json_prints_the_api_answer(fake, capsys):
    assert run(fake, "--json", "cash-sheet", "Alpha Court") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_to_pay"] == {"amount": 6000, "display": "6 000 XOF"}


def test_cash_sheet_text(fake, capsys):
    assert run(fake, "cash-sheet", "3", "today") == 0
    out = capsys.readouterr().out
    assert "To pay 6 000 XOF" in out and "Yao Mensah" in out


def test_workers_search_passes_filters(fake, capsys):
    assert run(fake, "workers", "yao", "--site", "Alpha") == 0
    assert dict(fake.requests[-1].url.params) == {"q": "yao", "site": "3"}
    assert "W-0009" in capsys.readouterr().out


def test_unknown_site_and_refusals_exit_1(fake, capsys):
    assert run(fake, "day", "Zulu") == 1
    assert "No site of yours matches 'Zulu'" in capsys.readouterr().err

    assert run(fake, "worker", "999") == 1
    assert "permission" in capsys.readouterr().err


def test_bad_token_exits_3(fake, monkeypatch, capsys):
    monkeypatch.setenv("POLARIS_TOKEN", "pol_wrong")
    assert run(fake, "whoami") == 3
    assert "revoked agent token" in capsys.readouterr().err


def test_not_signed_in_exits_3(fake, monkeypatch, capsys):
    monkeypatch.delenv("POLARIS_URL")
    monkeypatch.delenv("POLARIS_TOKEN")
    assert run(fake, "whoami") == 3
    assert "polaris login" in capsys.readouterr().err


def test_two_step_decision_for_scripts(fake, capsys):
    assert run(fake, "decision", "preview", "11", "approve") == 0
    out = capsys.readouterr().out
    assert "Decision: Approve" in out and out.rstrip().endswith("signed-c1")
    assert not fake.approved

    assert run(fake, "decision", "confirm", "signed-c1") == 0
    assert "Approved: Alpha Court · 2025-09-02 (now approved)." in capsys.readouterr().out
    assert fake.approved


def test_approve_asks_on_a_terminal(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    assert run(fake, "approve", "11") == 4
    captured = capsys.readouterr()
    assert "Approve the roll call of Alpha Court on 2025-09-02? [y/N]" in captured.err
    assert "Yao Mensah" in captured.err and captured.out == ""
    assert not fake.approved

    monkeypatch.setattr("builtins.input", lambda: "y")
    assert run(fake, "approve", "11") == 0
    assert "Approved: Alpha Court" in capsys.readouterr().out
    assert fake.approved


def test_approve_refuses_without_a_terminal(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run(fake, "approve", "11") == 2
    assert "polaris decision preview" in capsys.readouterr().err
    assert not any(r.url.path.endswith("/decision-preview/") for r in fake.requests)


def test_send_back_needs_a_note(fake, capsys):
    with pytest.raises(SystemExit) as exc:
        run(fake, "send-back", "11")
    assert exc.value.code == 2


def test_login_checks_the_token_then_saves_it(fake, monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("POLARIS_URL")
    monkeypatch.delenv("POLARIS_TOKEN")
    monkeypatch.setattr("getpass.getpass", lambda prompt: "pol_test")

    assert run(fake, "login", "--url", "https://polaris.example") == 0
    assert "Signed in as Afua Mensah" in capsys.readouterr().out
    saved = tmp_path / "credentials.json"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert Settings.load() == Settings("https://polaris.example", "pol_test")

    assert run(fake, "whoami") == 0  # now from the file
    capsys.readouterr()

    monkeypatch.setattr("getpass.getpass", lambda prompt: "pol_wrong")
    assert run(fake, "login", "--url", "https://polaris.example") == 3
    assert Settings.load().token == "pol_test"  # a bad token is not saved

    assert run(fake, "logout") == 0
    assert not saved.exists()


def test_login_refuses_plain_http(fake, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: "pol_test")
    assert run(fake, "login", "--url", "http://polaris.example") == 2
    assert "https" in capsys.readouterr().err


def test_the_cli_has_no_paying_command():
    commands = set(cli.parser()._subparsers._group_actions[0].choices)
    assert commands == {
        "login",
        "logout",
        "whoami",
        "today",
        "approvals",
        "day",
        "cash-sheet",
        "workers",
        "worker",
        "approve",
        "send-back",
        "decision",
    }
