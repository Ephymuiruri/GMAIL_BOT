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

## Getting Started

Before you dive in, make sure you have the following ready:

### Prerequisites

You'll need:
- **A Google account with an email address** — Use any account you already have; the bot reads from that inbox
- **A [Gemini API key](https://aistudio.google.com/apikey)** — Free tier available! Sign up at Google AI Studio with no credit card required

We'll verify you have Python and the other tools when you install dependencies in the next step.

### Step 1: Set Up Google Cloud OAuth

Gmail requires OAuth so the bot can read your mail securely. Here's how to set it up:

1. Head to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (call it "Gmail Triage" or whatever you like)
2. Go to **APIs & Services → Library** and search for **Gmail API** — enable it
3. Then go to **APIs & Services → Credentials** → **+ Create Credentials → OAuth client ID**
4. Choose **Application type: Desktop app** and create it
5. Click the download icon next to your OAuth client to grab the JSON file
6. Then go to **APIs & Services → Oauth consent screen** → **Audience → Test users section → add test user - Here add your email to give the app access**
7. Create a `Credentials/` folder in this project directory and save the JSON as `Credentials/credentials.json`

**On first run**, the bot will open your browser to log in and automatically create `Credentials/token.json` — you only do the OAuth flow once.

### Step 2: Install Dependencies

Clone this repo and set up a Python virtual environment.

**On macOS/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cd gmail-mcp
uv sync
cd ..
```

**On Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd gmail-mcp
uv sync
cd ..
```

If you get an execution policy error on Windows, run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```
Then try `.\venv\Scripts\Activate.ps1` again.

### Step 3: Configure Your Settings

Copy the example config files and customize them for your inbox:

**On macOS/Linux:**
```bash
cp .env.example .env
cp preferences.example.txt preferences.txt
cp prefilter_rules_example.py prefilter_rules.py
```

**On Windows (PowerShell):**
```powershell
Copy-Item .env.example -Destination .env
Copy-Item preferences.example.txt -Destination preferences.txt
Copy-Item prefilter_rules_example.py -Destination prefilter_rules.py
```

Now edit each file:

- **`.env`** — Paste your **Gemini API key** here (the free tier is plenty). Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) — no credit card required.
- **`preferences.txt`** — Write a brief about yourself: what counts as an opportunity, what's urgent, what you never want to see. This text is sent to Gemini on every run, so be specific and honest.
- **`prefilter_rules.py`** — List senders you already know how to handle automatically (your boss, your school, recruiters, noisy newsletters). Anything matching a rule here skips Gemini entirely and goes straight to action. Anything that doesn't match falls through to Gemini.

None of these files are tracked in git — they're yours alone.

### Step 4: Run the Bot

Make sure your virtual environment is activated, then kick it off:

**On macOS/Linux:**
```bash
python3 gmail_client.py
```

**On Windows:**
```powershell
python gmail_client.py
```

Each run does this:
1. **Fetches** your unread mail from Gmail
2. **Prefilters** it against your deterministic rules (no LLM, fully auditable)
3. **Sends** the rest to Gemini along with your `preferences.txt` briefing
4. **Executes** the resulting actions (label, star, archive, etc.) through the MCP server
5. **Logs everything** to `logs/`:
   - `logs/emails_<timestamp>.json` — snapshot of what it saw
   - `logs/decisions_<timestamp>.json` — what it decided to do and why

**Want to watch it work?** See [LOGGING_GUIDE.md](LOGGING_GUIDE.md) to tail both client and server logs in parallel for real-time debugging.

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
