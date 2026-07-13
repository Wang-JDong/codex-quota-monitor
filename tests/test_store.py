from datetime import UTC, datetime
import sqlite3

import pytest

from codex_quota_monitor.models import Decision, HealthTransition, Post, ResetStatus
from codex_quota_monitor.store import DeliveryState, Store


def sample(post_id: str = "1") -> Post:
    return Post(
        post_id,
        "OpenAI",
        "We reset Codex usage limits",
        datetime.now(UTC),
        f"https://x.com/OpenAI/status/{post_id}",
    )


def test_baseline_makes_existing_posts_seen_without_pending(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    store.baseline_source("OpenAI", [sample()])

    assert store.is_source_baselined("OpenAI") is True
    assert store.unseen([sample()]) == []
    assert store.pending() == []


def test_baseline_rolls_back_if_any_post_cannot_be_stored(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    valid = sample()
    invalid = Post(
        "2",
        "OpenAI",
        "Reset",
        datetime.now(UTC),
        "https://x.com/OpenAI/status/2",
        quoted_text=None,  # type: ignore[arg-type]
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.baseline_source("OpenAI", [valid, invalid])

    assert store.is_source_baselined("OpenAI") is False
    assert store.unseen([valid]) == [valid]


def test_matched_post_is_pending_until_claimed_and_marked_sent(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    store.record_decision(sample(), decision)

    assert [item.post.post_id for item in store.pending()] == ["1"]
    assert store.claim_delivery("1") is True
    assert store.pending() == []
    store.mark_delivery_sent("1")
    assert store.pending() == []


def test_pushed_post_stays_deduplicated_after_restart(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    first_run = Store(database, failure_threshold=3)
    first_run.initialize()
    first_run.record_decision(sample(), decision)

    retry_run = Store(database, failure_threshold=3)
    assert [item.post.post_id for item in retry_run.pending()] == ["1"]
    assert retry_run.claim_delivery("1") is True
    retry_run.mark_delivery_sent("1")
    retry_run.record_decision(sample(), decision)

    final_run = Store(database, failure_threshold=3)
    assert final_run.pending() == []


def test_initialize_migrates_legacy_database_and_recovers_in_flight_as_uncertain(
    tmp_path,
) -> None:
    database = tmp_path / "monitor.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE posts (
                post_id TEXT PRIMARY KEY, author TEXT NOT NULL, text TEXT NOT NULL,
                quoted_text TEXT NOT NULL, quoted_author TEXT NOT NULL,
                is_retweet INTEGER NOT NULL, published_at TEXT NOT NULL,
                url TEXT NOT NULL, matched INTEGER NOT NULL, status TEXT,
                reason TEXT NOT NULL, matched_rules TEXT NOT NULL,
                pushed INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL
            );
            CREATE TABLE sources (
                handle TEXT PRIMARY KEY, baselined INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT, last_error TEXT
            );
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        now = datetime.now(UTC).isoformat()
        connection.execute(
            "INSERT INTO posts VALUES(?, ?, ?, '', '', 0, ?, ?, 1, ?, ?, ?, 0, ?)",
            (
                "legacy-pending",
                "OpenAI",
                "reset Codex limits",
                now,
                "https://x.com/OpenAI/status/legacy-pending",
                ResetStatus.COMPLETED.value,
                "explicit",
                "[]",
                now,
            ),
        )
        connection.execute(
            "INSERT INTO posts VALUES(?, ?, ?, '', '', 0, ?, ?, 1, ?, ?, ?, 1, ?)",
            (
                "legacy-sent",
                "OpenAI",
                "reset Codex limits",
                now,
                "https://x.com/OpenAI/status/legacy-sent",
                ResetStatus.COMPLETED.value,
                "explicit",
                "[]",
                now,
            ),
        )

    store = Store(database, 3)
    store.initialize()
    assert [item.post.post_id for item in store.pending()] == ["legacy-pending"]
    assert store.claim_delivery("legacy-pending") is True

    restarted = Store(database, 3)
    restarted.initialize()
    status = restarted.status()
    assert restarted.pending() == []
    assert status["uncertain"] == 1
    assert status["sent"] == 1
    assert status["delivery_attempts"] == 1


def test_delivery_claim_is_atomic_and_failures_have_explicit_states(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", 3)
    store.initialize()
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    store.record_decision(sample("claim"), decision)

    assert store.claim_delivery("claim") is True
    assert store.claim_delivery("claim") is False
    store.mark_delivery_retryable("claim")
    assert [item.post.post_id for item in store.pending()] == ["claim"]
    assert store.claim_delivery("claim") is True
    store.mark_delivery_permanent("claim")

    status = store.status()
    assert status["permanent_failed"] == 1
    assert status["delivery_attempts"] == 2
    assert store.pending() == []


def test_delivery_errors_are_fixed_safe_labels(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    store = Store(database, 3)
    store.initialize()
    store.record_decision(
        sample("safe-error"),
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )
    store.claim_delivery("safe-error")
    store.mark_delivery_uncertain("safe-error")

    with sqlite3.connect(database) as connection:
        error = connection.execute(
            "SELECT last_delivery_error FROM posts WHERE post_id = 'safe-error'"
        ).fetchone()[0]
    assert error == "outcome_unknown"


def test_nonmatches_and_baseline_do_not_enter_delivery_queue(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    store = Store(database, 3)
    store.initialize()
    store.baseline_source("OpenAI", [sample("baseline")])
    store.record_decision(
        sample("nonmatch"), Decision(False, None, "not relevant")
    )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT post_id, delivery_state, content_hash FROM posts ORDER BY post_id"
        ).fetchall()
    assert rows == [("baseline", None, None), ("nonmatch", None, None)]


def test_manual_resolution_requires_matched_failed_delivery(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", 3)
    store.initialize()
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    store.record_decision(sample("uncertain"), decision)
    store.claim_delivery("uncertain")
    store.mark_delivery_uncertain("uncertain")

    assert store.resolve_delivery("uncertain", "retry") is DeliveryState.PENDING
    store.claim_delivery("uncertain")
    store.mark_delivery_permanent("uncertain")
    assert store.resolve_delivery("uncertain", "sent") is DeliveryState.SENT

    store.record_decision(sample("pending"), decision)
    with pytest.raises(ValueError, match="cannot resolve"):
        store.resolve_delivery("pending", "retry")
    with pytest.raises(ValueError, match="not found"):
        store.resolve_delivery("missing", "sent")
    with pytest.raises(ValueError, match="resolution"):
        store.resolve_delivery("uncertain", "later")


def test_pending_round_trips_the_complete_post(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    post = Post(
        "2",
        "OpenAI",
        "Codex limits reset",
        datetime.now(UTC),
        "https://x.com/OpenAI/status/2",
        quoted_text="More capacity is available",
        quoted_author="OpenAIDevs",
        is_retweet=True,
    )
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))

    store.record_decision(post, decision)

    assert store.pending()[0].post == post


def test_health_alerts_once_after_three_full_outages_and_recovers_once(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()

    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(0, 4, False) is HealthTransition.ALERT
    assert store.claim_health_delivery(HealthTransition.ALERT) is True
    assert store.claim_health_delivery(HealthTransition.ALERT) is False
    store.mark_health_sent(HealthTransition.ALERT)
    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(4, 4, False) is HealthTransition.RECOVERED
    assert store.claim_health_delivery(HealthTransition.RECOVERED) is True
    store.mark_health_sent(HealthTransition.RECOVERED)
    assert store.update_health(4, 4, False) is HealthTransition.NONE

    status = store.status()
    assert status["health_transition"] == "recovered"
    assert status["health_delivery_state"] == "sent"
    assert status["health_transition_epoch"] == 2
    assert status["health_delivery_attempts"] == 2


def test_health_in_flight_becomes_uncertain_after_restart(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    store = Store(database, failure_threshold=1)
    store.initialize()
    assert store.update_health(0, 4, False) is HealthTransition.ALERT
    assert store.claim_health_delivery(HealthTransition.ALERT) is True

    restarted = Store(database, failure_threshold=1)
    restarted.initialize()

    assert restarted.status()["health_delivery_state"] == "uncertain"
    assert restarted.update_health(0, 4, False) is HealthTransition.NONE
    assert restarted.claim_health_delivery(HealthTransition.ALERT) is False


def test_health_manual_resolution_controls_ack_and_retry(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=1)
    store.initialize()
    assert store.update_health(0, 4, False) is HealthTransition.ALERT
    store.claim_health_delivery(HealthTransition.ALERT)
    store.mark_health_uncertain(HealthTransition.ALERT)

    assert store.resolve_health_delivery("alert", "retry") is DeliveryState.PENDING
    assert store.status()["alert_active"] is False
    assert store.update_health(0, 4, False) is HealthTransition.ALERT
    store.claim_health_delivery(HealthTransition.ALERT)
    store.mark_health_permanent(HealthTransition.ALERT)
    assert store.resolve_health_delivery("alert", "sent") is DeliveryState.SENT
    assert store.status()["alert_active"] is True

    with pytest.raises(ValueError, match="current health transition"):
        store.resolve_health_delivery("recovered", "retry")
    with pytest.raises(ValueError, match="resolution"):
        store.resolve_health_delivery("alert", "later")


def test_legacy_pending_health_transition_migrates_to_uncertain(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    store = Store(database, failure_threshold=1)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO state(key, value) VALUES('pending_health_transition', 'alert')"
        )

    upgraded = Store(database, failure_threshold=1)
    upgraded.initialize()

    assert upgraded.status()["health_transition"] == "alert"
    assert upgraded.status()["health_delivery_state"] == "uncertain"
    assert upgraded.update_health(0, 4, False) is HealthTransition.NONE


def test_auth_failure_alerts_immediately(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()

    assert store.update_health(3, 4, True) is HealthTransition.ALERT


def test_source_results_preserve_last_success_and_cap_error(tmp_path) -> None:
    database = tmp_path / "monitor.db"
    store = Store(database, failure_threshold=3)
    store.initialize()
    store.record_source_result("OpenAI", True)
    store.record_source_result("OpenAI", False, "x" * 600)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT last_success_at, last_error FROM sources WHERE handle = 'OpenAI'"
        ).fetchone()

    assert row is not None
    assert datetime.fromisoformat(row[0]).tzinfo is UTC
    assert row[1] == "x" * 500


def test_status_summarizes_sources_pending_and_health(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    store.record_source_result("sama", False, "timeout")
    store.record_source_result("OpenAI", True)
    store.record_decision(
        sample(), Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    )
    store.update_health(0, 4, False)

    status = store.status()

    assert [source["handle"] for source in status["sources"]] == ["OpenAI", "sama"]
    assert status["pending"] == 1
    assert status["uncertain"] == 0
    assert status["permanent_failed"] == 0
    assert status["delivery_attempts"] == 0
    assert status["full_outages"] == 1
    assert status["alert_active"] is False
    assert status["health_transition"] == "none"
    assert status["health_delivery_state"] == "none"
    assert status["health_delivery_attempts"] == 0


def test_run_lock_rejects_overlapping_run(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    with store.run_lock():
        with pytest.raises(RuntimeError, match="another monitor run"):
            with store.run_lock():
                pass
