"""
MCP client + orchestrator.

Connects to Gmail directly (to fetch emails), starts the gmail-mcp server as
a subprocess and talks to it over MCP (to execute actions), and drives the
tool-calling conversation with Gemini. This file owns the whole run: fetch ->
snapshot -> prefilter -> classify -> act -> log. The actual work for each
step lives in triage/.
"""

import os
from datetime import datetime

from dotenv import load_dotenv
from google import genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from triage.gmail_auth import fetch_unread_emails, get_gmail_service
from triage.llm import classify_batch
from triage.mcp_actions import apply_decisions
from triage.prefilter import prefilter
from triage.run_log import write_decisions, write_snapshot

load_dotenv()


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
            llm_decisions = await classify_batch(session, gemini_client, emails,run_id) if emails else []

    decisions = prefilter_decisions + llm_decisions
    decisions_path = write_decisions(decisions, run_id)
    print(f"  Decisions written: {decisions_path}")
    print(f"-- Done: {len(decisions)} emails processed --")


if __name__ == "__main__":
    import asyncio
    asyncio.run(run())
