"""Executing triage decisions against the Gmail MCP server."""

from mcp import ClientSession


async def apply_decisions(session: ClientSession, decisions: list[dict]) -> None:
    """Execute a batch of already-made decisions against the MCP server."""
    for decision in decisions:
        await session.call_tool("process_email", {
            "email_id": decision["id"],
            "label": decision.get("label"),
            "star": decision.get("star", False),
            "archive": decision.get("archive", False),
        })
