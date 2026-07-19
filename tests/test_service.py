from datetime import UTC, datetime, timedelta

import pytest

from codex_quota_monitor.feed import FeedError
from codex_quota_monitor.feishu import FeishuError
from codex_quota_monitor.models import Decision, Post, ResetStatus, Source
from codex_quota_monitor.service import MonitorService
from codex_quota_monitor.store import Store


class FakeFeed:
    def __init__(self, posts: dict[str, list[Post] | FeedError]) -> None:
        self.posts = posts

    def fetch(self, source: Source) -> list[Post]:
        result = self.posts.get(source.handle, [])
        if isinstance(result, FeedError):
            raise result
        return list(result)


class FakeFeishu:
    def __init__(self) -> None:
        self.sent = []

    def send(self, note) -> None:
        self.sent.append(note)


def post(post_id: str, author: str = "OpenAI") -> Post:
    return Post(
        post_id,
        author,
        "We have reset Codex usage limits across all plans",
        datetime.now(UTC),
        f"https://x.com/{author}/status/{post_id}",
    )


def service_for(tmp_path, sources, feed, feishu=None):
    store = Store(tmp_path / "db.sqlite", 3)
    store.initialize()
    client = feishu or FakeFeishu()
    return MonitorService(tuple(sources), feed, store, client), store, client


def test_first_run_baselines_then_new_match_sends_once(tmp_path) -> None:
    source = Source("OpenAI")
    old = post("1")
    feed = FakeFeed({"OpenAI": [old]})
    service, _store, feishu = service_for(tmp_path, (source,), feed)

    first = service.run()
    assert first.fetched_sources == 1
    assert feishu.sent == []

    feed.posts["OpenAI"] = [post("2"), old]
    second = service.run()
    third = service.run()

    assert second.new_posts == 1
    assert second.matched_posts == 1
    assert second.sent_posts == 1
    assert third.sent_posts == 0
    assert len(feishu.sent) == 1
    assert "已经重置" in feishu.sent[0].title


def test_run_uses_media_supplement_when_timeline_omits_new_post(tmp_path) -> None:
    source = Source("thsottiaux")
    old = post("2078320950488297000", "thsottiaux")
    missed = post("2078320950488297917", "thsottiaux")

    class FetchAllOnlyFeed:
        def __init__(self) -> None:
            self.current = [old]

        def fetch(self, _source: Source) -> list[Post]:
            raise AssertionError("the service must use the supplemented fetch")

        def fetch_all(self, _source: Source) -> list[Post]:
            return list(self.current)

    feed = FetchAllOnlyFeed()
    service, _store, feishu = service_for(tmp_path, (source,), feed)

    service.run()
    feed.current = [missed, old]
    summary = service.run()

    assert summary.new_posts == 1
    assert summary.matched_posts == 1
    assert summary.sent_posts == 1
    assert len(feishu.sent) == 1


def test_reprocesses_known_unmatched_post_and_sends_exactly_once(tmp_path) -> None:
    source = Source("thsottiaux")
    full = Post(
        "2076735790567338203",
        "thsottiaux",
        "We have added a banked reset for Codex weekly usage.",
        datetime.now(UTC),
        "https://x.com/thsottiaux/status/2076735790567338203",
    )
    feed = FakeFeed({"thsottiaux": [full]})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    stored = Post(full.post_id, full.author, "truncated...", full.published_at, full.url)
    store.record_decision(stored, Decision(False, None, "missing:limit"))

    first = service.reprocess(full.post_id)
    second = service.reprocess(full.post_id)

    assert first.post_id == full.post_id
    assert first.changed is True
    assert first.sent is True
    assert second.changed is False
    assert second.sent is False
    assert len(feishu.sent) == 1
    assert "可保存重置次数已发放" in feishu.sent[0].title
    assert store.status()["delivery_attempts"] == 1


def test_reprocess_fails_without_mutation_when_post_is_not_in_feed(tmp_path) -> None:
    source = Source("thsottiaux")
    service, store, feishu = service_for(
        tmp_path, (source,), FakeFeed({"thsottiaux": []})
    )
    store.record_decision(post("missing", "thsottiaux"), Decision(False, None, "old"))

    with pytest.raises(ValueError, match="not found in current trusted feeds"):
        service.reprocess("missing")

    assert feishu.sent == []
    assert store.pending() == []


