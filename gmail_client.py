"""
MCP client + orchestrator.

Connects to Gmail directly (to fetch emails), starts the gmail-mcp server as
a subprocess and talks to it over MCP (to execute actions), and drives the
tool-calling conversation with Gemini. This file owns the whole run: fetch ->
snapshot -> classify -> act -> log.
"""

import base64
import json
import os
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "Credentials/credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "Credentials/token.json")

GEMINI_MODEL = "gemini-3.5-flash"
MAX_RESULTS = 30
LOGS_DIR = "logs"
BODY_SNIPPET_CHARS = 2000

# Senders whose prefilter rules need to inspect body content, not just the
# subject/snippet (e.g. distinguishing a GitHub mention from a CI notification).
DEEP_INSPECT_SENDERS = [
    "notifications@github.com",
    "clientservices@cytonn.com",
]


def get_gmail_service():
    """Authenticate and return a Gmail API service object (client-side auth)."""
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


def _extract_body_text(payload: dict) -> str:
    """Walk a Gmail message payload and decode the first text/plain part found."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _extract_body_text(part)
        if text:
            return text

    return ""


def _needs_deep_inspect(sender: str) -> bool:
    sender = sender.lower()
    return any(needle in sender for needle in DEEP_INSPECT_SENDERS)


def fetch_unread_emails(service, max_results: int = MAX_RESULTS) -> list[dict]:
    """Fetch unread emails, keeping every field we might reasonably need later.

    Most senders only need metadata + snippet for triage. A short list of
    senders (DEEP_INSPECT_SENDERS) have prefilter rules that key off body
    content (e.g. "were you mentioned"), so those get a second full-format
    fetch to pull a truncated body.
    """
    results = service.users().messages().list(
        userId="me",
        q="is:unread",
        maxResults=max_results,
    ).execute()

    messages = results.get("messages", [])
    emails = []

    for msg in messages:
        raw = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        ).execute()

        headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
        sender = headers.get("From", "unknown")

        body = ""
        if _needs_deep_inspect(sender):
            full = service.users().messages().get(
                userId="me",
                id=msg["id"],
                format="full",
            ).execute()
            body = _extract_body_text(full["payload"])[:BODY_SNIPPET_CHARS]

        emails.append({
            "id": raw["id"],
            "thread_id": raw["threadId"],
            "sender": sender,
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "snippet": raw.get("snippet", ""),
            "body": body,
            "label_ids": raw.get("labelIds", []),
        })

    return emails


def write_snapshot(emails: list[dict], run_id: str) -> str:
    """Write the raw fetch snapshot to disk. Untouched audit record — never re-written."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"emails_{run_id}.json")
    with open(path, "w") as f:
        json.dump({"fetched_at": datetime.now().isoformat(), "emails": emails}, f, indent=2)
    return path


def write_decisions(decisions: list[dict], run_id: str) -> str:
    """Write what was decided/done for each email, separate from the raw snapshot."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    path = os.path.join(LOGS_DIR, f"decisions_{run_id}.json")
    with open(path, "w") as f:
        json.dump({"run_at": datetime.now().isoformat(), "decisions": decisions}, f, indent=2)
    return path


def build_system_prompt() -> str:
    with open("preferences.txt", "r") as f:
        preferences = f.read()

    return f"""You are a personal email triage assistant. For each email in the
batch, decide what to do with it and call the `process_email` tool once per
email with your decision.

Valid values for the `label` argument (use exactly these words, or omit to
apply no label):
- opportunity, learn, network, urgent, event

Use the `star` argument for anything with a deadline or that needs a response.
Use the `archive` argument for promotional emails, receipts, and social
notifications that don't need to stay in the inbox.

User profile / preferences:
{preferences}

