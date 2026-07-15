from datetime import UTC, datetime

import pytest

from codex_quota_monitor.classifier import classify
from codex_quota_monitor.models import Post, ResetStatus


TRUSTED = frozenset({"openai", "openaidevs", "thsottiaux", "sama"})


def post(
    text: str,
    author: str = "thsottiaux",
    *,
    quoted_text: str = "",
    quoted_author: str = "",
    is_retweet: bool = False,
) -> Post:
    return Post(
        "1",
        author,
        text,
        datetime(2026, 6, 16, tzinfo=UTC),
        "https://x.com/x/status/1",
        quoted_text=quoted_text,
        quoted_author=quoted_author,
        is_retweet=is_retweet,
    )


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("We have reset Codex usage limits across all plans.", ResetStatus.COMPLETED),
        ("We are resetting the Codex rate limits now.", ResetStatus.IN_PROGRESS),
        (
            "Hello. We have reached 8M active users across Codex and ChatGPT "
            "Work. We are once again resetting the usage limits for all.",
            ResetStatus.IN_PROGRESS,
        ),
        ("Give us 24 hours to reset the Codex rate limits.", ResetStatus.PLANNED),
        ("We will credit Codex users one additional reset.", ResetStatus.PLANNED),
        ("Codex usage limits have been reset.", ResetStatus.COMPLETED),
        ("We've reset Codex usage limits.", ResetStatus.COMPLETED),
        (
            "Another Codex usage limits reset will come tomorrow.",
            ResetStatus.PLANNED,
        ),
    ],
)
def test_matches_explicit_reset_announcements(text: str, status: ResetStatus) -> None:
    assert classify(post(text), TRUSTED).status is status


def test_matches_explicit_simple_past_reset_announcement() -> None:
    decision = classify(post("We reset Codex rate limits for everyone."), TRUSTED)

    assert decision.status is ResetStatus.COMPLETED


def test_matches_explicit_banked_reset_grant_as_distinct_status() -> None:
    text = (
        "Thank you to the 7M active users who are now using Codex and ChatGPT "
        "Work. We have added a banked reset to everyone's account to celebrate "
        "the milestone. You can apply the reset in the desktop app or on web and "
        "it will replenish the weekly usage for you. Have fun out there."
    )

    decision = classify(post(text), TRUSTED)

    assert decision.matched is True
    assert decision.status is not None
    assert decision.status.value == "banked_available"
    assert decision.reason == "explicit_codex_banked_reset"
    assert decision.matched_rules == (
        "product",
        "limit",
        "reset",
        "banked_available",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Should we reset Codex usage limits?",
        "Please reset Codex rate limits.",
        "Codex rate limits were not reset.",
        "We will not reset Codex usage limits today.",
        "Invite a friend to add another Codex reset to your bank.",
        "Codex now has higher rate limits.",
        "Codex is stable again after capacity errors.",
    ],
)
def test_rejects_questions_negations_conditions_and_unrelated_news(text: str) -> None:
    assert classify(post(text), TRUSTED).matched is False


@pytest.mark.parametrize(
    "text",
    [
        "Did we reset Codex usage limits?",
        "We have no plans to reset Codex usage limits.",
        "How to reset Codex usage limits.",
        "A guide to reset Codex usage limits.",
        "We don't intend to reset Codex usage limits.",
    ],
)
def test_rejects_ambiguous_non_announcements(text: str) -> None:
    assert classify(post(text), TRUSTED).matched is False


def test_rejects_untrusted_author() -> None:
    assert classify(post("We reset Codex usage limits", "randomuser"), TRUSTED).matched is False


def test_rejects_retweet_even_when_trusted_account_reposts_explicit_reset() -> None:
    decision = classify(
        post(
            "RT @randomuser We have reset Codex usage limits.",
            is_retweet=True,
        ),
        TRUSTED,
    )

    assert decision.matched is False
    assert decision.reason == "retweet_not_original"
    assert decision.matched_rules == ()


def test_quote_cannot_supply_reset_evidence_even_when_quote_author_is_trusted() -> None:
    decision = classify(
        post(
            "A product update is available.",
            quoted_text="We have reset Codex usage limits.",
            quoted_author="OpenAI",
        ),
        TRUSTED,
    )

    assert decision.matched is False
    assert decision.matched_rules == ()


def test_original_text_from_trusted_account_still_matches_with_quote_present() -> None:
    decision = classify(
        post(
            "We have reset Codex usage limits.",
            quoted_text="Unrelated quoted material.",
            quoted_author="randomuser",
        ),
        TRUSTED,
    )

    assert decision.matched is True
    assert decision.status is ResetStatus.COMPLETED
    assert decision.matched_rules == ("product", "limit", "reset", "completed")


@pytest.mark.parametrize(
    "text",
    [
        "We reset Codex usage limits every Monday.",
        "We reset Codex usage limits each Monday.",
        "We reset Codex usage limits on Mondays.",
        "We reset Codex quota as part of the reset bank feature.",
        "Codex usage limits receive a weekly reset.",
        "Codex usage limits reset automatically every week.",
        "This explains how the Codex quota reset mechanism works.",
    ],
)
def test_rejects_periodic_or_mechanism_explanations(text: str) -> None:
    assert classify(post(text), TRUSTED).matched is False


@pytest.mark.parametrize(
    "text",
    [
        "How to use the banked reset for Codex weekly usage.",
        "Invite a friend and get a banked reset for Codex weekly usage.",
        "If we added a banked reset, Codex weekly usage would replenish.",
        "Codex weekly usage includes a recurring banked reset mechanism.",
        "We have not added a banked reset for Codex weekly usage.",
    ],
)
def test_rejects_non_announcement_banked_reset_language(text: str) -> None:
    assert classify(post(text), TRUSTED).matched is False
