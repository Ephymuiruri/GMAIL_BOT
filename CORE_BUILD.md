# Email triage bot — core build guide

> Phase 2 of 4. By the end of this guide you'll have a working bot that reads your inbox,
> classifies emails using Gemini, and applies Gmail labels automatically.

---

## What you're building

```
email-triage/
├── preferences.txt       ← your profile (already written)
├── .env                  ← API keys, never commit this
├── .gitignore            ← keeps secrets out of git
├── gmail_client.py       ← talks to Gmail via Gmail API
├── classifier.py         ← sends emails to Gemini, gets labels back
├── orchestrator.py       ← the main loop that runs everything
└── credentials/
    ├── credentials.json  ← Google OAuth client (you download this)
    └── token.json        ← auto-generated after first login
```

---

## Step 1 — Prerequisites

Make sure you have these before starting.

**Python 3.9+**
```bash
python3 --version
```

**pip packages**
```bash
pip install google-generativeai google-auth google-auth-oauthlib google-api-python-client python-dotenv
```

---

## Step 2 — Google Cloud setup

You need to create a project in Google Cloud to get OAuth credentials.
This sounds scarier than it is — takes about 5 minutes.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project — name it anything (e.g. `email-triage`)
3. In the sidebar go to **APIs & Services → Library**
4. Search for **Gmail API** and click **Enable**
5. Go to **APIs & Services → Credentials**
6. Click **Create Credentials → OAuth client ID**
7. Application type: **Desktop app**
8. Download the JSON — rename it to `credentials.json`
9. Place it in your `credentials/` folder

> **Why OAuth?** Gmail requires it for any app reading your email.
> Your `credentials.json` identifies your app to Google.
> `token.json` is created automatically the first time you run the bot
> and stores your login so you don't have to re-authenticate every time.

---

## Step 3 — Environment file

Create a `.env` file in your project root. Never commit this to git.

```bash
# .env
GEMINI_API_KEY=your_gemini_api_key_here
GMAIL_CREDENTIALS_PATH=credentials/credentials.json
GMAIL_TOKEN_PATH=credentials/token.json
```

