"""Rule-based prefilter: resolves as many emails as possible before the LLM.

Rules come from prefilter_rules.py (see prefilter_rules_example.py).
"""

try:
    import prefilter_rules as rules
except ImportError:
    # No personal config yet — fall back to the shipped example so the bot
    # still runs. Copy prefilter_rules_example.py to prefilter_rules.py to
    # customize.
    import prefilter_rules_example as rules


def _rule_matches(sender: str, subject: str, rule: dict) -> bool:
    if "from" in rule and rule["from"] not in sender:
        return False
    if "domain" in rule and rule["domain"] not in sender:
        return False
    if "subject_contains" in rule and rule["subject_contains"] not in subject:
        return False
    return True


def _triage_github(body: str) -> dict | str:
    """Route GitHub notifications: human mentions go to the LLM, everything
    else (CI/CD runs, digests, generic activity) is machine noise."""
    if any(keyword in body.lower() for keyword in rules.GITHUB_MENTION_KEYWORDS):
        return "ROUTE_TO_LLM"
    return {"label": "dev-automated", "star": False, "archive": False}


def _triage_linkedin(subject: str) -> dict | str:
    """Route LinkedIn mail by subject: passive updates are archived silently,
    everything else (including human contact like messages/InMail/proposals)
    goes to the LLM to assess."""
    if any(keyword in subject for keyword in rules.LINKEDIN_ARCHIVE_SUBJECT_KEYWORDS):
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

        archived = next((r for r in rules.PRE_FILTER_ARCHIVE_LIST if _rule_matches(sender, subject, r)), None)
        if archived:
            decisions.append({
                "id": email["id"], "label": None, "star": False, "archive": True,
                "source": "prefilter",
            })
            continue

        fast_pass = next((r for r in rules.PRE_FILTER_FAST_PASS if _rule_matches(sender, subject, r)), None)
        if fast_pass:
            decisions.append({
                "id": email["id"], "label": fast_pass["label"], "star": False,
                "archive": fast_pass.get("archive", False),
                "source": "prefilter",
            })
            continue

        if "notifications@github.com" in sender:
            outcome = _triage_github(body)
        elif "linkedin.com" in sender:
            outcome = _triage_linkedin(subject)
        else:
            outcome = "ROUTE_TO_LLM"

        if outcome == "ROUTE_TO_LLM":
            remaining.append(email)
        else:
            decisions.append({"id": email["id"], "source": "prefilter", **outcome})

    return decisions, remaining
