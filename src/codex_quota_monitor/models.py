from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ResetStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    PLANNED = "planned"
    BANKED_AVAILABLE = "banked_available"
    POSSIBLE_RESET = "possible_reset"


class Confidence(StrEnum):
    HIGH = "high"
    LOW = "low"


class HealthTransition(StrEnum):
    NONE = "none"
    ALERT = "alert"
    RECOVERED = "recovered"


@dataclass(frozen=True)
class Source:
    handle: str
    include_replies: bool = True


@dataclass(frozen=True)
class Post:
    post_id: str
    author: str
    text: str
    published_at: datetime
    url: str
    quoted_text: str = ""
    quoted_author: str = ""
    is_retweet: bool = False


@dataclass(frozen=True)
class Decision:
    matched: bool
    status: ResetStatus | None
    reason: str
    matched_rules: tuple[str, ...] = ()
    confidence: Confidence = Confidence.HIGH
    candidate_reason: tuple[str, ...] = ()


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    template: str
    url: str = ""