Get your Gemini API key from [aistudio.google.com](https://aistudio.google.com) —
sign in with your Google Student AI Pro account, click **Get API key**.

Create your `.gitignore` now:

```bash
# .gitignore
.env
credentials/
__pycache__/
*.pyc
```

---

## Step 4 — gmail_client.py

This file handles everything Gmail-related. It authenticates, fetches emails,
strips HTML noise, and applies labels. It does not think — it just acts.

```python
# gmail_client.py

import os
import re
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from dotenv import load_dotenv

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials/credentials.json")
TOKEN_PATH       = os.getenv("GMAIL_TOKEN_PATH",       "credentials/token.json")


def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds = None

    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_unread_emails(service, max_results: int = 30) -> list[dict]:
    """
    Fetch unread emails. Returns a clean list of dicts with only
    what the classifier needs — no bloat.
    """
    results = service.users().messages().list(
        userId="me",
        q="is:unread",
        maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        raw = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
        subject = headers.get("Subject", "(no subject)")
        sender  = headers.get("From", "unknown")
        snippet = raw.get("snippet", "")

        # Strip HTML and truncate — token reduction built in
        clean_snippet = strip_html(snippet)[:300]

        emails.append({
            "id":      msg["id"],
            "sender":  sender,
            "subject": subject,
            "snippet": clean_snippet,
        })

    return emails


def get_or_create_label(service, label_name: str) -> str:
    """Get label ID by name, creating it if it doesn't exist."""
    existing = service.users().labels().list(userId="me").execute()

    for label in existing.get("labels", []):
        if label["name"].lower() == label_name.lower():
            return label["id"]

    # Create it
    created = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow"}
    ).execute()

    return created["id"]


def apply_label(service, email_id: str, label_name: str):
    """Apply a label to an email by name."""
    label_id = get_or_create_label(service, label_name)
    service.users().messages().modify(
        userId="me",
        id=email_id,
        body={"addLabelIds": [label_id]}
    ).execute()
```

---

## Step 5 — classifier.py

This is the AI brain. It takes a batch of emails, sends them to Gemini
with your preference profile as context, and gets back a label for each one.

One API call for up to 10 emails — not one call per email.

```python
# classifier.py

import os
import json
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# Load your preference profile once at startup
with open("preferences.txt", "r") as f:
    PREFERENCE_PROFILE = f.read()

SYSTEM_PROMPT = f"""
You are a personal email triage assistant. Classify each email based on this profile:

{PREFERENCE_PROFILE}

Valid labels (use exactly these words):
- opportunity   → internships, jobs, hackathons, open source, collaboration
- learn         → articles, newsletters, courses, tech news
- network       → people reaching out, mentors, peers, recruiters
- event         → meetups, conferences, webinars
- urgent        → school deadlines, work emails from colleagues/managers
- noise         → promotions, receipts, automated alerts, spam

Rules:
- When unsure, lean toward flagging (opportunity or learn) rather than noise
- Cold outreach from known orgs = opportunity, not spam
- Return ONLY a JSON array, no explanation, no markdown

Input format: array of emails with id, sender, subject, snippet
Output format: [{{"id": "...", "label": "..."}}]
"""


def classify_batch(emails: list[dict]) -> list[dict]:
    """
    Classify a batch of emails in a single API call.
    Returns list of {id, label} dicts.
    """
    if not emails:
        return []

    # Build a compact input — only what the AI needs
    compact = [
        {
            "id":      e["id"],
            "sender":  e["sender"],
            "subject": e["subject"],
            "snippet": e["snippet"],
        }
        for e in emails
    ]

    prompt = f"{SYSTEM_PROMPT}\n\nEmails to classify:\n{json.dumps(compact, indent=2)}"

    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()

        # Strip markdown fences if Gemini adds them
        raw = raw.replace("```json", "").replace("```", "").strip()

        results = json.loads(raw)
        return results

    except (json.JSONDecodeError, Exception) as e:
        print(f"  [classifier] Error: {e}")
        # Fallback — label everything as learn rather than crash
        return [{"id": e["id"], "label": "learn"} for e in emails]
```

---

## Step 6 — orchestrator.py

The main loop. This is the file you run. It connects every piece together:
fetch → pre-filter → classify → label → log.

```python
# orchestrator.py

import json
import os
from datetime import datetime
from gmail_client import get_gmail_service, fetch_unread_emails, apply_label
from classifier import classify_batch

# ── Sender rules — zero AI cost for known senders ──────────────────────────
# Add to this as you encounter repeat senders.
SENDER_RULES = {
    # format: "string in sender address": "label"
    "github.com":          "noise",
    "notifications@":      "noise",
    "noreply@":            "noise",
    "no-reply@":           "noise",
    "receipts@":           "noise",
    "invoice@":            "noise",
    "linkedin.com":        "network",
    "coursera.org":        "learn",
    "eventbrite.com":      "event",
    "meetup.com":          "event",
}

LOG_FILE = "decisions.log"
BATCH_SIZE = 10


def pre_filter(email: dict) -> str | None:
    """
    Check sender rules before calling the AI.
    Returns a label if matched, None if the AI should decide.
    """
    sender_lower = email["sender"].lower()
    for pattern, label in SENDER_RULES.items():
        if pattern in sender_lower:
            return label
    return None


def log_decision(email: dict, label: str, source: str):
    """Write every classification decision to the log file."""
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"[{source}] | "
        f"label={label:<12} | "
        f"from={email['sender'][:40]:<40} | "
        f"subject={email['subject'][:60]}\n"
    )
    with open(LOG_FILE, "a") as f:
        f.write(line)


def run():
    print("── Email triage bot starting ──")
    service = get_gmail_service()

    # 1. Fetch unread emails
    print("Fetching unread emails...")
    emails = fetch_unread_emails(service, max_results=30)
    print(f"  Found {len(emails)} unread emails")

    if not emails:
        print("Nothing to process.")
        return

    # 2. Pre-filter with sender rules
    to_classify = []
    rule_count = 0

    for email in emails:
        label = pre_filter(email)
        if label:
            apply_label(service, email["id"], label)
            log_decision(email, label, "rule")
            rule_count += 1
        else:
            to_classify.append(email)

    print(f"  Rules handled:   {rule_count} emails")
    print(f"  Sending to AI:   {len(to_classify)} emails")

    # 3. Classify remaining emails in batches
    ai_count = 0
    for i in range(0, len(to_classify), BATCH_SIZE):
        batch = to_classify[i:i + BATCH_SIZE]
        print(f"  Classifying batch {i // BATCH_SIZE + 1}...")

        results = classify_batch(batch)

        # Build lookup by id
        label_map = {r["id"]: r["label"] for r in results}

        for email in batch:
            label = label_map.get(email["id"], "learn")
            apply_label(service, email["id"], label)
            log_decision(email, label, "ai")
            print(f"    [{label:<12}] {email['subject'][:55]}")
            ai_count += 1

    print(f"\n── Done ──")
    print(f"  Rule-based:  {rule_count}")
    print(f"  AI-based:    {ai_count}")
    print(f"  Log written: {LOG_FILE}")


if __name__ == "__main__":
    run()
```

---

## Step 7 — First run

```bash
python3 orchestrator.py
```

The first time you run it, a browser window will open asking you to log in
to your Google account and grant the app permission to access Gmail.
This only happens once — after that `token.json` handles authentication silently.

**Expected output:**
```
── Email triage bot starting ──
Fetching unread emails...
  Found 24 unread emails
  Rules handled:   8 emails
  Sending to AI:   16 emails
  Classifying batch 1...
    [opportunity  ] AI research internship — summer 2026
    [learn        ] This week in AI — issue #47
    [urgent       ] Assignment submission deadline reminder
    [network      ] Re: coffee chat next week?
    [event        ] AI Summit London — early bird tickets
    ...

── Done ──
  Rule-based:  8
  AI-based:    16
  Log written: decisions.log
```

---

## Step 8 — Check your Gmail

Open Gmail. Your custom labels now appear in the left sidebar.
Click any label to see only those emails.

If something is miscategorised:

1. Check `decisions.log` to see what label was applied and whether it came from a rule or the AI
2. If it was a rule → update `SENDER_RULES` in `orchestrator.py`
3. If it was the AI → add a specific line to `preferences.txt`
4. Re-run

---

## What's next

Once this is running reliably, phase 3 adds:

- `rules.json` as a separate file (so you don't edit code to add sender rules)
- Weekly log review habit
- First preference profile tune based on real mistakes

Phase 4 adds scheduling (cron), newsletter summaries, and optional draft replies.

---

> **Tip:** Run the bot manually for the first week before scheduling it.
> You want to see what it does before it runs unsupervised.
