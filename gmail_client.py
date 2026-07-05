"""
MCP client + orchestrator.

Connects to Gmail directly (to fetch emails), starts the gmail-mcp server as
a subprocess and talks to it over MCP (to execute actions), and drives the
tool-calling conversation with Gemini. This file owns the whole run: fetch ->
snapshot -> classify -> act -> log.
"""

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
MAX_RESULTS = 5
LOGS_DIR = "logs"


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


def fetch_unread_emails(service, max_results: int = MAX_RESULTS) -> list[dict]:
    """Fetch unread emails, keeping every field we might reasonably need later."""
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

        emails.append({
            "id": raw["id"],
            "thread_id": raw["threadId"],
            "sender": headers.get("From", "unknown"),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "snippet": raw.get("snippet", ""),
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
#explain this step
    server_params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", "gmail-mcp", "python", "gmail.py"],
    )

    gemini_client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
#explain this part
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            decisions = await classify_batch(session, gemini_client, emails)

    decisions_path = write_decisions(decisions, run_id)
    print(f"  Decisions written: {decisions_path}")
    print(f"-- Done: {len(decisions)} emails processed --")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
