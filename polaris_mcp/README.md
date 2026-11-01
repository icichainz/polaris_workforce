# polaris-mcp

An MCP server that lets office staff use Polaris Workforce from Claude Desktop. It talks
to the Polaris office API (`/api/v1/office/`) with an **agent token** and acts as the
person who created the token: same company, role and sites.

**What it can do:** read today's overview, the roll calls waiting for approval, a site's
roll call, a cash sheet, and a worker's days and pay. If the token allows it, it can also
approve a submitted roll call or send it back to the foreman, in two steps: a preview,
then a confirmation the person agrees to.

**What it cannot do:** record payments, issue cash, give advances, or change workers,
pay rates or settings. The server refuses agent tokens on every other endpoint, so this
does not depend on the model behaving.

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/) are needed. Until the package is
published, install it from a checkout of this repository:

```sh
uv tool install ./polaris_mcp
```

This puts `polaris-mcp` on the PATH.

## Get a token

In Polaris, open **AI assistant access** in the sidebar (admins, site managers and
accountants), give the token a name and a duration, and tick *May approve or send back
roll calls* only if you want that. The token (`pol_…`) is shown once. Revoke it on the
same screen; admins can revoke anyone's token.

Check the token and the address:

```sh
POLARIS_URL=https://polaris.example.com POLARIS_TOKEN=pol_… polaris-mcp check
```

## Claude Desktop

Settings → Developer → Edit config, then restart Claude:

```json
{
  "mcpServers": {
    "polaris": {
      "command": "polaris-mcp",
      "env": {
        "POLARIS_URL": "https://polaris.example.com",
        "POLARIS_TOKEN": "pol_…"
      }
    }
  }
}
```

`POLARIS_URL` must use `https://`, except for `localhost` during development.

## Tools

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

Sites can be given by name (or part of it) or id; dates as `YYYY-MM-DD`, `today` or
`yesterday`. Money comes as `{"amount": <minor units>, "display": "350 000 XOF"}`.

## How the approval stays safe

- Polaris signs the confirmation with the token, the day, the decision, the note and a
  fingerprint of the day's records. It expires after 10 minutes.
- If anything on the day changes after the preview (a correction, someone else approves),
  the confirmation is refused and the model has to preview again.
- Sending the same confirmation twice decides once (the second answer is the current day).
- Polaris writes its usual audit entry under the person's name, plus an
  `agents.day.approve` / `agents.day.reject` entry naming the token.
- The confirm tool is marked destructive, so Claude Desktop asks before running it, and
  the server's instructions tell the model to get an explicit yes first.

## Development

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests run the tools in-process against a fake Polaris (`httpx.MockTransport`). The
office API itself is tested in `polaris_workforce/tests/test_agents_api.py`.

## Not yet

- **claude.ai** (web) connectors need a remote MCP server reachable over the internet
  with OAuth sign-in. That waits for hosting; the tools and token rules here carry over.
- A shell CLI for scripts: the HTTP client in `client.py` is the place to start.