def test_reprocess_unmatched_promotes_and_is_idempotent(tmp_path) -> None:
    source = Source("thsottiaux")
    full = Post(
        "2077607697487188198",
        "thsottiaux",
        "Another reset for our Codex and ChatGPT Work users. Should have that "
        "sweet 100% weekly usage limit back in a few minutes.",
        datetime.now(UTC),
        "https://x.com/thsottiaux/status/2077607697487188198",
    )
    feed = FakeFeed({"thsottiaux": [full]})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    store.record_decision(
        Post(full.post_id, full.author, "old text", full.published_at, full.url),
        Decision(False, None, "reset_state_not_explicit"),
    )

    first = service.reprocess_unmatched()
    second = service.reprocess_unmatched()

    assert first.scanned == 1
    assert first.changed == 1
    assert first.sent == 1
    assert first.skipped == 0
    assert second.scanned == 0
    assert second.changed == 0
    assert second.sent == 0
    assert len(feishu.sent) == 1
    assert store.status()["sent"] == 1


def test_reprocess_unmatched_skips_old_and_already_matched_rows(tmp_path) -> None:
    source = Source("OpenAI")
    old = Post(
        "old",
        "OpenAI",
        "old text",
        datetime.now(UTC) - timedelta(days=8),
        "https://x.com/OpenAI/status/old",
    )
    matched = post("already-matched")
    feed = FakeFeed({"OpenAI": [old, matched]})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    store.record_decision(old, Decision(False, None, "old"))
    store.record_decision(
        matched,
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )

    summary = service.reprocess_unmatched(days=7, limit=100)

    assert summary.scanned == 0
    assert summary.changed == 0
    assert summary.sent == 0
    assert summary.skipped == 0
    assert feishu.sent == []
    assert store.status()["pending"] == 1


def test_reprocess_unmatched_counts_still_negative_and_continues_after_source_error(
    tmp_path,
) -> None:
    sources = (Source("OpenAI"), Source("sama"))
    negative = Post(
        "negative",
        "sama",
        "Codex now has higher rate limits.",
        datetime.now(UTC),
        "https://x.com/sama/status/negative",
    )
    positive = post("positive", "sama")
    feed = FakeFeed(
        {"OpenAI": FeedError("temporary"), "sama": [negative, positive]}
    )
    service, store, feishu = service_for(tmp_path, sources, feed)
    store.record_decision(negative, Decision(False, None, "missing:action"))
    store.record_decision(positive, Decision(False, None, "old"))

    summary = service.reprocess_unmatched(days=7, limit=100)

    assert summary.scanned == 2
    assert summary.changed == 1
    assert summary.sent == 1
    assert summary.skipped == 1
    assert len(feishu.sent) == 1


def test_current_feed_refreshes_old_unmatched_rule_version_once(tmp_path) -> None:
    source = Source("OpenAI")
    full = post("versioned")
    feed = FakeFeed({"OpenAI": []})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    service.run()  # establish the source baseline
    stale = Post(full.post_id, full.author, "old text", full.published_at, full.url)
    store.record_decision(
        stale,
        Decision(False, None, "reset_state_not_explicit"),
        classifier_version="1",
    )
    feed.posts["OpenAI"] = [full]

    first = service.run()
    second = service.run()

    assert first.new_posts == 0
    assert first.matched_posts == 1
    assert first.sent_posts == 1
    assert second.matched_posts == 0
    assert second.sent_posts == 0
    assert len(feishu.sent) == 1


def test_current_feed_refreshes_negative_version_without_delivery(tmp_path) -> None:
    source = Source("OpenAI")
    negative = Post(
        "negative-version",
        "OpenAI",
        "Codex now has higher rate limits.",
        datetime.now(UTC),
        "https://x.com/OpenAI/status/negative-version",
    )
    feed = FakeFeed({"OpenAI": []})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    service.run()
    store.record_decision(
        negative,
        Decision(False, None, "old"),
        classifier_version="1",
    )
    feed.posts["OpenAI"] = [negative]

    summary = service.run()

    assert summary.matched_posts == 0
    assert summary.sent_posts == 0
    assert feishu.sent == []
    with store._connect() as connection:
        row = connection.execute(
            "SELECT matched, classification_version, delivery_state FROM posts "
            "WHERE post_id = ?",
            (negative.post_id,),
        ).fetchone()
    assert tuple(row) == (0, "3", None)


def test_current_feed_never_reclassifies_matched_or_sent_rows(tmp_path) -> None:
    source = Source("OpenAI")
    matched = post("already-sent")
    feed = FakeFeed({"OpenAI": []})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    service.run()
    store.record_decision(
        matched,
        Decision(
            True,
            ResetStatus.COMPLETED,
            "explicit_codex_limit_reset",
            ("product", "limit", "reset", "completed"),
        ),
        classifier_version="1",
    )
    store.claim_delivery(matched.post_id)
    store.mark_delivery_sent(matched.post_id)
    feed.posts["OpenAI"] = [
        Post(
            matched.post_id,
            matched.author,
            "Codex now has higher rate limits.",
            matched.published_at,
            matched.url,
        )
    ]

    summary = service.run()

    assert summary.matched_posts == 0
    assert summary.sent_posts == 0
    assert len(feishu.sent) == 0
    assert store.status()["sent"] == 1


