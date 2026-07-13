import re

from .models import Decision, Post, ResetStatus


PRODUCT = re.compile(r"\bcodex\b", re.I)
LIMIT = re.compile(
    r"\b(rate|usage)\s+limits?\b|\bquota\b|\bcodex\s+usage\b|"
    r"\bweekly\s+usage\b|\badditional\s+reset\b",
    re.I,
)
RESET = re.compile(
    r"\breset(?:s|ting)?\b|\breset\s+button\s+pressed\b|\bcredit\b.{0,40}\breset\b",
    re.I,
)
QUESTION = re.compile(
    r"\?|^\s*(?:who|what|when|where|why|how|do|does|did|is|are|was|were|"
    r"can|could|would|should|will|has|have)\b",
    re.I,
)
EXCLUDE = re.compile(
    r"\bshould\s+we\b|\bwould\s+you\b|\bif\s+we\b|\bplease\b|\bhope\b|"
    r"\b(?:have|has)\s+not\s+added\b|"
    r"\bnot\s+(?:been\s+)?reset\b|\bdo\s+not\s+reset\b|\bdon't\s+reset\b|"
    r"\bno\s+plans?\s+to\s+reset\b|\b(?:do\s+not|don't)\s+intend\s+to\s+reset\b|"
    r"\bwill\s+not\s+reset\b|\bwon't\s+reset\b|\breset\s+failed\b|\binvite\b|\breferr?al\b|"
    r"\bbuy\b|\bpurchase\b|\bwhen\s+do(?:es)?\b|"
    r"\b(?:every|each)\s+(?:hour|day|week|month|year|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday)s?\b|"
    r"\bon\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|"
    r"weekdays|weekends)\b|\b(?:daily|weekly|monthly|yearly|hourly|recurring)\s+reset\b|"
    r"\breset\s+bank\b|\breset\s+(?:feature|mechanism|policy)\b|"
    r"\bhow\b.{0,40}\breset\b.{0,40}\bworks\b",
    re.I,
)
COMPLETED = re.compile(
    r"\b(?:we|i)\s+reset\b|\bwe(?:'ve|’ve)\s+reset\b|"
    r"\bhave\s+(?:been\s+)?reset\b|\bhas\s+(?:been\s+)?reset\b|"
    r"\bwere\s+reset\b|\bare\s+reset\b|"
    r"\breset\s+button\s+pressed\b",
    re.I,
)
IN_PROGRESS = re.compile(r"\b(?:are|'re)\s+resetting\b|\bis\s+resetting\b", re.I)
PLANNED = re.compile(
    r"\bwill\s+(?:be\s+)?reset\b|\bwill\s+reset\b|"
    r"\bgive\s+us\b.{0,40}\bto\s+reset\b|"
    r"\badditional\s+reset\b|\bcredit\b.{0,40}\breset\b|"
    r"\breset\b.{0,40}\bwill\s+come\b",
    re.I,
)
BANKED_RESET = re.compile(r"\bbanked\s+reset\b", re.I)
BANKED_GRANTED = re.compile(
    r"\b(?:we|i)\s+have\s+added\b|\bwe(?:'ve|’ve)\s+added\b|"
    r"\bhas\s+added\b|\badded\s+(?:a|an|one|the)\s+banked\s+reset\b",
    re.I,
)


def classify(post: Post, trusted_handles: frozenset[str]) -> Decision:
    if post.author.casefold() not in trusted_handles:
        return Decision(False, None, "author_not_trusted")
    if post.is_retweet:
        return Decision(False, None, "retweet_not_original")

    text = post.text.strip()
    if QUESTION.search(text) or EXCLUDE.search(text):
        return Decision(False, None, "excluded_language", ("exclude",))

    checks = {
        "product": bool(PRODUCT.search(text)),
        "limit": bool(LIMIT.search(text)),
        "reset": bool(RESET.search(text)),
    }
    if not all(checks.values()):
        missing = ",".join(name for name, found in checks.items() if not found)
        return Decision(False, None, f"missing:{missing}")

    if BANKED_RESET.search(text) and BANKED_GRANTED.search(text):
        status = ResetStatus.BANKED_AVAILABLE
        state_rule = "banked_available"
    elif COMPLETED.search(text):
        status = ResetStatus.COMPLETED
        state_rule = "completed"
    elif IN_PROGRESS.search(text):
        status = ResetStatus.IN_PROGRESS
        state_rule = "in_progress"
    elif PLANNED.search(text):
        status = ResetStatus.PLANNED
        state_rule = "planned"
    else:
        return Decision(False, None, "reset_state_not_explicit")

    return Decision(
        True,
        status,
        (
            "explicit_codex_banked_reset"
            if status is ResetStatus.BANKED_AVAILABLE
            else "explicit_codex_limit_reset"
        ),
        ("product", "limit", "reset", state_rule),
    )
