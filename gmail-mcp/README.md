# gmail-mcp

MCP server exposing Gmail actions as a single tool. Started as a subprocess
by `gmail_client.py` — see the [root README](../README.md) for setup and
usage.

## Tool

- `process_email(email_id, label=None, star=False, archive=False)` — applies
  one or more actions to a message in one Gmail API call. Any action also
  clears `UNREAD`. Retries on HTTP 429/503 with exponential backoff; if all
  retries fail, the message is filed under an `Unsorted` label instead of
  the action being silently dropped.

## Run standalone

```bash
uv sync
uv run python gmail.py
```

Requires `Credentials/credentials.json` and the env vars documented in the
root `.env.example` (paths are relative to this directory by default).
