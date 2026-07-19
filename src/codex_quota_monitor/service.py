from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging

from .classifier import CLASSIFIER_VERSION, classify
from .feed import FeedError, RssHubClient
from .feishu import (
    FeishuClient,
    FeishuError,
    health_notification,
    notification_for_post,
)
from .models import Decision, HealthTransition, Post, Source
from .store import Store


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    fetched_sources: int
    new_posts: int
    matched_posts: int
    sent_posts: int


@dataclass(frozen=True)
class ReprocessSummary:
    post_id: str
    matched: bool
    changed: bool
    sent: bool


@dataclass(frozen=True)
class ReprocessBatchSummary:
    scanned: int
    changed: int
    sent: int
    skipped: int


class MonitorService:
    def __init__(
        self,
        sources: tuple[Source, ...],
        feed: RssHubClient,
        store: Store,
        feishu: FeishuClient,
    ) -> None:
        self.sources = sources
        self.feed = feed
        self.store = store
        self.feishu = feishu
        self.trusted = frozenset(source.handle.casefold() for source in sources)

    def _fetch_posts(self, source: Source) -> list[Post]:
        fetch_all = getattr(self.feed, "fetch_all", None)
        if callable(fetch_all):
            return fetch_all(source)
        return self.feed.fetch(source)

    def _send_business(self, post: Post, decision: Decision) -> bool:
        if not self.store.claim_delivery(post.post_id):
            return True
        try:
            self.feishu.send(notification_for_post(post, decision))
        except FeishuError as exc:
            if exc.outcome_unknown:
                self.store.mark_delivery_uncertain(post.post_id)
                label = "outcome_unknown"
            elif exc.retryable:
                self.store.mark_delivery_retryable(post.post_id)
                label = "retryable_failure"
            else:
                self.store.mark_delivery_permanent(post.post_id)
                label = "permanent_failure"
            logger.error("business delivery failed: %s", label)
            return False
        except Exception:
            self.store.mark_delivery_uncertain(post.post_id)
            logger.error("business delivery failed: outcome_unknown")
            return False
        self.store.mark_delivery_sent(post.post_id)
        return True

    def _send_health(self, transition: HealthTransition) -> bool:
        if not self.store.claim_health_delivery(transition):
            return True
        try:
            self.feishu.send(health_notification(transition))
        except FeishuError as exc:
            if exc.outcome_unknown:
                self.store.mark_health_uncertain(transition)
                label = "outcome_unknown"
            elif exc.retryable:
                self.store.mark_health_retryable(transition)
                label = "retryable_failure"
            else:
                self.store.mark_health_permanent(transition)
                label = "permanent_failure"
            logger.error("health delivery failed: %s", label)
            return False
        except Exception:
            self.store.mark_health_uncertain(transition)
            logger.error("health delivery failed: outcome_unknown")
            return False
        self.store.mark_health_sent(transition)
        return True

    def _promote_and_send(self, post: Post, decision: Decision) -> tuple[bool, bool]:
        changed = self.store.refresh_unmatched(
            post, decision, CLASSIFIER_VERSION
        )
        if not changed:
            return False, False
        return True, self._send_business(post, decision)

    def reprocess(self, post_id: str) -> ReprocessSummary:
        found: Post | None = None
        for source in self.sources:
            try:
                posts = self._fetch_posts(source)
            except FeedError:
                continue
            found = next((post for post in posts if post.post_id == post_id), None)
            if found is not None:
                break
        if found is None:
            raise ValueError("post not found in current trusted feeds")

        decision = classify(found, self.trusted)
        if not decision.matched:
            raise ValueError(f"post did not match: {decision.reason}")
        changed, sent = self._promote_and_send(found, decision)
        return ReprocessSummary(post_id, True, changed, sent)

    def reprocess_unmatched(
        self, days: int = 7, limit: int = 100
    ) -> ReprocessBatchSummary:
        if not 1 <= days <= 31:
            raise ValueError("days must be between 1 and 31")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        candidates = self.store.unmatched_since(
            datetime.now(UTC) - timedelta(days=days), limit
        )
        feed_index: dict[str, Post] = {}
        for source in self.sources:
            try:
                posts = self._fetch_posts(source)
            except FeedError as exc:
                logger.error(
                    "reprocess source @%s failed: %s",
                    source.handle,
                    "authentication failed" if exc.auth_failed else "feed fetch failed",
                )
                continue
            for post in posts:
                feed_index.setdefault(post.post_id, post)

        scanned = changed = sent = skipped = 0
        for stored in candidates:
            scanned += 1
            current = feed_index.get(stored.post_id)
            if current is None:
                skipped += 1
                continue
            decision = classify(current, self.trusted)
            if not decision.matched:
                self.store.refresh_unmatched(
                    current, decision, CLASSIFIER_VERSION
                )
                skipped += 1
                continue
            promoted, delivered = self._promote_and_send(current, decision)
            if not promoted:
                skipped += 1
                continue
            changed += 1
            sent += int(delivered)

        return ReprocessBatchSummary(scanned, changed, sent, skipped)

    def run(self, dry_run: bool = False) -> RunSummary:
        fetched = 0
        new_count = 0
        matched = 0
        sent = 0
        auth_failed = False
        delivery_available = True

        if not dry_run:
            for item in self.store.pending():
                if not self._send_business(item.post, item.decision):
                    delivery_available = False
                    break
                sent += 1

        for source in self.sources:
            try:
                posts = self._fetch_posts(source)
                fetched += 1
            except FeedError as exc:
                auth_failed = auth_failed or exc.auth_failed
                error = "authentication failed" if exc.auth_failed else "feed fetch failed"
                if not dry_run:
                    self.store.record_source_result(source.handle, False, error)
                logger.error("source @%s failed: %s", source.handle, error)
                continue

            if dry_run:
                for item in posts:
                    decision = classify(item, self.trusted)
                    logger.info(
                        "dry-run @%s %s matched=%s reason=%s",
                        item.author,
                        item.post_id,
                        decision.matched,
                        decision.reason,
                    )
                continue

            self.store.record_source_result(source.handle, True)
            if not self.store.is_source_baselined(source.handle):
                self.store.baseline_source(source.handle, posts)
                continue

            unseen = self.store.unseen(posts)
            unseen_ids = {item.post_id for item in unseen}
            for item in posts:
                if item.post_id in unseen_ids:
                    new_count += 1
                    decision = classify(item, self.trusted)
                    self.store.record_decision(item, decision)
                    if not decision.matched:
                        continue
                    matched += 1
                    if delivery_available:
                        if self._send_business(item, decision):
                            sent += 1
                        else:
                            delivery_available = False
                    continue

                if not self.store.needs_reclassification(
                    item.post_id, CLASSIFIER_VERSION
                ):
                    continue
                decision = classify(item, self.trusted)
                if not decision.matched:
                    self.store.refresh_unmatched(
                        item, decision, CLASSIFIER_VERSION
                    )
                    continue
                if not self.store.refresh_unmatched(
                    item, decision, CLASSIFIER_VERSION
                ):
                    continue
                matched += 1
                if delivery_available:
                    if self._send_business(item, decision):
                        sent += 1
                    else:
                        delivery_available = False

        if not dry_run:
            transition = self.store.update_health(
                fetched, len(self.sources), auth_failed
            )
            if transition.value != "none" and delivery_available:
                self._send_health(transition)

        return RunSummary(fetched, new_count, matched, sent)
