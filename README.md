# Gmail Triage Bot

An email triage bot that reads your unread Gmail, classifies each message with
Gemini, and applies labels/star/archive actions automatically — with a
rule-based prefilter that skips the LLM entirely for senders you already know
how to handle.

It's built as two pieces talking over [MCP](https://modelcontextprotocol.io/):

- **`gmail_client.py`** — the orchestrator. Fetches unread mail, runs it
  through the prefilter, sends what's left to Gemini, and drives the
  tool-calling loop.
- **`gmail-mcp/`** — an MCP server exposing a single `process_email` tool that
  actually calls the Gmail API (label / star / archive / mark read). It has no
  say in *what* to do with an email — only the client decides that.

The client logic is modularized into **`triage/`**, breaking concerns into:
- **`triage/gmail_auth.py`** — Gmail OAuth setup and email fetching
- **`triage/prefilter.py`** — Rule-based deterministic sender/subject/body matching
- **`triage/llm.py`** — Gemini classification and tool-calling loop
- **`triage/mcp_actions.py`** — Executing decisions through the MCP server
- **`triage/run_log.py`** — Audit trail snapshots and decision logs

See [LOGGING_GUIDE.md](LOGGING_GUIDE.md) for observability, [docs/implementation.md](docs/implementation.md) 
for the architecture reasoning (batching, prompt caching, tool design) and
[CORE_BUILD.md](CORE_BUILD.md) for a from-scratch walkthrough of the raw
Gmail API mechanics.

## Setup

### 1. Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (used to run the MCP server)
- A Google account with Gmail
- A [Gemini API key](https://aistudio.google.com/apikey)

### 2. Google Cloud OAuth credentials

Gmail requires OAuth for any app reading your mail.

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a project.
2. **APIs & Services → Library** → enable the **Gmail API**.
3. **APIs & Services → Credentials** → **Create Credentials → OAuth client
   ID** → Application type **Desktop app**.
4. Download the JSON, save it as `Credentials/credentials.json`.

The first run opens a browser to log in and creates `Credentials/token.json`
automatically — you won't need to repeat this.

### 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cd gmail-mcp
uv sync
cd ..
```

### 4. Configure

```bash
cp .env.example .env
cp preferences.example.txt preferences.txt
cp prefilter_rules_example.py prefilter_rules.py
```

- **`.env`** — add your `GEMINI_API_KEY`.
- **`preferences.txt`** — describe yourself: what counts as an opportunity,
  what's urgent, what you'd rather never see. This text is sent to the LLM
  on every run, so write it like a briefing.
- **`prefilter_rules.py`** — senders you already know how to handle
  (employer, school, recruiters, noisy newsletters). Anything that matches a
  rule here skips the LLM. Anything that doesn't falls through to Gemini.

None of these three files are tracked in git — they're yours.

### 5. Run

```bash
python3 gmail_client.py
```

Each run:
1. Fetches your unread mail via Gmail API
2. Prefilters it against deterministic rules (skips LLM, fully auditable)
3. Sends the rest to Gemini along with your `preferences.txt`
4. Executes the resulting actions via the MCP server
5. Writes audit logs:
   - `logs/emails_<run_id>.json` — raw email snapshot
   - `logs/decisions_<run_id>.json` — actions taken and why

**Observability:** See [LOGGING_GUIDE.md](LOGGING_GUIDE.md) to watch client and
server logs in parallel (`tail -f triage.log` + `tail -f gmail-mcp/mcp_server.log`).

## How classification works

1. **Prefilter** (`triage/prefilter.py`) — deterministic sender/subject/body rules
   from `prefilter_rules.py`. No LLM call, no ambiguity, fully auditable.
2. **LLM classification** — everything the prefilter didn't resolve goes to
   Gemini (in `triage/llm.py`) along with your `preferences.txt`. The model calls
   `process_email` via MCP once per email with its decision (label, star, archive).
3. **Execution** — both prefilter and LLM decisions are applied through the same
   MCP tool (in `triage/mcp_actions.py`), and both are logged with a `source` field
   (`"prefilter"` or `"ai"`) so you can tell why any given email ended up where it did.

Valid labels are `opportunity`, `learn`, `network`, `urgent`, `event`, or any
custom label your prefilter rules assign — Gmail labels are created
automatically if they don't exist yet.

## License

MIT — see [LICENSE](LICENSE).
