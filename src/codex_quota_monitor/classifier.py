import re

from .models import Confidence, Decision, Post, ResetStatus


CLASSIFIER_VERSION = "3"


def normalize(text: str) -> str:
    """Normalize user-facing post text without joining distant phrases."""

    return re.sub(r"\s+", " ", text.casefold().replace("’", "'")).strip()


def _patterns(
    *items: tuple[str, str],
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((name, re.compile(pattern, re.I)) for name, pattern in items)


# These public names are kept for compatibility with the original classifier.
PRODUCT = re.compile(r"\bcodex\b|\bchatgpt\s+work\b", re.I)
LIMIT = re.compile(
    r"\b(?:rate|usage)\s+limits?\b|\bquota\b|\bweekly\s+usage\b|"
    r"\badditional\s+reset\b|\bbanked\s+reset\b",
    re.I,
)
RESET = re.compile(r"\breset(?:s|ting|ed)?\b", re.I)
QUESTION = re.compile(
    r"\?|^\s*(?:who|what|when|where|why|how|do|does|did|is|are|was|were|"
    r"can|could|would)\b|\bshould\s+(?:we|i|you)\b",
    re.I,
)
EXCLUDE = re.compile(
    r"\bshould\s+we\b|\bwould\s+you\b|\bif\s+we\b|\bplease\b|\bhope\b|"
    r"\b(?:have|has)\s+not\s+added\b|\bnot\s+(?:been\s+)?reset\b|"
    r"\bdo\s+not\s+reset\b|\bdon't\s+reset\b|"
    r"\bno\s+plans?\s+to\s+reset\b|"
    r"\b(?:do\s+not|don't)\s+intend\s+to\s+reset\b|"
    r"\bwill\s+not\s+reset\b|\bwon't\s+reset\b|"
    r"\breset\s+failed\b|\binvite\b|\breferr?al\b|\bbuy\b|\bpurchase\b|"
    r"\bwhen\s+do(?:es)?\b|\b(?:every|each)\s+(?:hour|day|week|month|year|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b|"
    r"\bon\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|"
    r"sundays|weekdays|weekends)\b|"
    r"\b(?:daily|weekly|monthly|yearly|hourly|recurring)\s+reset\b|"
    r"\breset\s+bank\b|\breset\s+(?:feature|mechanism|policy)\b|"
    r"\bhow\b.{0,60}\breset\b.{0,60}\bworks\b",
    re.I,
)


_PRODUCT_PATTERNS = _patterns(
    ("codex", r"\bcodex\b"),
    ("chatgpt_work", r"\bchatgpt\s+work\b"),
)
_LIMIT_PATTERNS = _patterns(
    ("rate_limits", r"\b(?:rate|usage)\s+limits?\b"),
    ("weekly_usage", r"\bweekly\s+usage\b"),
    ("quota", r"\bquota\b"),
    ("additional_reset", r"\badditional\s+reset\b"),
    ("banked_reset", r"\bbanked\s+reset\b"),
)
_ACTION_PATTERNS = _patterns(
    ("reset", r"\breset(?:s|ting|ed)?\b"),
    ("restore", r"\brestore(?:d|s|ing)?\b"),
    ("replenish", r"\breplenish(?:ed|es|ing)?\b"),
    ("refill", r"\brefill(?:ed|s|ing)?\b"),
    ("back_to_100", r"\bback\s+to\s+100%|\b100%\s+weekly\s+usage\b"),
    ("usage_back", r"\b(?:usage|limit|limits)\s+back\b"),
    ("credit", r"\bcredit\b.{0,24}\b(?:reset|usage|limit|quota)\b"),
)
_RECOVERY_PATTERNS = _patterns(
    ("restored", r"\brestore(?:d|s|ing)?\b"),
    ("replenished", r"\breplenish(?:ed|es|ing)?\b"),
    ("refilled", r"\brefill(?:ed|s|ing)?\b"),
    ("back_to_100", r"\bback\s+to\s+100%|\b100%\s+weekly\s+usage\b"),
    ("usage_back", r"\b(?:usage|limit|limits)\s+back\b"),
)
_TIME_PATTERNS = _patterns(
    ("now", r"\bnow\b"),
    ("currently", r"\bcurrently\b"),
    ("again", r"\bagain\b|\bonce\s+again\b"),
    ("coming", r"\bcoming\b"),
    ("soon", r"\bsoon\b|\bshortly\b"),
    (
        "relative_time",
        r"\b(?:today|tomorrow|later|in\s+(?:a\s+few|several|\d+)\s+"
        r"(?:minutes?|hours?|days?)|(?:a\s+few|several|\d+)\s+"
        r"(?:minutes?|hours?|days?))\b",
    ),
    (
        "should_have_back",
        r"\bshould\s+have\b.{0,80}\bback\b",
    ),
    (
        "will_change",
        r"\bwill\b.{0,80}\b(?:reset|restore|replenish|back)\b",
    ),
)

BANKED_RESET = re.compile(r"\bbanked\s+reset\b", re.I)
BANKED_GRANTED = re.compile(
    r"\b(?:we|i)\s+have\s+added\b|\bwe(?:'ve|’ve)\s+added\b|"
    r"\bhas\s+added\b|\badded\s+(?:a|an|one|the)\s+banked\s+reset\b",
    re.I,
)
COMPLETED = re.compile(
    r"\b(?:we|i)\s+(?:have\s+)?reset\b|"
    r"\bwe(?:'ve|’ve)\s+reset\b|"
    r"\b(?:has|have|had|were|are|is)\s+(?:been\s+)?reset\b|"
    r"\breset\s+button\s+pressed\b|\bback\s+to\s+100%|"
    r"\brestore(?:d|s)?\b|\breplenish(?:ed|es)?\b|\brefill(?:ed|s)?\b",
    re.I,
)
IN_PROGRESS = re.compile(
    r"\b(?:are|is|am|'re)\s+(?:once\s+again\s+)?resetting\b|"
    r"\bresetting\b.{0,20}\bnow\b",
    re.I,
)
PLANNED = re.compile(
    r"\bwill\b.{0,80}\b(?:reset|restore|replenish|back)\b|"
    r"\bgive\s+us\b.{0,60}\bto\s+reset\b|"
    r"\b(?:coming|today|tomorrow|later|soon|shortly)\b|"
    r"\bin\s+(?:a\s+few|several|\d+)\s+(?:minutes?|hours?|days?)\b|"
    r"\bshould\s+have\b.{0,80}\bback\b",
    re.I,
)
_UNCERTAIN = re.compile(
    r"\b(?:may|might|could|possibly|perhaps|expected|expect|should)\b",
    re.I,
)

_EXCLUSION_PATTERNS = _patterns(
    ("question", r"\?|^\s*(?:who|what|when|where|why|how|do|does|did|is|are|was|were|can|could|would)\b|\bshould\s+(?:we|i|you)\b"),
    ("negation", r"\b(?:will|would|do|does|did|have|has|are|is|was|were)\s+not\s+(?:be\s+)?(?:reset|added|restored)\b|\b(?:don't|do\s+not|won't|will\s+not)\s+(?:intend\s+to\s+)?reset\b|\bno\s+plans?\s+to\s+reset\b|\b(?:have|has)\s+not\s+added\b|\bnot\s+(?:been\s+)?reset\b"),
    ("condition", r"\bif\s+we\b|\bplease\b|\bhope\b|\bwould\s+you\b"),
    ("failure", r"\breset\s+failed\b|\bfailed\s+to\s+reset\b|\bunable\s+to\s+reset\b"),
    ("promotion", r"\binvite\b|\breferr?al\b|\bbuy\b|\bpurchase\b|\bget\b.{0,40}\breset\b.{0,40}\bfriend\b"),
    ("mechanism", r"\b(?:reset\s+bank|reset\s+(?:feature|mechanism|policy)|mechanism|tutorial|guide)\b|\b(?:how|ways?)\b.{0,60}\breset\b|\b(?:every|each)\s+(?:hour|day|week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b|\bon\s+(?:mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays|weekdays|weekends)\b|\b(?:daily|weekly|monthly|yearly|hourly|recurring)\s+reset\b"),
)


def _collect(
    text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> tuple[str, ...]:
    return tuple(name for name, pattern in patterns if pattern.search(text))


def collect_evidence(text: str) -> dict[str, tuple[str, ...]]:
    normalized = normalize(text)
    return {
        "product": _collect(normalized, _PRODUCT_PATTERNS),
        "limit": _collect(normalized, _LIMIT_PATTERNS),
        "action": _collect(normalized, _ACTION_PATTERNS),
        "recovery": _collect(normalized, _RECOVERY_PATTERNS),
        "time": _collect(normalized, _TIME_PATTERNS),
        "banked": ("banked_reset",) if BANKED_RESET.search(normalized) else (),
        "grant": ("grant",) if BANKED_GRANTED.search(normalized) else (),
    }


def hard_exclusion(text: str) -> tuple[str, ...] | None:
    normalized = normalize(text)
    exclusions = _collect(normalized, _EXCLUSION_PATTERNS)
    return exclusions or None


def is_candidate(evidence: dict[str, tuple[str, ...]]) -> bool:
    product = bool(evidence.get("product"))
    limit = bool(evidence.get("limit"))
    action = bool(evidence.get("action"))
    time_or_recovery = bool(evidence.get("time") or evidence.get("recovery"))
    return product and action and (limit or time_or_recovery)


def infer_state(
    text: str, evidence: dict[str, tuple[str, ...]]
) -> ResetStatus:
    normalized = normalize(text)
    if evidence.get("banked") and evidence.get("grant"):
        return ResetStatus.BANKED_AVAILABLE
    if not _UNCERTAIN.search(normalized) and COMPLETED.search(normalized):
        return ResetStatus.COMPLETED
    if IN_PROGRESS.search(normalized):
        return ResetStatus.IN_PROGRESS
    if PLANNED.search(normalized):
        return ResetStatus.PLANNED
    return ResetStatus.POSSIBLE_RESET


def _candidate_reason(evidence: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(
        name
        for name in ("product", "limit", "action", "time", "recovery", "banked", "grant")
        if evidence.get(name)
    )


def _matched_rules(
    evidence: dict[str, tuple[str, ...]], status: ResetStatus
) -> tuple[str, ...]:
    rules: list[str] = []
    if evidence.get("product"):
        rules.append("product")
    if evidence.get("limit"):
        rules.append("limit")
    if evidence.get("action"):
        # Keep the original public rule name for backwards compatibility.
        rules.append("reset")
    rules.append(
        {
            ResetStatus.BANKED_AVAILABLE: "banked_available",
            ResetStatus.COMPLETED: "completed",
            ResetStatus.IN_PROGRESS: "in_progress",
            ResetStatus.PLANNED: "planned",
            ResetStatus.POSSIBLE_RESET: "possible_reset",
        }[status]
    )
    return tuple(rules)


def classify(post: Post, trusted_handles: frozenset[str]) -> Decision:
    if post.author.casefold() not in trusted_handles:
        return Decision(False, None, "author_not_trusted")
    if post.is_retweet:
        return Decision(False, None, "retweet_not_original")

    normalized = normalize(post.text)
    exclusions = hard_exclusion(normalized)
    if exclusions:
        return Decision(
            False,
            None,
            "excluded_language",
            ("exclude",),
            candidate_reason=exclusions,
        )

    evidence = collect_evidence(normalized)
    if not is_candidate(evidence):
        missing = [
            name
            for name in ("product", "limit", "action")
            if not evidence.get(name)
        ]
        if evidence.get("product") and evidence.get("action") and not (
            evidence.get("limit")
            or evidence.get("time")
            or evidence.get("recovery")
        ):
            missing.append("time_or_recovery")
        return Decision(
            False,
            None,
            f"missing:{','.join(missing)}",
            candidate_reason=_candidate_reason(evidence),
        )

    status = infer_state(normalized, evidence)
    confidence = (
        Confidence.LOW
        if status is ResetStatus.POSSIBLE_RESET
        else Confidence.HIGH
    )
    reason = (
        "possible_codex_limit_reset"
        if confidence is Confidence.LOW
        else (
            "explicit_codex_banked_reset"
            if status is ResetStatus.BANKED_AVAILABLE
            else "explicit_codex_limit_reset"
        )
    )
    return Decision(
        True,
        status,
        reason,
        _matched_rules(evidence, status),
        confidence,
        _candidate_reason(evidence),
    )
