# polaris-mcp

Polaris Workforce for AI assistants and the shell. One package, two commands:

- **`polaris-mcp`**, an MCP server, so office staff can use Polaris from Claude Desktop.
- **`polaris`**, a command-line tool for people, scripts and coding agents.

Both talk to the Polaris office API (`/api/v1/office/`) with an **agent token**. They act
as the person who created the token: same company, role and sites.

**What they can do:** read today's overview, the roll calls waiting for approval, a
site's roll call, a cash sheet, and a worker's days and pay. If the token allows it, they
can also approve a submitted roll call or send it back to the foreman, in two steps: a
preview, then a confirmation.

**What they cannot do:** record payments, issue cash, give advances, or change workers,
pay rates or settings. Polaris refuses agent tokens on every other endpoint, so this does
not depend on the model or the script behaving.

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/) are needed. Until the package is
published, install it from a checkout of this repository:

```sh
uv tool install ./polaris_mcp
```

This puts `polaris` and `polaris-mcp` on the PATH.

## Get a token and sign in

In Polaris, open **AI assistant access** in the sidebar (admins, site managers and
accountants), give the token a name and a duration, and tick *May approve or send back
roll calls* only if you want that. The token (`pol_…`) is shown once. Revoke it on the
same screen; admins can revoke anyone's token.

```sh
polaris login --url https://polaris.example.com   # asks for the token (hidden)
polaris whoami
```

`login` checks the token with Polaris, then saves the address and token to
`~/.config/polaris/credentials.json`, readable by you only. `polaris logout` deletes that
file (the token still works until it is revoked in Polaris). The token is never taken as
a command-line argument, so it stays out of shell history.

For automation, pipe the token in with `polaris login --url … --token-stdin`, or set
`POLARIS_URL` and `POLARIS_TOKEN`, which take precedence over the file.
`POLARIS_CONFIG` moves the file. The address must use `https://`, except for `localhost`.

## The `polaris` command

```text
polaris whoami                         who the token acts for, and their sites
polaris today                          roll calls per site, cash tonight, alerts
polaris approvals                      roll calls waiting for approval (with day ids)
polaris day SITE [DATE]                a site's roll call, with corrections and reasons
polaris cash-sheet SITE [DATE]         a site's cash sheet for a pay day
polaris workers [QUERY] [--site SITE]  find workers by name or number
polaris worker ID [--from D] [--to D]  a worker's days and pay (up to 62 days)
polaris approve DAY_ID                 approve, after showing the day and asking y/N
polaris send-back DAY_ID --note TEXT   send back to the foreman, after asking y/N
polaris decision preview DAY_ID approve|reject [--note TEXT]
polaris decision confirm CONFIRMATION
```

Sites are an id, a name or part of a name (`polaris day riverside yesterday`). Dates are
`YYYY-MM-DD`, `today` (the default) or `yesterday`, in the company's time zone.

Add `--json` before the command to get the API's JSON instead of text, for example
`polaris --json approvals | jq '.days[].day_id'`. Money in JSON is
`{"amount": <minor units>, "display": "350 000 XOF"}`.

`approve` and `send-back` need a person at a terminal: they print the day to stderr, ask,
and do nothing unless the answer is yes. Without a terminal they refuse. Scripts use the
two explicit steps: `decision preview` prints a confirmation (valid 10 minutes) as its
last line, and `decision confirm` carries out exactly that decision.

Exit codes: `0` done, `1` Polaris refused (or no site matched), `2` bad usage, `3` not
signed in, token rejected or Polaris unreachable, `4` cancelled at the prompt.

## Claude Desktop

After `polaris login`, add this in Settings → Developer → Edit config, then restart
Claude:

```json
{
  "mcpServers": {
    "polaris": { "command": "polaris-mcp" }
  }
}
```

`polaris-mcp` reads the same credentials file. You can also pass `POLARIS_URL` and
`POLARIS_TOKEN` in an `"env"` block instead, but then the token sits in Claude's config
file. `polaris-mcp check` checks the connection.

### Tools

| Tool | What it does |
| --- | --- |
| `whoami` | The user, company (currency, time zone, today), sites, token rights |
| `today_overview` | Today's dashboard: roll calls per site, cash tonight, alerts |
| `pending_approvals` | Submitted roll calls waiting for approval |
| `site_roll_call` | One site's roll call for a date, with corrections and reasons |
| `cash_sheet` | One site's cash sheet for a pay day (read only) |
| `find_workers` | Workers by name, number or site (no phone numbers or ID documents) |
| `worker_history` | A worker's marks and pay lines over up to 62 days, and advances owed |
| `preview_roll_call_decision` | Step 1: check an approval or send-back and get a confirmation |
| `confirm_roll_call_decision` | Step 2: carry out exactly the previewed decision |

## How the approval stays safe

- Polaris signs the confirmation with the token, the day, the decision, the note and a
  fingerprint of the day's records. It expires after 10 minutes.
- If anything on the day changes after the preview (a correction, someone else approves),
  the confirmation is refused: preview again.
- Sending the same confirmation twice decides once (the second answer is the current day).
- Polaris writes its usual audit entry under the person's name, plus an
  `agents.day.approve` / `agents.day.reject` entry naming the token.
- In Claude Desktop the confirm tool is marked destructive, so Claude asks before running
  it, and the server's instructions tell the model to get an explicit yes first. In the
  shell, `approve` asks at the terminal.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`office.py` holds what both commands share (the profile, site names, relative dates);
`client.py` the HTTP client and credentials. The tests run the tools and the CLI
in-process against a fake Polaris (`httpx.MockTransport`). The office API itself is
tested in `polaris_workforce/tests/test_agents_api.py`.

## Not yet

- **claude.ai** (web) connectors need a remote MCP server reachable over the internet
  with OAuth sign-in. That waits for hosting; the tools and token rules here carry over.
