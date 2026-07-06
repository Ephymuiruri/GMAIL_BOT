"""LLM classification: system prompt construction and the Gemini tool-call loop."""

import json
import logging
import os

from google import genai
from mcp import ClientSession

logging.basicConfig(
    filename='triage.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
AI_LOGS = "logs/AI"
GEMINI_MODEL = "gemini-3.5-flash"


def build_system_prompt() -> str:
    try:
        with open("preferences.txt", "r") as f:
            preferences = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(
            "preferences.txt not found. Copy preferences.example.txt to "
            "preferences.txt and edit it to describe yourself."
        )

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


async def classify_batch(session: ClientSession, client: genai.Client, emails: list[dict],run_id:str) -> list[dict]:
    """
    Send one batch of emails to Gemini and manually drive the tool-call loop:
    for every process_email call the model makes, log it, execute it against
    the MCP server, and feed the result back until the model stops calling tools.
    """
    logger.info(f"=== BATCH START ===")
    logger.info(f"Processing {len(emails)} emails")

    tools_result = await session.list_tools()
    logger.info(f"MCP Server has {len(tools_result.tools)} tools: {[t.name for t in tools_result.tools]}")

    from google.genai import types
    logger.debug(f"Translating MCP schemas to Gemini format")
    gemini_tools = [
        types.Tool(function_declarations=[{
            "name": t.name,
            "description": t.description,
            "parameters_json_schema": t.inputSchema,
        }])
        for t in tools_result.tools
    ]
    logger.debug(f"Translated {len(gemini_tools)} tools for Gemini")
    
    compact = [
        {"id": e["id"], "sender": e["sender"], "subject": e["subject"], "snippet": e["snippet"]}
        for e in emails
    ]
    logger.debug(f"Prepared {len(compact)} compact email summaries")
    for email in compact:
        logger.debug(f"  Email: {email['id']} | {email['sender']} | {email['subject'][:50]}")

    contents = [
        types.Content(role="user", parts=[
            types.Part.from_text(text=f"Emails to process:\n{json.dumps(compact, indent=2)}")
        ])
    ]
    logger.debug(f"Initial contents prepared for Gemini")

    config = types.GenerateContentConfig(
        system_instruction=build_system_prompt(),
        tools=gemini_tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    decisions = []
    total_tokens = {"prompt": 0, "output": 0, "cached": 0}
    safety_ratings_collected = []
    block_reason = None
    block_reason_message = None
    # Loop until the model stops requesting tool calls (bounded to avoid a runaway loop).
    for iteration in range(len(emails) + 2):
        logger.info(f"--- ITERATION {iteration + 1} ---")
        logger.info(f"Sending request to Gemini with {len(contents)} content pieces")

        response = client.models.generate_content(
            model=GEMINI_MODEL, contents=contents, config=config,
        )
        logger.debug(f"Received response from Gemini")

        if response.usage_metadata:
            logger.info(
                f"Token usage — prompt: {response.usage_metadata.prompt_token_count}, "
                f"output: {response.usage_metadata.candidates_token_count}, "
                f"cached: {response.usage_metadata.cached_content_token_count or 0}, "
                f"total: {response.usage_metadata.total_token_count}"
            )
            total_tokens["prompt"] += response.usage_metadata.prompt_token_count or 0
            total_tokens["output"] += response.usage_metadata.candidates_token_count or 0
            total_tokens["cached"] += response.usage_metadata.cached_content_token_count or 0

        if response.prompt_feedback:
            if response.prompt_feedback.safety_ratings:
                for rating in response.prompt_feedback.safety_ratings:
                    if rating.blocked or (rating.probability_score and rating.probability_score > 0.5):
                        logger.warning(
                            f"Safety signal: {rating.category} | blocked={rating.blocked} | "
                            f"probability={rating.probability} | score={rating.probability_score}"
                        )
                        safety_ratings_collected.append({
                            "category": str(rating.category),
                            "blocked": rating.blocked,
                            "probability_score": rating.probability_score,
                        })
            if response.prompt_feedback.block_reason:
                logger.warning(f"Response blocked: {response.prompt_feedback.block_reason} — {response.prompt_feedback.block_reason_message}")
                block_reason = str(response.prompt_feedback.block_reason)
                block_reason_message = response.prompt_feedback.block_reason_message

        candidate = response.candidates[0]
        function_calls = [
            part.function_call for part in candidate.content.parts if part.function_call
        ]

        logger.info(f"Gemini made {len(function_calls)} tool call(s)")

        if not function_calls:
            logger.info(f"No tool calls. Loop complete.")
            break

        contents.append(candidate.content)
        logger.debug(f"Added Gemini's response to conversation history")
        response_parts = []

        for i, call in enumerate(function_calls):
            args = dict(call.args)
            logger.info(f"Tool call {i + 1}: {call.name}({args})")

            decision = {
                "id": args.get("email_id"),
                "label": args.get("label"),
                "star": args.get("star", False),
                "archive": args.get("archive", False),
                "source": "ai",
            }
            decisions.append(decision)
            logger.debug(f"Stored decision: {decision}")

            logger.debug(f"Executing tool call against MCP server")
            result = await session.call_tool(call.name, args)
            logger.debug(f"MCP server returned: {result}")

            result_payload = {"status": "error", "detail": "no content"}
            if result.content:
                result_payload = json.loads(result.content[0].text)
                logger.info(f"Tool result: {result_payload}")
            else:
                logger.warning(f"Tool returned empty content")

            response_parts.append(
                types.Part.from_function_response(name=call.name, response=result_payload)
            )
            logger.debug(f"Added tool result to response parts")

        contents.append(types.Content(role="user", parts=response_parts))
        logger.debug(f"Appended tool results to conversation history (now {len(contents)} pieces)")

    logger.info(f"=== BATCH COMPLETE ===")
    logger.info(f"Total decisions made: {len(decisions)}")
    logger.info(
        f"Total token usage — prompt: {total_tokens['prompt']}, output: {total_tokens['output']}, "
        f"cached: {total_tokens['cached']}, combined: {total_tokens['prompt'] + total_tokens['output']}"
    )

    valid_ids = {e["id"] for e in emails}
    for d in decisions:
        if d["id"] not in valid_ids:
            logger.error(f"HALLUCINATION DETECTED: Decision references unknown email ID {d['id']}")
        logger.info(f"Decision: {d['id']} → label={d['label']}, star={d['star']}, archive={d['archive']}")

    # Write this run's details to the cumulative history file
    log_run_details(total_tokens, safety_ratings_collected, block_reason, block_reason_message, run_id)

    return decisions

def log_run_details(total_tokens, safety_ratings, block_reason, block_reason_message, run_id):
    """Append run details to a cumulative JSON file tracking all runs."""
    os.makedirs(AI_LOGS, exist_ok=True)
    path = os.path.join(AI_LOGS, "run_history.json")

    run_entry = {
        "run_id": run_id,
        "total_tokens": total_tokens,
        "safety_ratings": safety_ratings,
        "blocked": block_reason is not None,
        "block_reason": block_reason,
        "block_reason_message": block_reason_message,
    }

    # Load existing history or start fresh
    history = []
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = []

    # Append this run
    history.append(run_entry)

    # Write back all history
    with open(path, "w") as f:
        json.dump(history, f, indent=2)

