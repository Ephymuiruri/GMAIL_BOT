import asyncio
import os

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

load_dotenv()

RETRYABLE_STATUS_CODES = {429, 503}
MAX_RETRIES = 5

mcp = FastMCP("GMAIL")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
]

CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS_PATH", "../Credentials/credentials.json")
TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "../Credentials/token.json")

_service = None


def get_gmail_service():
    """Authenticate and return a cached Gmail API service object."""
    global _service
    if _service is not None:
        return _service

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

    _service = build("gmail", "v1", credentials=creds)
    return _service


async def call_with_backoff(request):
    """Execute a Gmail API request, retrying on 429/503 with exponential backoff (1,2,4,8,16s)."""
    for attempt in range(MAX_RETRIES):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status not in RETRYABLE_STATUS_CODES or attempt == MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2 ** attempt)


async def get_or_create_label(service, label_name: str) -> str:
    """Get a label's ID by name, creating it if it doesn't exist."""
    existing = await call_with_backoff(service.users().labels().list(userId="me"))

    for existing_label in existing.get("labels", []):
        if existing_label["name"].lower() == label_name.lower():
            return existing_label["id"]

    created = await call_with_backoff(service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow"},
    ))
    return created["id"]


@mcp.tool()
async def process_email(
    email_id: str,
    label: str | None = None,
    star: bool = False,
    archive: bool = False,
) -> dict:
    """
    Apply one or more actions to a single email in one Gmail API call.

    Args:
        email_id: Gmail message ID to act on.
        label: Name of a label to apply (created if it doesn't exist). Omit to skip.
        star: If true, adds Gmail's STARRED label.
        archive: If true, removes the email from the inbox (removes INBOX label).
    """
    service = get_gmail_service()

    add_label_ids = []
    remove_label_ids = []

    try:
        if label:
            add_label_ids.append(await get_or_create_label(service, label))
        if star:
            add_label_ids.append("STARRED")
        if archive:
            remove_label_ids.append("INBOX")

        if not add_label_ids and not remove_label_ids:
            return {"status": "noop"}

        # Any triage action implies the email has been handled, so clear UNREAD too.
        remove_label_ids.append("UNREAD")

        await call_with_backoff(service.users().messages().modify(
            userId="me",
            id=email_id,
            body={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
        ))

        return {"status": "ok"}

    except HttpError as e:
        # All retries exhausted — park it under Unsorted instead of losing the action.
        unsorted_id = await get_or_create_label(service, "Unsorted")
        await call_with_backoff(service.users().messages().modify(
            userId="me",
            id=email_id,
            body={"addLabelIds": [unsorted_id], "removeLabelIds": ["UNREAD"]},
        ))
        return {"status": "error", "detail": str(e), "fallback": "Unsorted"}


if __name__ == "__main__":
    mcp.run()
