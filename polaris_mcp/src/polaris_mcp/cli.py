"""``polaris``: the Polaris office API from a shell, for people, scripts and coding agents.

Same token, same rules as the MCP server: reads, plus approving or sending back a roll
call in two steps. Nothing here can pay, issue cash or change workers; Polaris refuses
agent tokens everywhere else.

Output is readable text; ``--json`` prints what the API answered, for scripts.

Exit codes: 0 done, 1 Polaris refused (or nothing matched), 2 bad usage, 3 not signed in
or Polaris unreachable, 4 cancelled at the confirmation prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable
from typing import Any

import httpx

from .client import PolarisClient, PolarisError, Settings, SettingsError, credentials_path
from .office import Office

OK, REFUSED, USAGE, UNAVAILABLE, CANCELLED = 0, 1, 2, 3, 4


class Exit(Exception):
    def __init__(self, code: int, message: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message


def err(message: str) -> None:
    print(message, file=sys.stderr)


# --- text output -------------------------------------------------------------------------


def money(value: dict | None) -> str:
    return value["display"] if value else "—"


def words(value: str | None) -> str:
    return (value or "—").replace("_", " ")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    """Columns padded to fit; numbers right-aligned."""
    cells = [[("—" if v in (None, "") else str(v)) for v in row] for row in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]
    numeric = [
        bool(rows) and all(isinstance(row[i], int | float) for row in rows)
        for i in range(len(headers))
    ]

    def line(values):
        parts = [
            v.rjust(widths[i]) if numeric[i] else v.ljust(widths[i]) for i, v in enumerate(values)
        ]
        return "  ".join(parts).rstrip()

    return "\n".join([line(headers), *(line(r) for r in cells)])


def show_me(me: dict) -> str:
    user, company, token = me["user"], me["company"], me["token"]
    rights = "read and approve roll calls" if token["can_approve"] else "read only"
    lines = [
        f"{user['name']} ({words(user['role'])}) · {company['name']}",
        f"Token “{token['name']}”: {rights}, until {token['expires_at'][:10]}",
        f"Today {company['today']} · {company['currency']['code']} · {company['time_zone']}",
        "",
        table(["ID", "SITE", "STATUS"], [[s["id"], s["name"], s["status"]] for s in me["sites"]]),
    ]
    return "\n".join(lines)


def _alert(alert: dict) -> str:
    kind, site = alert["kind"], alert["site"] or "?"
    if kind == "missing_roll_call":
        return f"Roll call missing on {site} (foreman {alert['person'] or '?'})"
    if kind == "corrections":
        return f"{alert['count']} record(s) corrected after submission on {site}"
    if kind == "absent_streak":
        return f"{alert['person']} absent {alert['count']} days in a row ({site})"
    if kind == "unclosed_period":
        return f"Pay period not locked on {site} {alert['detail']}".rstrip()
    return f"{words(kind)}: {site} {alert['detail']}".rstrip()


def show_today(data: dict) -> str:
    working = "" if data["is_working_day"] else " (not a working day)"
    waiting = data["waiting_for_approval"]
    t = data["totals"]
    rows = [
        [
            row["site"]["name"],
            row["foreman"],
            words(row["status"]) + (" (late)" if row["overdue"] else ""),
            row["roll_call_due"],
            row["expected"],
            row["present"],
            row["absent"],
            row["half"],
            row["late"],
            money(row["cash_tonight"]),
        ]
        for row in data["sites"]
    ]
    lines = [
        f"{data['date']}{working} · {waiting} roll call(s) waiting for approval",
        "",
        table(
            [
                "SITE",
                "FOREMAN",
                "ROLL CALL",
                "DUE",
                "EXP",
                "PRES",
                "ABS",
                "HALF",
                "LATE",
                "CASH TONIGHT",
            ],
            rows,
        ),
        "",
        f"Total: {t['present']} present, {t['absent']} absent, {t['half']} half day, "
        f"{t['late']} late of {t['expected']} expected · cash tonight {money(t['cash_tonight'])}",
    ]
    if data["alerts"]:
        lines += ["", "Needs attention:", *(f"  ! {_alert(a)}" for a in data["alerts"])]
    return "\n".join(lines)


def show_approvals(data: dict) -> str:
    if not data["days"]:
        return "No roll call is waiting for approval."
    rows = [
        [
            d["day_id"],
            d["site"]["name"],
            d["date"],
            d["foreman"],
            d["counts"]["expected"],
            d["counts"]["present"],
            d["counts"]["absent"],
            d["counts"]["half_day"],
            d["counts"]["late"],
            "; ".join(d["flags"]),
        ]
        for d in data["days"]
    ]
    return table(
        ["DAY", "SITE", "DATE", "FOREMAN", "EXP", "PRES", "ABS", "HALF", "LATE", "FLAGS"], rows
    )


def show_day(day: dict) -> str:
    c = day["counts"]
    head = f"{day['site']['name']} · {day['date']} · {words(day['status'])}"
    if day["day_id"]:
        head += f" (day {day['day_id']})"
    lines = [head]
    if day["status"] == "not_started":
        lines.append(f"No roll call taken. {c['expected']} worker(s) expected.")
        return "\n".join(lines)
    sent = (day["submitted_at"] or "").replace("T", " ")[:16]
    lines.append(f"Foreman {day['foreman'] or '?'}" + (f", sent {sent}" if sent else ""))
    if day["approved_by"]:
        lines.append(
            f"Approved by {day['approved_by']}, {(day['approved_at'] or '')[:16].replace('T', ' ')}"
        )
    if day["rejection_note"]:
        lines.append(f"Sent back: {day['rejection_note']}")
    if day["foreman_note"]:
        lines.append(f"Foreman's note: {day['foreman_note']}")
    lines.append(
        f"Expected {c['expected']} · present {c['present']} · absent {c['absent']} · "
        f"half day {c['half_day']} · late {c['late']} · unmarked {c['unmarked']}"
    )
    lines += [f"! {flag}" for flag in day["flags"]]
    rows = []
    for r in day["records"]:
        mark = words(r["status"]) + (" *" if r["corrected"] else "")
        rows.append(
            [
                r["worker"]["name"],
                r["worker"]["number"],
                mark,
                r["arrival"],
                r["departure"],
                r["note"],
            ]
        )
    lines += ["", table(["WORKER", "NO.", "MARK", "IN", "OUT", "NOTE"], rows)]
    changes = [
        f"  {r['worker']['name']}: {ch['field']} {ch['old']} → {ch['new']} "
        f"— “{ch['reason']}” ({ch['at']})"
        for r in day["records"]
        for ch in r["changes"]
    ]
    if changes:
        lines += ["", "* Corrected after submission:", *changes]
    paid = [f"  {r['worker']['name']}: {r['paid_note']}" for r in day["records"] if r["paid_note"]]
    if paid:
        lines += ["", "Already paid:", *paid]
    return "\n".join(lines)


def show_cash_sheet(data: dict) -> str:
    rows = [
        [
            line["worker"]["name"],
            line["worker"]["number"],
            line["units"],
            money(line["gross"]),
            money(line["advance_deduction"]),
            money(line["net"]),
            money(line["paid"]),
            "yes" if line["is_paid"] else "",
        ]
        for line in data["lines"]
    ]
    lines = [
        f"{data['site']['name']} · cash sheet {data['date']} · {words(data['status'])} "
        f"(roll call {words(data['day_status'])})",
        f"To pay {money(data['total_to_pay'])} · paid {money(data['paid_total'])} · "
        f"left {money(data['left_to_pay'])} · cash issued {money(data['cash_issued'])} · "
        f"in hand {money(data['cash_in_hand'])}",
        "",
        table(["WORKER", "NO.", "DAYS", "GROSS", "ADVANCE", "NET", "PAID", "DONE"], rows)
        if rows
        else "No pay lines.",
    ]
    if data["absent"]:
        lines += ["", "Absent: " + ", ".join(w["name"] for w in data["absent"])]
    return "\n".join(lines)


def show_workers(data: dict) -> str:
    if not data["workers"]:
        return "No worker matches."
    rows = [
        [w["id"], w["name"], w["number"], w["trade"], w["site"]["name"] if w["site"] else ""]
        for w in data["workers"]
    ]
    return table(["ID", "NAME", "NO.", "TRADE", "SITE TODAY"], rows)


def show_worker(data: dict) -> str:
    site = data["site"]["name"] if data["site"] else "no site of yours today"
    lines = [
        f"{data['name']} ({data['number']}, {data['trade'] or 'no trade'}) · {site}",
        f"{data['from']} to {data['to']} · "
        f"still owes on advances: {money(data['advance_balance'])}",
        "",
    ]
    days = [
        [
            d["date"],
            d["site"]["name"],
            words(d["status"]) + (" *" if d["corrected"] else ""),
            d["arrival"],
            d["departure"],
            words(d["day_status"]),
            d["note"],
        ]
        for d in data["days"]
    ]
    lines.append(
        table(["DATE", "SITE", "MARK", "IN", "OUT", "ROLL CALL", "NOTE"], days)
        if days
        else "No roll-call marks in this period."
    )
    pay = [
        [
            f"{p['start_date']} → {p['end_date']}",
            p["site"]["name"],
            words(p["period_status"]),
            p["units"],
            money(p["gross"]),
            money(p["advance_deduction"]),
            money(p["statutory_deductions"]),
            money(p["net"]),
            money(p["paid"]),
        ]
        for p in data["pay"]
    ]
    if pay:
        lines += [
            "",
            table(
                [
                    "PERIOD",
                    "SITE",
                    "STATUS",
                    "DAYS",
                    "GROSS",
                    "ADVANCE",
                    "STATUTORY",
                    "NET",
                    "PAID",
                ],
                pay,
            ),
        ]
    return "\n".join(lines)


def show_preview(data: dict) -> str:
    verb = "Approve" if data["decision"] == "approve" else "Send back"
    lines = [show_day(data["day"]), "", f"Decision: {verb}"]
    if data["note"]:
        lines.append(f"Note to the foreman: {data['note']}")
    lines += [
        f"Confirmation (until {data['expires_at'][11:16]} UTC):",
        data["confirmation"],
    ]
    return "\n".join(lines)


def show_result(data: dict) -> str:
    done = "Approved" if data["decision"] == "approve" else "Sent back to the foreman"
    day = data["day"]
    return f"{done}: {day['site']['name']} · {day['date']} (now {words(day['status'])})."


# --- commands ----------------------------------------------------------------------------


async def cmd_whoami(office: Office, args) -> dict:
    return await office.me(refresh=True)


async def cmd_today(office: Office, args) -> dict:
    return await office.client.today()


async def cmd_approvals(office: Office, args) -> dict:
    return await office.client.approvals()


async def cmd_day(office: Office, args) -> dict:
    return await office.client.day(await office.site_id(args.site), await office.date(args.date))


async def cmd_cash_sheet(office: Office, args) -> dict:
    site_id = await office.site_id(args.site)
    return await office.client.cash_sheet(site_id, await office.date(args.date))


async def cmd_workers(office: Office, args) -> dict:
    site_id = await office.site_id(args.site) if args.site else None
    return await office.client.workers(args.query or "", site_id)


async def cmd_worker(office: Office, args) -> dict:
    start = await office.date(args.date_from) if args.date_from else None
    end = await office.date(args.date_to) if args.date_to else None
    return await office.client.worker(args.worker_id, start, end)


async def cmd_preview(office: Office, args) -> dict:
    return await office.client.preview_decision(args.day_id, args.decision, args.note or "")


async def cmd_confirm(office: Office, args) -> dict:
    return await office.client.confirm_decision(args.confirmation)


def ask(question: str) -> bool:
    """y/N on the terminal; the prompt goes to stderr so stdout stays clean."""
    print(question, end=" ", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes", "o", "oui"}


def interactive(decision: str):
    """``approve`` / ``send-back``: preview, show it, ask, then confirm. Needs a person."""

    async def run(office: Office, args) -> dict:
        if not sys.stdin.isatty():
            raise Exit(
                USAGE,
                "This asks for confirmation on a terminal. In a script, run "
                "`polaris decision preview` and then `polaris decision confirm`.",
            )
        preview = await office.client.preview_decision(
            args.day_id, decision, getattr(args, "note", "") or ""
        )
        day = preview["day"]
        verb = "Approve" if decision == "approve" else "Send back"
        err(show_day(day))
        if preview["note"]:
            err(f"\nNote to the foreman: {preview['note']}")
        if not ask(f"\n{verb} the roll call of {day['site']['name']} on {day['date']}? [y/N]"):
            raise Exit(CANCELLED, "Cancelled. Nothing changed.")
        return await office.client.confirm_decision(preview["confirmation"])

    return run


async def login(args, transport) -> int:
    url = args.url or input("Polaris address (https://…): ").strip()
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = getpass.getpass("Agent token (pol_…, hidden): ").strip()
    try:
        settings = Settings.checked(url, token)
    except SettingsError as exc:
        raise Exit(USAGE, str(exc)) from exc
    client = PolarisClient(settings, transport=transport)
    try:
        me = await client.me()
    finally:
        await client.aclose()
    path = settings.save()
    print(f"Signed in as {me['user']['name']} ({me['company']['name']}). Saved to {path}.")
    if Settings.from_env() is not None:
        err("Note: POLARIS_URL and POLARIS_TOKEN are set and take precedence over this file.")
    return OK


def logout() -> int:
    path = credentials_path()
    if path.exists():
        path.unlink()
        print(f"Removed {path}. The token still works until it is revoked in Polaris.")
    else:
        print("Not signed in on this computer.")
    return OK


# --- arguments ---------------------------------------------------------------------------

Handler = Callable[[Office, argparse.Namespace], Any]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polaris",
        description="Polaris Workforce from the shell: roll calls, cash sheets, workers. "
        "Read only, plus approving roll calls after a preview.",
        epilog="Dates: YYYY-MM-DD, today or yesterday. Sites: id, name or part of a name.",
    )
    p.add_argument("--json", action="store_true", help="print the API's JSON (for scripts)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name, handler, render, help_text, **kwargs):
        cmd = sub.add_parser(name, help=help_text, description=help_text, **kwargs)
        cmd.set_defaults(handler=handler, render=render)
        return cmd

    cmd = sub.add_parser("login", help="store the Polaris address and an agent token")
    cmd.add_argument("--url", help="Polaris address, e.g. https://polaris.example.com")
    cmd.add_argument("--token-stdin", action="store_true", help="read the token from stdin")
    sub.add_parser("logout", help="forget the stored token on this computer")

    add("whoami", cmd_whoami, show_me, "who the token acts for, and their sites")
    add("today", cmd_today, show_today, "today on the sites: roll calls, cash tonight, alerts")
    add("approvals", cmd_approvals, show_approvals, "roll calls waiting for approval")

    cmd = add("day", cmd_day, show_day, "a site's roll call for a date")
    cmd.add_argument("site")
    cmd.add_argument("date", nargs="?", default="today")

    cmd = add("cash-sheet", cmd_cash_sheet, show_cash_sheet, "a site's cash sheet for a pay day")
    cmd.add_argument("site")
    cmd.add_argument("date", nargs="?", default="today")

    cmd = add("workers", cmd_workers, show_workers, "find workers by name or number")
    cmd.add_argument("query", nargs="?", default="")
    cmd.add_argument("--site", help="only workers on this site today")

    cmd = add("worker", cmd_worker, show_worker, "a worker's days and pay (up to 62 days)")
    cmd.add_argument("worker_id", type=int)
    cmd.add_argument("--from", dest="date_from", help="first date (default: 13 days before --to)")
    cmd.add_argument("--to", dest="date_to", help="last date (default: today)")

    cmd = add("approve", interactive("approve"), show_result, "approve a roll call (asks first)")
    cmd.add_argument("day_id", type=int, help="from `polaris approvals`")
    cmd = add("send-back", interactive("reject"), show_result, "send a roll call back (asks first)")
    cmd.add_argument("day_id", type=int)
    cmd.add_argument("--note", required=True, help="what the foreman should check or fix")

    decision = sub.add_parser("decision", help="two-step decision for scripts: preview, confirm")
    steps = decision.add_subparsers(dest="step", required=True, metavar="STEP")
    cmd = steps.add_parser(
        "preview", help="check a decision; prints a confirmation, changes nothing"
    )
    cmd.set_defaults(handler=cmd_preview, render=show_preview)
    cmd.add_argument("day_id", type=int)
    cmd.add_argument("decision", choices=["approve", "reject"])
    cmd.add_argument("--note", default="", help="required to reject")
    cmd = steps.add_parser("confirm", help="carry out a previewed decision")
    cmd.set_defaults(handler=cmd_confirm, render=show_result)
    cmd.add_argument("confirmation")
    return p


async def run(args, transport=None) -> int:
    if args.command == "login":
        return await login(args, transport)
    if args.command == "logout":
        return logout()
    try:
        settings = Settings.load()
    except SettingsError as exc:
        raise Exit(UNAVAILABLE, str(exc)) from exc
    client = PolarisClient(settings, transport=transport)
    try:
        data = await args.handler(Office(client), args)
    finally:
        await client.aclose()
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else args.render(data))
    return OK


def main(
    argv: list[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args, transport))
    except Exit as exc:
        if exc.message:
            err(exc.message)
        return exc.code
    except PolarisError as exc:
        err(f"Polaris: {exc}")
        return UNAVAILABLE if exc.code == "unreachable" or exc.status == 401 else REFUSED
    except KeyboardInterrupt:
        err("Interrupted.")
        return CANCELLED


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
