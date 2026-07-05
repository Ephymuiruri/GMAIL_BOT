"""
Personal prefilter rules — copy this file to prefilter_rules.py and edit it.

These rules run before the LLM and skip it entirely for senders you already
know how to handle. Anything that doesn't match a rule here still goes to
Gemini for classification.
"""

# Senders archived outright, no LLM involvement.
# Match by exact "from" address and/or a required "subject_contains" substring.
PRE_FILTER_ARCHIVE_LIST = [
    {"from": "unread-messages@mail.instagram.com"},
    {"from": "informational@email.snapchat.com"},
    {"from": "no-reply@twitch.tv"},
    # Example: only archive if the subject also matches.
    # {"from": "no-reply@todoist.com", "subject_contains": "task(s) for"},
]

# Senders fast-tracked straight to a label, no LLM involvement.
# Match by "from" (exact address) or "domain" (substring match).
PRE_FILTER_FAST_PASS = [
    # {"domain": "your-employer.com", "label": "urgent"},
    # {"domain": "your-university.edu", "label": "urgent"},
    # {"from": "jobs@some-platform.com", "label": "opportunity"},
]

# Senders that need their email body inspected (not just subject/snippet) for
# the built-in GitHub/LinkedIn rules below to work. Add any sender here whose
# rule reads `body` rather than just `subject`.
DEEP_INSPECT_SENDERS = [
    "notifications@github.com",
]

# Phrases in a GitHub notification body that mean a human actually needs your
# attention (as opposed to an automated CI/CD run or digest). Include your own
# GitHub @handle so direct mentions are caught.
GITHUB_MENTION_KEYWORDS = [
    "mentioned you",
    "requested your review",
    "assigned you",
    # "@your-github-handle",
]

# Subject substrings that mean a LinkedIn email is passive noise and can be
# archived without LLM review (likes, connection suggestions, congratulations
# posts, etc). Anything else from LinkedIn (messages, InMail, proposals) is
# left for the LLM to assess.
LINKEDIN_ARCHIVE_SUBJECT_KEYWORDS = ["add you", "congratulations", "skills", "news"]