Call `process_email` exactly once per email you are given, using its id.
"""

# Senders that are archived outright, no LLM involvement.
PRE_FILTER_ARCHIVE_LIST = [
    # Social Loop Noise (Likes, Suggestions, Digests)
    {"from": "unread-messages@mail.instagram.com"},
    {"from": "informational@email.snapchat.com"},
    {"from": "no-reply@todoist.com", "subject_contains": "task(s) for"},
    {"from": "info@email.jumia.co.ke"},
    {"from": "no-reply@twitch.tv"},
    # Generic LinkedIn Updates (Matches that are NOT human-to-human DMs)
    {"from": "newsletters-noreply@linkedin.com"},
    {"from": "jobalerts-noreply@linkedin.com"},
]

# Senders that are fast-tracked to a label, no LLM involvement.
PRE_FILTER_FAST_PASS = [
    # Urgent Academic Senders
    {"domain": "students.uonbi.ac.ke", "label": "urgent"},
    {"domain": "uonbi.ac.ke", "label": "urgent"},
    # Urgent Work & Team Senders
    {"domain": "chumz.io", "label": "urgent"},
    {"domain": "moneto.ventures", "label": "urgent"},
    # High-Value Developer Pipelines
    {"from": "info@turing.com", "label": "opportunity"},
    {"domain": "mercor.com", "label": "opportunity"},
    {"domain": "propel.com", "label": "opportunity"},
]

GITHUB_MENTION_KEYWORDS = [
    "mentioned you",
    "requested your review",
    "assigned you",
    "@ephymuiruri",
]

LINKEDIN_ARCHIVE_SUBJECT_KEYWORDS = ["add you", "congratulations", "skills", "news"]


def _sender_matches(sender: str, rule: dict) -> bool:
    sender = sender.lower()
    if "from" in rule and rule["from"] not in sender:
        return False
    if "domain" in rule and rule["domain"] not in sender:
        return False
    return True


def _triage_github(body: str) -> dict | str:
    """Route GitHub notifications: human mentions go to the LLM, everything
    else (CI/CD runs, digests, generic activity) is machine noise."""
    if any(keyword in body.lower() for keyword in GITHUB_MENTION_KEYWORDS):
        return "ROUTE_TO_LLM"
    return {"label": "dev-automated", "star": False, "archive": False}


def _triage_cytonn(subject: str) -> dict | str:
    """Route Cytonn/financial mail: predictable logs are filed away, everything
    else (advisory notes, one-offs) goes to the LLM."""
    if any(keyword in subject for keyword in ["statement", "payment notification", "receipt"]):
        return {"label": "finance-logs", "star": False, "archive": False}
    if any(keyword in subject for keyword in ["wmt", "seminar", "training"]):
        return {"label": "finance-logs", "star": False, "archive": True}
    return "ROUTE_TO_LLM"


def _triage_linkedin(subject: str) -> dict | str:
    """Route LinkedIn mail by subject: passive updates are archived silently,
    everything else (including human contact like messages/InMail/proposals)
    goes to the LLM to assess."""
    if any(keyword in subject for keyword in LINKEDIN_ARCHIVE_SUBJECT_KEYWORDS):
        return {"label": None, "star": False, "archive": True}
    return "ROUTE_TO_LLM"


def prefilter(emails: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split emails into (decisions, remaining) without involving the LLM where
    the outcome is already deterministic from sender/subject/body rules.
    `decisions` are ready to execute via MCP; `remaining` still need Gemini.
    """
    decisions = []
    remaining = []

    for email in emails:
        sender = email["sender"].lower()
        subject = email["subject"].lower()
        body = email.get("body", "") or ""

        archived = next((r for r in PRE_FILTER_ARCHIVE_LIST if _sender_matches(sender, r)
                          and ("subject_contains" not in r or r["subject_contains"] in subject)), None)
        if archived:
            decisions.append({
                "id": email["id"], "label": None, "star": False, "archive": True,
                "source": "prefilter",
            })
            continue

        fast_pass = next((r for r in PRE_FILTER_FAST_PASS if _sender_matches(sender, r)), None)
        if fast_pass:
            decisions.append({
                "id": email["id"], "label": fast_pass["label"], "star": False, "archive": False,
                "source": "prefilter",
            })
            continue

        if "notifications@github.com" in sender:
            outcome = _triage_github(body)
        elif "clientservices@cytonn.com" in sender:
            outcome = _triage_cytonn(subject)
        elif "linkedin.com" in sender:
            outcome = _triage_linkedin(subject)
        else:
            outcome = "ROUTE_TO_LLM"

        if outcome == "ROUTE_TO_LLM":
            remaining.append(email)
        else:
            decisions.append({"id": email["id"], "source": "prefilter", **outcome})

    return decisions, remaining


