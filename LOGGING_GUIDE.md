# Logging Guide for MCP Email Triage

## What Logs Are Generated?

Two log files are created when you run the triage system:

1. **`triage.log`** — Client-side logs from the LLM processing (`triage/llm.py`)
2. **`mcp_server.log`** — Server-side logs from Gmail API calls (`gmail-mcp/gmail.py`)

## Running Your Triage

When you run your main triage script, open two terminals:

**Terminal 1** (watch client logs):
```bash
cd /home/ew/Dev/code/Personal/GMAIL_BOT
tail -f triage.log
```

**Terminal 2** (watch server logs):
```bash
cd /home/ew/Dev/code/Personal/GMAIL_BOT/gmail-mcp
tail -f mcp_server.log
```

**Terminal 3** (run the actual triage):
```bash
cd /home/ew/Dev/code/Personal/GMAIL_BOT
python -m triage.run_log  # or whatever your main script is
```

## What Each Log Shows

### Client Log (`triage.log`)

Shows the MCP process from the LLM client's perspective:

```
2025-01-15 10:30:45,123 - INFO - === BATCH START ===
2025-01-15 10:30:45,124 - INFO - Processing 5 emails
2025-01-15 10:30:45,201 - INFO - MCP Server has 1 tools: ['process_email']
2025-01-15 10:30:45,202 - DEBUG - Translating MCP schemas to Gemini format
2025-01-15 10:30:45,203 - DEBUG - Translated 1 tools for Gemini
2025-01-15 10:30:45,204 - DEBUG - Prepared 5 compact email summaries
2025-01-15 10:30:45,205 - DEBUG -   Email: msg_123 | alice@example.com | Project Update
2025-01-15 10:30:45,206 - DEBUG -   Email: msg_456 | bob@example.com | Meeting Reminder
...
2025-01-15 10:30:45,500 - INFO - --- ITERATION 1 ---
2025-01-15 10:30:45,501 - INFO - Sending request to Gemini with 1 content pieces
2025-01-15 10:30:46,234 - DEBUG - Received response from Gemini
2025-01-15 10:30:46,235 - INFO - Gemini made 2 tool call(s)
2025-01-15 10:30:46,236 - INFO - Tool call 1: process_email({'email_id': 'msg_123', 'label': 'opportunity', 'star': True})
2025-01-15 10:30:46,237 - DEBUG - Stored decision: {'id': 'msg_123', 'label': 'opportunity', 'star': True, ...}
2025-01-15 10:30:46,238 - DEBUG - Executing tool call against MCP server
2025-01-15 10:30:46,500 - DEBUG - MCP server returned: ClientToolResult(...)
2025-01-15 10:30:46,501 - INFO - Tool result: {'status': 'ok'}
...
```

**What to look for:**
- `Gemini made X tool call(s)` — Did Gemini make the right decision?
- `Tool result: {'status': 'ok'}` — Did the Gmail API call succeed?
- `No tool calls. Loop complete.` — When the batch finishes

### Server Log (`mcp_server.log`)

Shows what's happening in the Gmail API layer:

```
2025-01-15 10:30:45,150 - INFO - Starting MCP server
2025-01-15 10:30:46,238 - INFO - process_email called: email_id=msg_123, label=opportunity, star=True, archive=False
2025-01-15 10:30:46,239 - DEBUG - Returning cached Gmail service
2025-01-15 10:30:46,240 - DEBUG - Getting or creating label: opportunity
2025-01-15 10:30:46,350 - DEBUG - Building modification: add=['LabelId_xyz', 'STARRED'], remove=['UNREAD']
2025-01-15 10:30:46,351 - DEBUG - Executing API request (attempt 1/5)
2025-01-15 10:30:46,450 - INFO - Successfully processed email msg_123
2025-01-15 10:30:46,600 - INFO - process_email called: email_id=msg_456, label=None, star=False, archive=True
...
```

**What to look for:**
- `Executing API request (attempt X/5)` — Retries on rate limiting
- `Got 429, retrying in 1s` — Gmail is rate-limiting you
- `Successfully processed email` — The Gmail API call succeeded
- `Failed to process email X: ERROR. Using fallback to Unsorted.` — Error handling

## Common Issues

### No tool calls from Gemini
Look at the first request in `triage.log`:
```
2025-01-15 10:30:46,234 - DEBUG - Received response from Gemini
2025-01-15 10:30:46,235 - INFO - Gemini made 0 tool call(s)
```
Gemini received the batch but decided not to act. Check:
- Is your `preferences.txt` valid?
- Are the emails reaching Gemini properly?

### Gmail API failures
In `mcp_server.log`, look for:
```
2025-01-15 10:30:46,351 - ERROR - API request failed with status 403: ...
```
- 403 = Permission error. Check Gmail scopes.
- 429 = Rate limited. The code retries with backoff.
- 500 = Gmail service issue. Retry later.

### Mismatch between decision and Gmail action
Compare the two logs:

`triage.log`:
```
Tool call 1: process_email({'email_id': 'msg_123', 'label': 'opportunity', 'star': True})
```

`mcp_server.log`:
```
process_email called: email_id=msg_123, label=opportunity, star=True, archive=False
Building modification: add=['LabelId_xyz', 'STARRED'], remove=['UNREAD']
```

If these don't match, you found a bug!

## Clearing Old Logs

Before each test run:
```bash
rm triage.log mcp_server.log
```

Or keep them and search by timestamp:
```bash
grep "2025-01-15 10:35" triage.log  # Logs from a specific time
grep "ERROR" mcp_server.log         # All errors
grep "process_email called" mcp_server.log | head -20  # First 20 tool calls
```

## Log Levels Explained

| Level | Used For | Example |
|-------|----------|---------|
| INFO | High-level flow events | "Processing 5 emails", "Gemini made 2 tool calls", "Successfully processed email" |
| DEBUG | Detailed step-by-step info | "Translated 1 tools for Gemini", "MCP server returned: ..." |
| WARNING | Retries and non-fatal issues | "Got 429, retrying in 2s" |
| ERROR | Failures and exceptions | "API request failed with status 403" |

Change log level in the code:
```python
logging.basicConfig(
    filename='triage.log',
    level=logging.DEBUG,  # Change to logging.INFO for less noise
    ...
)
```
