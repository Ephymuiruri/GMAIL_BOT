"""Gmail authentication and fetching unread emails (client-side)."""

import base64
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

try:
    import prefilter_rules as rules
except ImportError:
    import prefilter_rules_example as rules

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "Credentials/credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "Credentials/token.json")

MAX_RESULTS = 50
BODY_SNIPPET_CHARS = 2000


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
    return any(needle in sender for needle in rules.DEEP_INSPECT_SENDERS)


def fetch_unread_emails(service, max_results: int = MAX_RESULTS) -> list[dict]:
    """Fetch unread emails, keeping every field we might reasonably need later.

    Most senders only need metadata + snippet for triage. A short list of
    senders (rules.DEEP_INSPECT_SENDERS) have prefilter rules that key off body
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