def test_explicit_retryable_pending_failure_is_retried_next_run(tmp_path) -> None:
    class FailOnceFeishu(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise FeishuError(
                    "rate limited", outcome_unknown=False, retryable=True
                )
            super().send(note)

    feishu = FailOnceFeishu()
    service, store, _ = service_for(tmp_path, (), FakeFeed({}), feishu)
    matching = post("pending")
    store.record_decision(
        matching,
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )

    first = service.run()
    assert first.sent_posts == 0
    assert [item.post.post_id for item in store.pending()] == ["pending"]

    summary = service.run()

    assert summary.sent_posts == 1
    assert store.pending() == []
    assert len(feishu.sent) == 1


def test_timeout_after_possible_accept_is_attempted_once_and_becomes_uncertain(
    tmp_path,
) -> None:
    class TimeoutAfterAccept(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            raise FeishuError(
                "delivery outcome unknown", outcome_unknown=True, retryable=False
            )

    client = TimeoutAfterAccept()
    service, store, _ = service_for(tmp_path, (), FakeFeed({}), client)
    store.record_decision(
        post("unknown"),
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )

    service.run()
    service.run()

    assert client.attempts == 1
    assert store.status()["uncertain"] == 1
    assert store.pending() == []


def test_crash_after_send_before_mark_recovers_as_uncertain_without_resend(
    tmp_path,
) -> None:
    class CrashAfterAccept(FakeFeishu):
        def send(self, note) -> None:
            self.sent.append(note)
            raise SystemExit("simulated crash after provider accepted")

    database = tmp_path / "db.sqlite"
    store = Store(database, 3)
    store.initialize()
    store.record_decision(
        post("crash"),
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )
    crashing = CrashAfterAccept()
    service = MonitorService((), FakeFeed({}), store, crashing)

    with pytest.raises(SystemExit, match="simulated crash"):
        service.run()

    restarted_store = Store(database, 3)
    restarted_store.initialize()
    restarted_client = FakeFeishu()
    restarted = MonitorService((), FakeFeed({}), restarted_store, restarted_client)
    restarted.run()

    assert len(crashing.sent) == 1
    assert restarted_client.sent == []
    assert restarted_store.status()["uncertain"] == 1


def test_pending_poison_failure_does_not_create_feed_gap(tmp_path) -> None:
    sources = tuple(Source(handle) for handle in ("OpenAI", "OpenAIDevs", "thsottiaux", "sama"))

    class PermanentFailure(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            raise FeishuError("rejected", outcome_unknown=False, retryable=False)

    feed = FakeFeed({source.handle: [] for source in sources})
    client = PermanentFailure()
    service, store, _ = service_for(tmp_path, sources, feed, client)
    service.run()  # baseline every source
    store.record_decision(
        post("poison"),
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )
    feed.posts = {
        source.handle: [post(f"new-{source.handle}", source.handle)]
        for source in sources
    }

    summary = service.run()

    assert summary.fetched_sources == 4
    assert summary.new_posts == 4
    assert summary.matched_posts == 4
    assert client.attempts == 1
    assert store.status()["permanent_failed"] == 1
    assert sorted(item.post.post_id for item in store.pending()) == sorted(
        f"new-{source.handle}" for source in sources
    )


def test_one_source_failure_does_not_block_other_sources(tmp_path) -> None:
    sources = (Source("OpenAI"), Source("sama"))
    old = post("1", "sama")
    feed = FakeFeed({"OpenAI": [], "sama": [old]})
    service, _store, feishu = service_for(tmp_path, sources, feed)
    service.run()

    feed.posts = {
        "OpenAI": FeedError("temporary error"),
        "sama": [post("2", "sama"), old],
    }
    summary = service.run()

    assert summary.fetched_sources == 1
    assert summary.matched_posts == 1
    assert summary.sent_posts == 1
    assert feishu.sent[0].title.startswith("Codex 额度重置通知")


def test_full_outage_alerts_once_and_full_recovery_notifies_once(tmp_path) -> None:
    sources = (Source("OpenAI"), Source("sama"))
    feed = FakeFeed({handle.handle: FeedError("offline") for handle in sources})
    service, _store, feishu = service_for(tmp_path, sources, feed)

    service.run()
    service.run()
    service.run()
    service.run()
    assert [note.title for note in feishu.sent] == ["Codex 额度监控异常"]

    feed.posts = {"OpenAI": [], "sama": []}
    service.run()
    service.run()

    assert [note.title for note in feishu.sent] == [
        "Codex 额度监控异常",
        "Codex 额度监控已恢复",
    ]


def test_auth_failure_alerts_immediately(tmp_path) -> None:
    source = Source("OpenAI")
    feed = FakeFeed({"OpenAI": FeedError("private token", auth_failed=True)})
    service, _store, feishu = service_for(tmp_path, (source,), feed)

    service.run()

    assert [note.title for note in feishu.sent] == ["Codex 额度监控异常"]


def test_alert_unknown_outcome_is_never_automatically_resent(
    tmp_path,
) -> None:
    source = Source("OpenAI")

    class UnknownOutcome(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            raise FeishuError(
                "outcome unknown", outcome_unknown=True, retryable=False
            )

    feed = FakeFeed({"OpenAI": FeedError("offline", auth_failed=True)})
    client = UnknownOutcome()
    service, store, _ = service_for(tmp_path, (source,), feed, client)

    first = service.run()
    assert first.fetched_sources == 0
    assert store.status()["alert_active"] is False
    assert store.status()["health_delivery_state"] == "uncertain"

    restarted_store = Store(tmp_path / "db.sqlite", 3)
    restarted_store.initialize()
    restarted = MonitorService((source,), feed, restarted_store, client)
    second = restarted.run()

    assert second.fetched_sources == 0
    assert client.attempts == 1
    assert restarted_store.status()["health_delivery_state"] == "uncertain"


def test_recovery_unknown_outcome_is_never_automatically_resent(tmp_path) -> None:
    source = Source("OpenAI")

    class FailRecovery(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            if "已恢复" in note.title:
                raise FeishuError(
                    "outcome unknown", outcome_unknown=True, retryable=False
                )
            super().send(note)

    feed = FakeFeed({"OpenAI": FeedError("offline", auth_failed=True)})
    client = FailRecovery()
    service, store, _ = service_for(tmp_path, (source,), feed, client)
    service.run()

    feed.posts = {"OpenAI": []}
    service.run()
    second = service.run()

    assert second.fetched_sources == 1
    assert client.attempts == 2
    assert store.status()["alert_active"] is True
    assert store.status()["health_transition"] == "recovered"
    assert store.status()["health_delivery_state"] == "uncertain"


def test_health_429_returns_pending_and_is_safely_retried(tmp_path) -> None:
    source = Source("OpenAI")

    class RateLimitOnce(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise FeishuError(
                    "rate limited", outcome_unknown=False, retryable=True
                )
            super().send(note)

    feed = FakeFeed({"OpenAI": FeedError("offline", auth_failed=True)})
    client = RateLimitOnce()
    service, store, _ = service_for(tmp_path, (source,), feed, client)

    service.run()
    assert store.status()["health_delivery_state"] == "pending"
    service.run()

    assert client.attempts == 2
    assert store.status()["health_delivery_state"] == "sent"
    assert store.status()["alert_active"] is True


def test_business_failure_leaves_health_pending_without_claiming(tmp_path) -> None:
    source = Source("OpenAI")

    class AlwaysUnknown(FakeFeishu):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def send(self, note) -> None:
            self.attempts += 1
            raise FeishuError(
                "outcome unknown", outcome_unknown=True, retryable=False
            )

    feed = FakeFeed({"OpenAI": FeedError("offline", auth_failed=True)})
    client = AlwaysUnknown()
    service, store, _ = service_for(tmp_path, (source,), feed, client)
    store.record_decision(
        post("business"),
        Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",)),
    )

    service.run()

    assert client.attempts == 1
    assert store.status()["health_transition"] == "alert"
    assert store.status()["health_delivery_state"] == "pending"
    assert store.status()["health_delivery_attempts"] == 0


def test_dry_run_does_not_send_or_change_persistent_state(tmp_path) -> None:
    source = Source("OpenAI")
    feed = FakeFeed({"OpenAI": [post("1")]})
    service, store, feishu = service_for(tmp_path, (source,), feed)
    before = store.status()

    summary = service.run(dry_run=True)

    assert summary.fetched_sources == 1
    assert summary.new_posts == 0
    assert summary.matched_posts == 0
    assert summary.sent_posts == 0
    assert store.status() == before
    assert store.is_source_baselined("OpenAI") is False
    assert feishu.sent == []


def test_feed_error_details_are_not_logged_or_persisted(tmp_path, caplog) -> None:
    source = Source("OpenAI")
    secret = "sensitive-auth-token"
    feed = FakeFeed({"OpenAI": FeedError(secret, auth_failed=True)})
    service, store, _feishu = service_for(tmp_path, (source,), feed)

    service.run()

    status = store.status()
    assert secret not in caplog.text
    assert secret not in str(status)
    assert status["sources"][0]["last_error"] == "authentication failed"
