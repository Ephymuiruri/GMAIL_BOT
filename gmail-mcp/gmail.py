import os

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

load_dotenv()

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


def get_or_create_label(service, label_name: str) -> str:
    """Get a label's ID by name, creating it if it doesn't exist."""
    existing = service.users().labels().list(userId="me").execute()

    for existing_label in existing.get("labels", []):
        if existing_label["name"].lower() == label_name.lower():
            return existing_label["id"]

    created = service.users().labels().create(
        userId="me",
        body={"name": label_name, "labelListVisibility": "labelShow"},
    ).execute()
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

    if label:
        add_label_ids.append(get_or_create_label(service, label))
    if star:
        add_label_ids.append("STARRED")
    if archive:
        remove_label_ids.append("INBOX")

    if not add_label_ids and not remove_label_ids:
        return {"status": "noop"}

    service.users().messages().modify(
        userId="me",
        id=email_id,
        body={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
    ).execute()

    return {"status": "ok"}


if __name__ == "__main__":
    mcp.run()