async def classify_batch(session: ClientSession, client: genai.Client, emails: list[dict]) -> list[dict]:
    """
    Send one batch of emails to Gemini and manually drive the tool-call loop:
    for every process_email call the model makes, log it, execute it against
    the MCP server, and feed the result back until the model stops calling tools.
    """
    tools_result = await session.list_tools()

    from google.genai import types
    gemini_tools = [
        types.Tool(function_declarations=[{
            "name": t.name,
            "description": t.description,
            "parameters": types.Schema.from_json_schema(json_schema=types.JSONSchema(**t.inputSchema)),
        }])
        for t in tools_result.tools
    ]

    compact = [
        {"id": e["id"], "sender": e["sender"], "subject": e["subject"], "snippet": e["snippet"]}
        for e in emails
    ]

    contents = [
        types.Content(role="user", parts=[
            types.Part.from_text(text=f"Emails to process:\n{json.dumps(compact, indent=2)}")
        ])
    ]

    config = types.GenerateContentConfig(
        system_instruction=build_system_prompt(),
        tools=gemini_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    decisions = []
    #explain this
    # Loop until the model stops requesting tool calls (bounded to avoid a runaway loop).
    for _ in range(len(emails) + 2):
        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=config,
        )

        candidate = response.candidates[0]
        function_calls = [
            part.function_call for part in candidate.content.parts if part.function_call
        ]

        if not function_calls:
            break

        contents.append(candidate.content)
        response_parts = []

        for call in function_calls:
            args = dict(call.args)
            decision = {
                "id": args.get("email_id"),
                "label": args.get("label"),
                "star": args.get("star", False),
                "archive": args.get("archive", False),
                "source": "ai",
            }
            decisions.append(decision)

            result = await session.call_tool(call.name, args)
            result_payload = {"status": "error", "detail": "no content"}
            if result.content:
                result_payload = json.loads(result.content[0].text)

            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result_payload)
            )

        contents.append(types.Content(role="user", parts=response_parts))

    return decisions


async def apply_decisions(session: ClientSession, decisions: list[dict]) -> None:
    """Execute a batch of already-made decisions against the MCP server."""
    for decision in decisions:
        await session.call_tool("process_email", {
            "email_id": decision["id"],
            "label": decision.get("label"),
            "star": decision.get("star", False),
            "archive": decision.get("archive", False),
        })


async def run():
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("-- Email triage bot starting --")
    service = get_gmail_service()

    print("Fetching unread emails...")
    emails = fetch_unread_emails(service)
    print(f"  Found {len(emails)} unread emails")

    if not emails:
        print("Nothing to process.")
        return

    snapshot_path = write_snapshot(emails, run_id)
    print(f"  Snapshot written: {snapshot_path}")
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", "gmail-mcp", "python", "gmail.py"],
    )

    prefilter_decisions, emails = prefilter(emails)
    print(f"  Prefiltered {len(prefilter_decisions)} emails, {len(emails)} left for the LLM")

    gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await apply_decisions(session, prefilter_decisions)
            llm_decisions = await classify_batch(session, gemini_client, emails) if emails else []

    decisions = prefilter_decisions + llm_decisions
    decisions_path = write_decisions(decisions, run_id)
    print(f"  Decisions written: {decisions_path}")
    print(f"-- Done: {len(decisions)} emails processed --")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
