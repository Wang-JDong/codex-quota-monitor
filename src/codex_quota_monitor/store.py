from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import Decision, HealthTransition, Post, ResetStatus


@dataclass(frozen=True)
class StoredMatch:
    post: Post
    decision: Decision


class DeliveryState(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SENT = "sent"
    PERMANENT_FAILED = "permanent_failed"
    UNCERTAIN = "uncertain"


class Store:
    def __init__(self, path: Path, failure_threshold: int) -> None:
        self.path = path
        self.failure_threshold = failure_threshold

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY,
                    author TEXT NOT NULL,
                    text TEXT NOT NULL,
                    quoted_text TEXT NOT NULL,
                    quoted_author TEXT NOT NULL,
                    is_retweet INTEGER NOT NULL,
                    published_at TEXT NOT NULL,
                    url TEXT NOT NULL,
                    matched INTEGER NOT NULL,
                    status TEXT,
                    reason TEXT NOT NULL,
                    matched_rules TEXT NOT NULL,
                    pushed INTEGER NOT NULL DEFAULT 0,
                    delivery_state TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_delivery_error TEXT,
                    content_hash TEXT,
                    first_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    handle TEXT PRIMARY KEY,
                    baselined INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(posts)")
            }
            migrations = {
                "delivery_state": "ALTER TABLE posts ADD COLUMN delivery_state TEXT",
                "attempt_count": (
                    "ALTER TABLE posts ADD COLUMN attempt_count "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "last_delivery_error": (
                    "ALTER TABLE posts ADD COLUMN last_delivery_error TEXT"
                ),
                "content_hash": "ALTER TABLE posts ADD COLUMN content_hash TEXT",
            }
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(statement)
            connection.execute(
                "UPDATE posts SET delivery_state = CASE "
                "WHEN pushed = 1 THEN ? ELSE ? END "
                "WHERE matched = 1 AND delivery_state IS NULL",
                (DeliveryState.SENT, DeliveryState.PENDING),
            )
            for row in connection.execute(
                "SELECT * FROM posts WHERE matched = 1 AND content_hash IS NULL"
            ):
                connection.execute(
                    "UPDATE posts SET content_hash = ? WHERE post_id = ?",
                    (self._content_hash_from_row(row), row["post_id"]),
                )
            connection.execute(
                "UPDATE posts SET delivery_state = ?, "
                "last_delivery_error = 'outcome_unknown_after_restart' "
                "WHERE delivery_state = ?",
                (DeliveryState.UNCERTAIN, DeliveryState.IN_FLIGHT),
            )
            legacy_transition = HealthTransition(
                self._get_state(
                    connection,
                    "pending_health_transition",
                    HealthTransition.NONE,
                )
            )
            health_transition = HealthTransition(
                self._get_state(
                    connection, "health_transition", HealthTransition.NONE
                )
            )
            health_delivery_state = self._get_state(
                connection, "health_delivery_state", "none"
            )
            if (
                health_transition is HealthTransition.NONE
                and legacy_transition is not HealthTransition.NONE
            ):
                self._set_state(
                    connection, "health_transition", legacy_transition.value
                )
                self._set_state(
                    connection,
                    "health_delivery_state",
                    DeliveryState.UNCERTAIN,
                )
                self._set_state(
                    connection,
                    "health_last_delivery_error",
                    "legacy_outcome_unknown",
                )
                epoch = int(
                    self._get_state(connection, "health_transition_epoch", "0")
                )
                self._set_state(
                    connection, "health_transition_epoch", str(max(1, epoch))
                )
            elif health_delivery_state == DeliveryState.IN_FLIGHT:
                self._set_state(
                    connection,
                    "health_delivery_state",
                    DeliveryState.UNCERTAIN,
                )
                self._set_state(
                    connection,
                    "health_last_delivery_error",
                    "outcome_unknown_after_restart",
                )
            if legacy_transition is not HealthTransition.NONE:
                self._set_state(
                    connection,
                    "pending_health_transition",
                    HealthTransition.NONE,
                )

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("w", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("another monitor run is active") from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def is_source_baselined(self, handle: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT baselined FROM sources WHERE handle = ?", (handle,)
            ).fetchone()
        return bool(row and row["baselined"])

    def baseline_source(self, handle: str, posts: list[Post]) -> None:
        with self._connect() as connection:
            for post in posts:
                self._insert(connection, post, Decision(False, None, "baseline"))
            connection.execute(
                "INSERT INTO sources(handle, baselined) VALUES(?, 1) "
                "ON CONFLICT(handle) DO UPDATE SET baselined = 1",
                (handle,),
            )

    def unseen(self, posts: list[Post]) -> list[Post]:
        with self._connect() as connection:
            return [
                post
                for post in posts
                if connection.execute(
                    "SELECT 1 FROM posts WHERE post_id = ?", (post.post_id,)
                ).fetchone()
                is None
            ]

    def _insert(
        self, connection: sqlite3.Connection, post: Post, decision: Decision
    ) -> None:
        delivery_state = DeliveryState.PENDING if decision.matched else None
        content_hash = self._content_hash(post, decision) if decision.matched else None
        connection.execute(
            "INSERT INTO posts ("
            "post_id, author, text, quoted_text, quoted_author, is_retweet, "
            "published_at, url, matched, status, reason, matched_rules, pushed, "
            "delivery_state, attempt_count, last_delivery_error, content_hash, "
            "first_seen_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, NULL, ?, ?) "
            "ON CONFLICT(post_id) DO NOTHING",
            (
                post.post_id,
                post.author,
                post.text,
                post.quoted_text,
                post.quoted_author,
                int(post.is_retweet),
                post.published_at.isoformat(),
                post.url,
                int(decision.matched),
                decision.status.value if decision.status else None,
                decision.reason,
                json.dumps(decision.matched_rules),
                delivery_state,
                content_hash,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _content_hash(post: Post, decision: Decision) -> str:
        canonical = json.dumps(
            {
                "post_id": post.post_id,
                "author": post.author,
                "text": post.text,
                "published_at": post.published_at.isoformat(),
                "url": post.url,
                "status": decision.status.value if decision.status else None,
                "reason": decision.reason,
                "matched_rules": decision.matched_rules,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    @staticmethod
    def _content_hash_from_row(row: sqlite3.Row) -> str:
        canonical = json.dumps(
            {
                "post_id": row["post_id"],
                "author": row["author"],
                "text": row["text"],
                "published_at": row["published_at"],
                "url": row["url"],
                "status": row["status"],
                "reason": row["reason"],
                "matched_rules": tuple(json.loads(row["matched_rules"])),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def record_decision(self, post: Post, decision: Decision) -> None:
        with self._connect() as connection:
            self._insert(connection, post, decision)

    def pending(self) -> list[StoredMatch]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM posts "
                "WHERE matched = 1 AND delivery_state = ? ORDER BY published_at",
                (DeliveryState.PENDING,),
            ).fetchall()
        return [
            StoredMatch(
                Post(
                    row["post_id"],
                    row["author"],
                    row["text"],
                    datetime.fromisoformat(row["published_at"]),
                    row["url"],
                    row["quoted_text"],
                    row["quoted_author"],
                    bool(row["is_retweet"]),
                ),
                Decision(
                    True,
                    ResetStatus(row["status"]),
                    row["reason"],
                    tuple(json.loads(row["matched_rules"])),
                ),
            )
            for row in rows
        ]

    def claim_delivery(self, post_id: str) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE posts SET delivery_state = ?, "
                "attempt_count = attempt_count + 1, last_delivery_error = NULL "
                "WHERE post_id = ? AND matched = 1 AND delivery_state = ?",
                (DeliveryState.IN_FLIGHT, post_id, DeliveryState.PENDING),
            )
            return result.rowcount == 1

    def _mark_delivery(
        self,
        post_id: str,
        state: DeliveryState,
        safe_error: str | None,
    ) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE posts SET delivery_state = ?, last_delivery_error = ?, "
                "pushed = ? WHERE post_id = ? AND delivery_state = ?",
                (
                    state,
                    safe_error,
                    int(state is DeliveryState.SENT),
                    post_id,
                    DeliveryState.IN_FLIGHT,
                ),
            )
            if result.rowcount != 1:
                raise ValueError("delivery is not in flight")

    def mark_delivery_sent(self, post_id: str) -> None:
        self._mark_delivery(post_id, DeliveryState.SENT, None)

    def mark_delivery_retryable(self, post_id: str) -> None:
        self._mark_delivery(post_id, DeliveryState.PENDING, "rate_limited")

    def mark_delivery_permanent(self, post_id: str) -> None:
        self._mark_delivery(post_id, DeliveryState.PERMANENT_FAILED, "rejected")

    def mark_delivery_uncertain(self, post_id: str) -> None:
        self._mark_delivery(post_id, DeliveryState.UNCERTAIN, "outcome_unknown")

    def resolve_delivery(self, post_id: str, resolution: str) -> DeliveryState:
        if resolution not in {"sent", "retry"}:
            raise ValueError("resolution must be sent or retry")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT matched, delivery_state FROM posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            if row is None:
                raise ValueError("post not found")
            if not row["matched"] or row["delivery_state"] not in {
                DeliveryState.UNCERTAIN,
                DeliveryState.PERMANENT_FAILED,
            }:
                raise ValueError("cannot resolve delivery in current state")
            state = (
                DeliveryState.SENT
                if resolution == "sent"
                else DeliveryState.PENDING
            )
            connection.execute(
                "UPDATE posts SET delivery_state = ?, pushed = ?, "
                "last_delivery_error = NULL WHERE post_id = ?",
                (state, int(state is DeliveryState.SENT), post_id),
            )
        return state

    def record_source_result(
        self, handle: str, success: bool, error: str = ""
    ) -> None:
        with self._connect() as connection:
            if success:
                connection.execute(
                    "INSERT INTO sources(handle, last_success_at, last_error) "
                    "VALUES(?, ?, '') "
                    "ON CONFLICT(handle) DO UPDATE SET "
                    "last_success_at = excluded.last_success_at, last_error = ''",
                    (handle, datetime.now(UTC).isoformat()),
                )
            else:
                connection.execute(
                    "INSERT INTO sources(handle, last_error) VALUES(?, ?) "
                    "ON CONFLICT(handle) DO UPDATE SET "
                    "last_error = excluded.last_error",
                    (handle, error[:500]),
                )

    def _get_state(
        self, connection: sqlite3.Connection, key: str, default: str
    ) -> str:
        row = connection.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return str(row["value"]) if row else default

    def _set_state(
        self, connection: sqlite3.Connection, key: str, value: str
    ) -> None:
        connection.execute(
            "INSERT INTO state VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def update_health(
        self, successful_sources: int, total_sources: int, auth_failed: bool
    ) -> HealthTransition:
        with self._connect() as connection:
            failures = int(self._get_state(connection, "full_outages", "0"))
            alert_active = (
                self._get_state(connection, "alert_active", "0") == "1"
            )
            if auth_failed:
                failures = self.failure_threshold
            elif successful_sources == 0:
                failures += 1
            else:
                failures = 0

            current_transition = HealthTransition(
                self._get_state(
                    connection, "health_transition", HealthTransition.NONE
                )
            )
            delivery_state = self._get_state(
                connection, "health_delivery_state", "none"
            )
            self._set_state(connection, "full_outages", str(failures))
            if delivery_state == DeliveryState.PENDING:
                return current_transition
            if delivery_state in {
                DeliveryState.IN_FLIGHT,
                DeliveryState.UNCERTAIN,
                DeliveryState.PERMANENT_FAILED,
            }:
                return HealthTransition.NONE

            transition = HealthTransition.NONE
            if not alert_active and failures >= self.failure_threshold:
                transition = HealthTransition.ALERT
            elif alert_active and successful_sources == total_sources:
                transition = HealthTransition.RECOVERED

            if transition is not HealthTransition.NONE:
                epoch = int(
                    self._get_state(connection, "health_transition_epoch", "0")
                )
                self._set_state(
                    connection, "health_transition", transition.value
                )
                self._set_state(
                    connection,
                    "health_delivery_state",
                    DeliveryState.PENDING,
                )
                self._set_state(
                    connection, "health_transition_epoch", str(epoch + 1)
                )
                self._set_state(
                    connection, "health_last_delivery_error", ""
                )
            return transition

    def claim_health_delivery(self, transition: HealthTransition) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE state SET value = ? WHERE key = 'health_delivery_state' "
                "AND value = ? AND EXISTS ("
                "SELECT 1 FROM state WHERE key = 'health_transition' AND value = ?"
                ")",
                (
                    DeliveryState.IN_FLIGHT,
                    DeliveryState.PENDING,
                    transition.value,
                ),
            )
            if result.rowcount != 1:
                return False
            attempts = int(
                self._get_state(connection, "health_delivery_attempts", "0")
            )
            self._set_state(
                connection, "health_delivery_attempts", str(attempts + 1)
            )
            self._set_state(connection, "health_last_delivery_error", "")
            return True

    def _mark_health_delivery(
        self,
        transition: HealthTransition,
        state: DeliveryState,
        safe_error: str,
    ) -> None:
        with self._connect() as connection:
            current_transition = self._get_state(
                connection, "health_transition", HealthTransition.NONE
            )
            current_state = self._get_state(
                connection, "health_delivery_state", "none"
            )
            if (
                current_transition != transition.value
                or current_state != DeliveryState.IN_FLIGHT
            ):
                raise ValueError("health delivery is not in flight")
            self._set_state(connection, "health_delivery_state", state.value)
            self._set_state(
                connection, "health_last_delivery_error", safe_error
            )
            if state is DeliveryState.SENT:
                self._set_state(
                    connection,
                    "alert_active",
                    "1" if transition is HealthTransition.ALERT else "0",
                )

    def mark_health_sent(self, transition: HealthTransition) -> None:
        self._mark_health_delivery(transition, DeliveryState.SENT, "")

    def mark_health_retryable(self, transition: HealthTransition) -> None:
        self._mark_health_delivery(
            transition, DeliveryState.PENDING, "rate_limited"
        )

    def mark_health_permanent(self, transition: HealthTransition) -> None:
        self._mark_health_delivery(
            transition, DeliveryState.PERMANENT_FAILED, "rejected"
        )

    def mark_health_uncertain(self, transition: HealthTransition) -> None:
        self._mark_health_delivery(
            transition, DeliveryState.UNCERTAIN, "outcome_unknown"
        )

    def resolve_health_delivery(
        self, transition: str, resolution: str
    ) -> DeliveryState:
        if resolution not in {"sent", "retry"}:
            raise ValueError("resolution must be sent or retry")
        try:
            requested = HealthTransition(transition)
        except ValueError:
            raise ValueError("invalid health transition") from None
        if requested is HealthTransition.NONE:
            raise ValueError("invalid health transition")
        with self._connect() as connection:
            current_transition = self._get_state(
                connection, "health_transition", HealthTransition.NONE
            )
            if current_transition != requested.value:
                raise ValueError("not the current health transition")
            current_state = self._get_state(
                connection, "health_delivery_state", "none"
            )
            if current_state not in {
                DeliveryState.UNCERTAIN,
                DeliveryState.PERMANENT_FAILED,
            }:
                raise ValueError("cannot resolve health delivery in current state")
            state = (
                DeliveryState.SENT
                if resolution == "sent"
                else DeliveryState.PENDING
            )
            self._set_state(connection, "health_delivery_state", state.value)
            self._set_state(connection, "health_last_delivery_error", "")
            if state is DeliveryState.SENT:
                self._set_state(
                    connection,
                    "alert_active",
                    "1" if requested is HealthTransition.ALERT else "0",
                )
        return state

    def status(self) -> dict[str, object]:
        with self._connect() as connection:
            sources = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM sources ORDER BY handle"
                )
            ]
            delivery_counts = {
                row["delivery_state"]: row["count"]
                for row in connection.execute(
                    "SELECT delivery_state, COUNT(*) AS count FROM posts "
                    "WHERE matched = 1 GROUP BY delivery_state"
                )
            }
            delivery_attempts = connection.execute(
                "SELECT COALESCE(SUM(attempt_count), 0) FROM posts"
            ).fetchone()[0]
            outages = int(self._get_state(connection, "full_outages", "0"))
            alert_active = (
                self._get_state(connection, "alert_active", "0") == "1"
            )
            health_transition = self._get_state(
                connection, "health_transition", HealthTransition.NONE
            )
            health_delivery_state = self._get_state(
                connection, "health_delivery_state", "none"
            )
            health_transition_epoch = int(
                self._get_state(connection, "health_transition_epoch", "0")
            )
            health_delivery_attempts = int(
                self._get_state(connection, "health_delivery_attempts", "0")
            )
        return {
            "sources": sources,
            "pending": delivery_counts.get(DeliveryState.PENDING, 0),
            "in_flight": delivery_counts.get(DeliveryState.IN_FLIGHT, 0),
            "sent": delivery_counts.get(DeliveryState.SENT, 0),
            "uncertain": delivery_counts.get(DeliveryState.UNCERTAIN, 0),
            "permanent_failed": delivery_counts.get(
                DeliveryState.PERMANENT_FAILED, 0
            ),
            "delivery_attempts": delivery_attempts,
            "full_outages": outages,
            "alert_active": alert_active,
            "health_transition": health_transition,
            "health_delivery_state": health_delivery_state,
            "health_transition_epoch": health_transition_epoch,
            "health_delivery_attempts": health_delivery_attempts,
        }
