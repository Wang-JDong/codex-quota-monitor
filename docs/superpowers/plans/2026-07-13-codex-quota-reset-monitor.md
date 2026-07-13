# Codex Quota Reset Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a resource-capped hourly service that reads four trusted X timelines through a free authenticated RSSHub session and sends Feishu only for explicit Codex quota reset announcements or monitor outages.

**Architecture:** A systemd timer starts a private RSSHub process through a project-local portable Node.js runtime and then runs a one-shot Python monitor, stopping RSSHub in every exit path. The production monitor uses only Python 3.12 standard-library modules to parse RSS, call Feishu, classify posts, and persist SQLite state. RSSHub is an adapter behind a small interface so it can be replaced without changing classification, persistence, or notification code.

**Tech Stack:** Python 3.12 standard library, SQLite, pytest for local development only, portable Node.js 22.20.0, RSSHub npm package, systemd, Feishu custom bot webhook.

## Global Constraints

- Monitor only `@OpenAI`, `@OpenAIDevs`, `@thsottiaux`, and `@sama`; sources are a manually maintained allowlist.
- Run once per hour; no business notification when there is no matching new post.
- Match completed, in-progress, or explicitly planned Codex quota resets; exclude questions, wishes, negations, periodic-limit explanations, purchase/referral conditions, and generic capacity news.
- First successful fetch for each source establishes a baseline and sends no historical business notifications.
- The same X post can be successfully notified at most once.
- Alert once after three consecutive full outages or immediately on explicit X authentication failure; alert once again after all four sources recover.
- Do not use the paid X API or an LLM classifier.
- Do not install or use Docker, pip packages, venv, Redis, PostgreSQL, Chromium, or a web admin UI on the VPS.
- Do not run `apt upgrade`, change nftables/iptables/UFW/SSH, restart existing services, or modify existing node files.
- Use project-local Node.js 22.20.0; run production Python with `/usr/bin/python3` and `PYTHONPATH`.
- RSSHub and Monitor share one systemd cgroup capped at `MemoryMax=384M` and `CPUQuota=30%`.
- Stop RSSHub and Monitor after every run, including failure and timeout paths.
- Secrets must stay out of Git and logs; RSSHub binds only to `127.0.0.1`.
- Deployment target: Ubuntu 24.04 RackNerd KVM VPS, 2 Xeon cores, 2.5 GB RAM, 45 GB disk.
- Preserve active `ssh`, `sing-box`, `cdn-subscription`, `friend-clash-sub`, and `share-100gb-sub` services and their preflight listener set.

---

## File Map

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | Python package metadata, runtime dependencies, pytest configuration |
| `.gitignore` | Exclude secrets, SQLite, caches, and local runtime data |
| `.env.example` | Secret and runtime variable names with safe example values |
| `config/sources.json` | The four-source allowlist and RSSHub route options |
| `src/codex_quota_monitor/models.py` | Immutable domain models and enums |
| `src/codex_quota_monitor/settings.py` | YAML and environment validation |
| `src/codex_quota_monitor/classifier.py` | Deterministic reset classifier |
| `src/codex_quota_monitor/feed.py` | RSSHub HTTP adapter and Feed parser |
| `src/codex_quota_monitor/store.py` | SQLite schema, baseline, idempotency, pending queue, health state |
| `src/codex_quota_monitor/feishu.py` | Feishu signing, cards, retryable sender |
| `src/codex_quota_monitor/service.py` | One-run orchestration and health transitions |
| `src/codex_quota_monitor/cli.py` | `run`, `dry-run`, `test-notification`, and `status` commands |
| `src/codex_quota_monitor/__main__.py` | Module entry point |
| `tests/` | Focused unit, state, HTTP, orchestration, and CLI tests |
| `rsshub/package.json` | Pin the private RSSHub npm package |
| `deploy/preflight.sh` | Read-only snapshot and invariants for existing node services |
| `deploy/install.sh` | Install only project-local runtime and new units; no system network changes |
| `deploy/run-monitor.sh` | Start private RSSHub, wait, run Monitor, always stop RSSHub |
| `deploy/postflight.sh` | Compare protected services and ports after deployment |
| `deploy/codex-quota-monitor.service` | systemd one-shot unit |
| `deploy/codex-quota-monitor.timer` | Hourly persistent timer |
| `deploy/codex-quota-monitor.logrotate` | Dedicated log size and retention policy |
| `Makefile` | Single-command test, install, run, status, and rollback operations |
| `README.md` | Setup, cookie rotation, operation, resource verification, troubleshooting |

---

### Task 1: Project foundation, domain models, and validated settings

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config/sources.json`
- Create: `src/codex_quota_monitor/__init__.py`
- Create: `src/codex_quota_monitor/models.py`
- Create: `src/codex_quota_monitor/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Produces: `ResetStatus`, `Source`, `Post`, `Decision`, `Notification`, `Settings`.
- Produces: `load_settings(config_path: Path, environ: Mapping[str, str] | None = None) -> Settings`.

- [ ] **Step 1: Write the failing settings test**

```python
# tests/test_settings.py
from pathlib import Path

import pytest

from codex_quota_monitor.settings import load_settings


def test_load_settings_requires_exact_allowlist(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        '{"sources": ['
        '{"handle": "OpenAI"}, {"handle": "OpenAIDevs"}, '
        '{"handle": "thsottiaux"}, {"handle": "sama"}]}',
        encoding="utf-8",
    )
    env = {
        "RSSHUB_BASE_URL": "http://rsshub:1200",
        "DATABASE_PATH": "/data/monitor.db",
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
        "FEISHU_SIGNING_SECRET": "secret",
    }

    settings = load_settings(config, env)

    assert [source.handle for source in settings.sources] == [
        "OpenAI", "OpenAIDevs", "thsottiaux", "sama"
    ]
    assert settings.failure_threshold == 3
    assert settings.feed_count == 20


def test_load_settings_rejects_missing_secret(tmp_path: Path) -> None:
    config = tmp_path / "sources.json"
    config.write_text(
        '{"sources": [{"handle": "OpenAI"}, {"handle": "OpenAIDevs"}, '
        '{"handle": "thsottiaux"}, {"handle": "sama"}]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="FEISHU_SIGNING_SECRET"):
        load_settings(
            config,
            {
                "RSSHUB_BASE_URL": "http://127.0.0.1:1200",
                "DATABASE_PATH": "/data/monitor.db",
                "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/example",
            },
        )
```

- [ ] **Step 2: Run the test and verify the import fails**

Run: `python -m pytest tests/test_settings.py -v`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'codex_quota_monitor'`.

- [ ] **Step 3: Add package metadata and safe configuration files**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "codex-quota-monitor"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[project.optional-dependencies]
test = ["pytest>=8.3,<9"]

[project.scripts]
codex-quota-monitor = "codex_quota_monitor.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/codex_quota_monitor"]

[tool.pytest.ini_options]
addopts = "-q"
testpaths = ["tests"]
```

```gitignore
# .gitignore
.env
.venv/
__pycache__/
.pytest_cache/
*.py[cod]
data/
*.db
*.db-shm
*.db-wal
```

```dotenv
# .env.example
TWITTER_AUTH_TOKEN=replace-with-x-auth_token-cookie-value
RSSHUB_BASE_URL=http://127.0.0.1:1200
DATABASE_PATH=/opt/codex-quota-monitor/data/monitor.db
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/replace-me
FEISHU_SIGNING_SECRET=replace-me
LOG_LEVEL=INFO
```

```json
{
  "feed_count": 20,
  "failure_threshold": 3,
  "request_timeout_seconds": 20,
  "request_retries": 3,
  "sources": [
    {"handle": "OpenAI", "include_replies": true},
    {"handle": "OpenAIDevs", "include_replies": true},
    {"handle": "thsottiaux", "include_replies": true},
    {"handle": "sama", "include_replies": true}
  ]
}
```

- [ ] **Step 4: Implement models and settings validation**

```python
# src/codex_quota_monitor/models.py
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ResetStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    PLANNED = "planned"


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


@dataclass(frozen=True)
class Notification:
    title: str
    body: str
    template: str
    url: str = ""
```

```python
# src/codex_quota_monitor/settings.py
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping
import os

from .models import Source


EXPECTED_HANDLES = ("OpenAI", "OpenAIDevs", "thsottiaux", "sama")


@dataclass(frozen=True)
class Settings:
    sources: tuple[Source, ...]
    rsshub_base_url: str
    database_path: Path
    feishu_webhook_url: str
    feishu_signing_secret: str
    feed_count: int = 20
    failure_threshold: int = 3
    request_timeout_seconds: int = 20
    request_retries: int = 3


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def load_settings(
    config_path: Path, environ: Mapping[str, str] | None = None
) -> Settings:
    env = os.environ if environ is None else environ
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    sources = tuple(
        Source(str(item["handle"]), bool(item.get("include_replies", True)))
        for item in raw.get("sources", [])
    )
    handles = tuple(source.handle for source in sources)
    if handles != EXPECTED_HANDLES:
        raise ValueError(f"sources must be exactly {EXPECTED_HANDLES!r}, got {handles!r}")
    base_url = _required(env, "RSSHUB_BASE_URL").rstrip("/")
    webhook = _required(env, "FEISHU_WEBHOOK_URL")
    secret = _required(env, "FEISHU_SIGNING_SECRET")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("RSSHUB_BASE_URL must be http(s)")
    if not webhook.startswith("https://open.feishu.cn/"):
        raise ValueError("FEISHU_WEBHOOK_URL must use open.feishu.cn")
    return Settings(
        sources=sources,
        rsshub_base_url=base_url,
        database_path=Path(_required(env, "DATABASE_PATH")),
        feishu_webhook_url=webhook,
        feishu_signing_secret=secret,
        feed_count=int(raw.get("feed_count", 20)),
        failure_threshold=int(raw.get("failure_threshold", 3)),
        request_timeout_seconds=int(raw.get("request_timeout_seconds", 20)),
        request_retries=int(raw.get("request_retries", 3)),
    )
```

Create empty `src/codex_quota_monitor/__init__.py`.

- [ ] **Step 5: Install and run the settings tests**

Run: `python -m pip install -e '.[test]' && python -m pytest tests/test_settings.py -v`

Expected: 2 tests PASS.

- [ ] **Step 6: Commit the foundation**

```bash
git add pyproject.toml .gitignore .env.example config src tests/test_settings.py
git commit -m "chore: scaffold quota monitor"
```

---

### Task 2: Deterministic reset classifier

**Files:**
- Create: `src/codex_quota_monitor/classifier.py`
- Test: `tests/test_classifier.py`

**Interfaces:**
- Consumes: `Post`, `Decision`, `ResetStatus` from Task 1.
- Produces: `classify(post: Post, trusted_handles: frozenset[str]) -> Decision`.

- [ ] **Step 1: Write positive and negative classification tests**

```python
# tests/test_classifier.py
from datetime import UTC, datetime

import pytest

from codex_quota_monitor.classifier import classify
from codex_quota_monitor.models import Post, ResetStatus


TRUSTED = frozenset({"openai", "openaidevs", "thsottiaux", "sama"})


def post(text: str, author: str = "thsottiaux") -> Post:
    return Post("1", author, text, datetime(2026, 6, 16, tzinfo=UTC), "https://x.com/x/status/1")


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("We have reset Codex usage limits across all plans.", ResetStatus.COMPLETED),
        ("We are resetting the Codex rate limits now.", ResetStatus.IN_PROGRESS),
        ("Give us 24 hours to reset the Codex rate limits.", ResetStatus.PLANNED),
        ("We will credit Codex users one additional reset.", ResetStatus.PLANNED),
    ],
)
def test_matches_explicit_reset_announcements(text: str, status: ResetStatus) -> None:
    assert classify(post(text), TRUSTED).status is status


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


def test_rejects_untrusted_author() -> None:
    assert classify(post("We reset Codex usage limits", "randomuser"), TRUSTED).matched is False
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `python -m pytest tests/test_classifier.py -v`

Expected: FAIL during collection because `classifier.py` does not exist.

- [ ] **Step 3: Implement the conservative classifier**

```python
# src/codex_quota_monitor/classifier.py
import re

from .models import Decision, Post, ResetStatus


PRODUCT = re.compile(r"\bcodex\b", re.I)
LIMIT = re.compile(r"\b(rate|usage)\s+limits?\b|\bquota\b|\bcodex\s+usage\b", re.I)
RESET = re.compile(r"\breset(?:s|ting)?\b|\breset\s+button\s+pressed\b|\bcredit\b.{0,40}\breset\b", re.I)
EXCLUDE = re.compile(
    r"\bshould\s+we\b|\bwould\s+you\b|\bif\s+we\b|\bplease\b|\bhope\b|"
    r"\bnot\s+(?:been\s+)?reset\b|\bdo\s+not\s+reset\b|\bdon't\s+reset\b|"
    r"\bwill\s+not\s+reset\b|\bwon't\s+reset\b|\breset\s+failed\b|\binvite\b|\breferr?al\b|"
    r"\bbuy\b|\bpurchase\b|\bwhen\s+do(?:es)?\b",
    re.I,
)
COMPLETED = re.compile(r"\bhave\s+reset\b|\bhas\s+reset\b|\bwere\s+reset\b|\bare\s+reset\b|\breset\s+button\s+pressed\b", re.I)
IN_PROGRESS = re.compile(r"\b(?:are|'re)\s+resetting\b|\bis\s+resetting\b", re.I)
PLANNED = re.compile(r"\bwill\s+(?:be\s+)?reset\b|\bwill\s+reset\b|\bto\s+reset\b|\badditional\s+reset\b|\bcredit\b.{0,40}\breset\b", re.I)


def classify(post: Post, trusted_handles: frozenset[str]) -> Decision:
    if post.author.casefold() not in trusted_handles:
        return Decision(False, None, "author_not_trusted")
    text = " ".join(part for part in (post.text, post.quoted_text) if part).strip()
    if EXCLUDE.search(text):
        return Decision(False, None, "excluded_language", ("exclude",))
    checks = {
        "product": bool(PRODUCT.search(text)),
        "limit": bool(LIMIT.search(text)),
        "reset": bool(RESET.search(text)),
    }
    if not all(checks.values()):
        missing = ",".join(name for name, found in checks.items() if not found)
        return Decision(False, None, f"missing:{missing}")
    if COMPLETED.search(text):
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
    return Decision(True, status, "explicit_codex_limit_reset", ("product", "limit", "reset", state_rule))
```

- [ ] **Step 4: Run classifier tests**

Run: `python -m pytest tests/test_classifier.py -v`

Expected: all parameterized cases PASS.

- [ ] **Step 5: Commit the classifier**

```bash
git add src/codex_quota_monitor/classifier.py tests/test_classifier.py
git commit -m "feat: classify codex reset announcements"
```

---

### Task 3: RSSHub feed adapter and parser

**Files:**
- Create: `src/codex_quota_monitor/feed.py`
- Create: `tests/fixtures/twitter-user.xml`
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: `Settings`, `Source`, `Post`.
- Produces: `FeedError(message: str, auth_failed: bool = False)`.
- Produces: `RssHubClient.fetch(source: Source) -> list[Post]`.
- RSSHub route: `/twitter/user/{handle}/includeReplies=1&includeRts=0&count=20&readable=1&showQuotedInTitle=0`.

- [ ] **Step 1: Add a representative RSS fixture and failing tests**

```xml
<!-- tests/fixtures/twitter-user.xml -->
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Twitter @Tibo</title>
<item>
  <title>Give us 24 hours to reset the Codex rate limits across all plans.</title>
  <link>https://x.com/thsottiaux/status/2066956441173323943</link>
  <guid>https://twitter.com/thsottiaux/status/2066956441173323943</guid>
  <pubDate>Tue, 16 Jun 2026 12:00:00 GMT</pubDate>
  <description><![CDATA[Give us 24 hours to reset the Codex rate limits across all plans.]]></description>
</item>
</channel></rss>
```

```python
# tests/test_feed.py
from pathlib import Path
from unittest.mock import patch

from codex_quota_monitor.feed import FeedError, RssHubClient
from codex_quota_monitor.models import Source


def test_fetches_posts_from_exact_lightweight_route() -> None:
    xml = Path("tests/fixtures/twitter-user.xml").read_text(encoding="utf-8")
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self): return xml.encode()
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response()) as mocked:
        posts = client.fetch(Source("thsottiaux", True))

    requested_url = mocked.call_args.args[0].full_url
    assert "includeReplies=1" in requested_url
    assert "includeRts=0" in requested_url
    assert posts[0].post_id == "2066956441173323943"
    assert posts[0].author == "thsottiaux"


def test_marks_authentication_failure() -> None:
    from urllib.error import HTTPError
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    error = HTTPError("http://rsshub", 401, "Unauthorized", {}, None)
    try:
        with patch("codex_quota_monitor.feed.urlopen", side_effect=error):
            client.fetch(Source("OpenAI"))
    except FeedError as exc:
        assert exc.auth_failed is True
    else:
        raise AssertionError("FeedError was not raised")
```

- [ ] **Step 2: Run and verify the missing module failure**

Run: `python -m pytest tests/test_feed.py -v`

Expected: FAIL because `codex_quota_monitor.feed` does not exist.

- [ ] **Step 3: Implement low-frequency HTTP fetching and parsing**

```python
# src/codex_quota_monitor/feed.py
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import Post, Source


POST_ID = re.compile(r"/status/(\d+)")
HANDLE_LINK = re.compile(r"https://x\.com/([^/]+)/status/")


class QuoteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.quote_depth: int | None = None
        self.text: list[str] = []
        self.author = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        values = dict(attrs)
        if tag == "div" and "rsshub-quote" in values.get("class", "").split():
            self.quote_depth = self.depth
        if self.quote_depth is not None and tag == "a":
            match = HANDLE_LINK.search(values.get("href", ""))
            if match:
                self.author = match.group(1)

    def handle_endtag(self, _tag: str) -> None:
        if self.quote_depth == self.depth:
            self.quote_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.quote_depth is not None and data.strip():
            self.text.append(data.strip())


class FeedError(RuntimeError):
    def __init__(self, message: str, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


class RssHubClient:
    def __init__(self, base_url: str, timeout: int, retries: int, count: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.count = count

    def _url(self, source: Source) -> str:
        replies = "1" if source.include_replies else "0"
        params = (
            f"includeReplies={replies}&includeRts=0&count={self.count}"
            "&readable=1&showQuotedInTitle=0"
        )
        return f"{self.base_url}/twitter/user/{source.handle}/{params}"

    def fetch(self, source: Source) -> list[Post]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = Request(self._url(source), headers={"Accept": "application/rss+xml", "User-Agent": "codex-quota-monitor/0.1"})
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                root = ElementTree.fromstring(body)
                items = root.findall("./channel/item")
                if not items:
                    raise FeedError(f"empty feed for @{source.handle}")
                return [self._post(source, item) for item in items]
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise FeedError("X authentication failed", auth_failed=True) from exc
                last_error = exc
            except FeedError:
                raise
            except (URLError, TimeoutError, ElementTree.ParseError, ValueError) as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                time.sleep(2**attempt)
        raise FeedError(f"feed fetch failed for @{source.handle}: {last_error}")

    @staticmethod
    def _post(source: Source, item: ElementTree.Element) -> Post:
        link = item.findtext("link", "")
        match = POST_ID.search(link)
        if not match:
            raise ValueError(f"entry has no X status ID: {link}")
        link_author = HANDLE_LINK.search(link)
        if not link_author or link_author.group(1).casefold() != source.handle.casefold():
            raise ValueError(f"entry author does not match trusted route @{source.handle}: {link}")
        title = re.sub(r"<[^>]+>", " ", unescape(item.findtext("title", "")))
        title = " ".join(title.split())
        title = re.sub(r"^Re\s+", "", title)
        quote = QuoteParser()
        quote.feed(item.findtext("description", ""))
        published_text = item.findtext("pubDate", "")
        published_at = parsedate_to_datetime(published_text).astimezone(UTC) if published_text else datetime.now(UTC)
        return Post(
            post_id=match.group(1),
            author=source.handle,
            text=title,
            published_at=published_at,
            url=link,
            quoted_text=" ".join(quote.text) if quote.author.casefold() in {"openai", "openaidevs", "thsottiaux", "sama"} else "",
            quoted_author=quote.author,
            is_retweet=title.startswith("RT "),
        )
```

- [ ] **Step 4: Run feed tests and the full suite**

Run: `python -m pytest tests/test_feed.py -v && python -m pytest`

Expected: feed tests PASS; full suite PASS.

- [ ] **Step 5: Commit the feed adapter**

```bash
git add src/codex_quota_monitor/feed.py tests/test_feed.py tests/fixtures/twitter-user.xml
git commit -m "feat: fetch trusted x timelines through rsshub"
```

---

### Task 4: SQLite baseline, idempotency, pending queue, and health state

**Files:**
- Create: `src/codex_quota_monitor/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Post`, `Decision`, `ResetStatus`, `HealthTransition`.
- Produces: `StoredMatch(post: Post, decision: Decision)`.
- Produces: `Store.initialize()`, `run_lock()`, `is_source_baselined()`, `baseline_source()`, `unseen()`, `record_decision()`, `pending()`, `mark_pushed()`, `record_source_result()`, `update_health()`, `ack_health()`, and `status()`.

- [ ] **Step 1: Write state and health tests**

```python
# tests/test_store.py
from datetime import UTC, datetime

import pytest

from codex_quota_monitor.models import Decision, HealthTransition, Post, ResetStatus
from codex_quota_monitor.store import Store


def sample(post_id: str = "1") -> Post:
    return Post(post_id, "OpenAI", "We reset Codex usage limits", datetime.now(UTC), f"https://x.com/OpenAI/status/{post_id}")


def test_baseline_makes_existing_posts_seen_without_pending(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    store.baseline_source("OpenAI", [sample()])

    assert store.is_source_baselined("OpenAI") is True
    assert store.unseen([sample()]) == []
    assert store.pending() == []


def test_matched_post_is_pending_until_marked_pushed(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()
    decision = Decision(True, ResetStatus.COMPLETED, "explicit", ("reset",))
    store.record_decision(sample(), decision)

    assert [item.post.post_id for item in store.pending()] == ["1"]
    store.mark_pushed("1")
    assert store.pending() == []


def test_health_alerts_once_after_three_full_outages_and_recovers_once(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    store.initialize()

    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(0, 4, False) is HealthTransition.ALERT
    store.ack_health(HealthTransition.ALERT)
    assert store.update_health(0, 4, False) is HealthTransition.NONE
    assert store.update_health(4, 4, False) is HealthTransition.RECOVERED
    store.ack_health(HealthTransition.RECOVERED)
    assert store.update_health(4, 4, False) is HealthTransition.NONE


def test_run_lock_rejects_overlapping_run(tmp_path) -> None:
    store = Store(tmp_path / "monitor.db", failure_threshold=3)
    with store.run_lock():
        with pytest.raises(RuntimeError, match="another monitor run"):
            with store.run_lock():
                pass
```

- [ ] **Step 2: Run and verify the missing module failure**

Run: `python -m pytest tests/test_store.py -v`

Expected: FAIL because `store.py` does not exist.

- [ ] **Step 3: Implement the SQLite store**

```python
# src/codex_quota_monitor/store.py
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
from pathlib import Path
import sqlite3

from .models import Decision, HealthTransition, Post, ResetStatus


@dataclass(frozen=True)
class StoredMatch:
    post: Post
    decision: Decision


class Store:
    def __init__(self, path: Path, failure_threshold: int) -> None:
        self.path = path
        self.failure_threshold = failure_threshold

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    post_id TEXT PRIMARY KEY, author TEXT NOT NULL, text TEXT NOT NULL,
                    quoted_text TEXT NOT NULL, published_at TEXT NOT NULL, url TEXT NOT NULL,
                    matched INTEGER NOT NULL, status TEXT, reason TEXT NOT NULL,
                    matched_rules TEXT NOT NULL, pushed INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sources (
                    handle TEXT PRIMARY KEY, baselined INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT, last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )

    @contextmanager
    def run_lock(self):
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
        with self._connect() as conn:
            row = conn.execute("SELECT baselined FROM sources WHERE handle = ?", (handle,)).fetchone()
        return bool(row and row["baselined"])

    def baseline_source(self, handle: str, posts: list[Post]) -> None:
        with self._connect() as conn:
            for post in posts:
                self._insert(conn, post, Decision(False, None, "baseline"))
            conn.execute(
                "INSERT INTO sources(handle, baselined) VALUES(?, 1) "
                "ON CONFLICT(handle) DO UPDATE SET baselined = 1",
                (handle,),
            )

    def unseen(self, posts: list[Post]) -> list[Post]:
        with self._connect() as conn:
            return [post for post in posts if conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post.post_id,)).fetchone() is None]

    def _insert(self, conn: sqlite3.Connection, post: Post, decision: Decision) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO posts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                post.post_id, post.author, post.text, post.quoted_text,
                post.published_at.isoformat(), post.url, int(decision.matched),
                decision.status.value if decision.status else None, decision.reason,
                json.dumps(decision.matched_rules), datetime.now(UTC).isoformat(),
            ),
        )

    def record_decision(self, post: Post, decision: Decision) -> None:
        with self._connect() as conn:
            self._insert(conn, post, decision)

    def pending(self) -> list[StoredMatch]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM posts WHERE matched = 1 AND pushed = 0 ORDER BY published_at").fetchall()
        return [
            StoredMatch(
                Post(row["post_id"], row["author"], row["text"], datetime.fromisoformat(row["published_at"]), row["url"], row["quoted_text"]),
                Decision(True, ResetStatus(row["status"]), row["reason"], tuple(json.loads(row["matched_rules"]))),
            )
            for row in rows
        ]

    def mark_pushed(self, post_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE posts SET pushed = 1 WHERE post_id = ?", (post_id,))

    def record_source_result(self, handle: str, success: bool, error: str = "") -> None:
        with self._connect() as conn:
            if success:
                conn.execute(
                    "INSERT INTO sources(handle, last_success_at, last_error) VALUES(?, ?, '') "
                    "ON CONFLICT(handle) DO UPDATE SET last_success_at = excluded.last_success_at, last_error = ''",
                    (handle, datetime.now(UTC).isoformat()),
                )
            else:
                conn.execute(
                    "INSERT INTO sources(handle, last_error) VALUES(?, ?) "
                    "ON CONFLICT(handle) DO UPDATE SET last_error = excluded.last_error",
                    (handle, error[:500]),
                )

    def _get_state(self, conn: sqlite3.Connection, key: str, default: str) -> str:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _set_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute("INSERT INTO state VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def update_health(self, successful_sources: int, total_sources: int, auth_failed: bool) -> HealthTransition:
        with self._connect() as conn:
            failures = int(self._get_state(conn, "full_outages", "0"))
            alert_active = self._get_state(conn, "alert_active", "0") == "1"
            if auth_failed:
                failures = self.failure_threshold
            elif successful_sources == 0:
                failures += 1
            else:
                failures = 0
            transition = HealthTransition.NONE
            if not alert_active and failures >= self.failure_threshold:
                transition = HealthTransition.ALERT
            elif alert_active and successful_sources == total_sources:
                transition = HealthTransition.RECOVERED
            self._set_state(conn, "full_outages", str(failures))
            return transition

    def ack_health(self, transition: HealthTransition) -> None:
        if transition is HealthTransition.NONE:
            return
        with self._connect() as conn:
            self._set_state(conn, "alert_active", "1" if transition is HealthTransition.ALERT else "0")

    def status(self) -> dict[str, object]:
        with self._connect() as conn:
            sources = [dict(row) for row in conn.execute("SELECT * FROM sources ORDER BY handle")]
            pending = conn.execute("SELECT COUNT(*) FROM posts WHERE matched = 1 AND pushed = 0").fetchone()[0]
            outages = int(self._get_state(conn, "full_outages", "0"))
            alert_active = self._get_state(conn, "alert_active", "0") == "1"
        return {"sources": sources, "pending": pending, "full_outages": outages, "alert_active": alert_active}
```

- [ ] **Step 4: Run store tests and full regression**

Run: `python -m pytest tests/test_store.py -v && python -m pytest`

Expected: all tests PASS.

- [ ] **Step 5: Commit persistence**

```bash
git add src/codex_quota_monitor/store.py tests/test_store.py
git commit -m "feat: persist monitor state and health"
```

---

### Task 5: Signed Feishu cards with retry behavior

**Files:**
- Create: `src/codex_quota_monitor/feishu.py`
- Test: `tests/test_feishu.py`

**Interfaces:**
- Consumes: `Notification`, `Post`, `Decision`.
- Produces: `notification_for_post(post, decision) -> Notification`.
- Produces: `health_notification(transition) -> Notification`.
- Produces: `FeishuClient.send(notification: Notification) -> None`.

- [ ] **Step 1: Write signing, card, and retry tests**

```python
# tests/test_feishu.py
from datetime import UTC, datetime
import json
from unittest.mock import patch

from codex_quota_monitor.feishu import FeishuClient, notification_for_post
from codex_quota_monitor.models import Decision, Post, ResetStatus


def test_sends_signed_interactive_card() -> None:
    webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/example"
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return None
        def read(self): return b'{"code": 0, "msg": "success"}'
    client = FeishuClient(webhook, "secret", retries=1, timeout=10)
    post = Post("1", "sama", "We will reset Codex usage limits.", datetime(2026, 7, 13, tzinfo=UTC), "https://x.com/sama/status/1")
    note = notification_for_post(post, Decision(True, ResetStatus.PLANNED, "explicit"))

    with patch("codex_quota_monitor.feishu.urlopen", return_value=Response()) as mocked:
        client.send(note)

    payload = json.loads(mocked.call_args.args[0].data)
    assert payload["msg_type"] == "interactive"
    assert payload["timestamp"] and payload["sign"]
    assert "计划重置" in payload["card"]["header"]["title"]["content"]
```

- [ ] **Step 2: Run and verify the missing module failure**

Run: `python -m pytest tests/test_feishu.py -v`

Expected: FAIL because `feishu.py` does not exist.

- [ ] **Step 3: Implement signatures, cards, and bounded retry**

```python
# src/codex_quota_monitor/feishu.py
import base64
import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import Decision, HealthTransition, Notification, Post, ResetStatus


LABELS = {
    ResetStatus.COMPLETED: "已经重置",
    ResetStatus.IN_PROGRESS: "正在重置",
    ResetStatus.PLANNED: "计划重置",
}


def notification_for_post(post: Post, decision: Decision) -> Notification:
    if decision.status is None:
        raise ValueError("matched decision requires status")
    label = LABELS[decision.status]
    beijing = post.published_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    body = f"**状态：** {label}\n**来源：** @{post.author}\n**时间：** {beijing}（北京时间）\n\n**官方原文：**\n{post.text[:1500]}"
    return Notification(f"Codex 额度重置通知｜{label}", body, "green", post.url)


def health_notification(transition: HealthTransition) -> Notification:
    if transition is HealthTransition.ALERT:
        return Notification("Codex 额度监控异常", "四个可信来源已连续三轮无法抓取，或 X 登录会话已失效。请检查日志并更新 Cookie。", "red")
    if transition is HealthTransition.RECOVERED:
        return Notification("Codex 额度监控已恢复", "四个可信来源均已恢复正常抓取。", "blue")
    raise ValueError("NONE has no notification")


class FeishuClient:
    def __init__(self, webhook_url: str, secret: str, retries: int, timeout: int) -> None:
        self.webhook_url = webhook_url
        self.secret = secret
        self.retries = retries
        self.timeout = timeout

    def _signature(self, timestamp: str) -> str:
        key = f"{timestamp}\n{self.secret}".encode()
        digest = hmac.new(key, digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def send(self, notification: Notification) -> None:
        timestamp = str(int(time.time()))
        elements: list[dict] = [{"tag": "div", "text": {"tag": "lark_md", "content": notification.body}}]
        if notification.url:
            elements.append({"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "查看 X 原帖"}, "url": notification.url, "type": "primary"}]})
        payload = {
            "timestamp": timestamp,
            "sign": self._signature(timestamp),
            "msg_type": "interactive",
            "card": {"header": {"template": notification.template, "title": {"tag": "plain_text", "content": notification.title}}, "elements": elements},
        }
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = Request(
                    self.webhook_url,
                    data=json.dumps(payload, ensure_ascii=False).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read())
                if result.get("code", 0) != 0:
                    raise RuntimeError(f"Feishu rejected request: {result.get('msg', 'unknown')}")
                return
            except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(2**attempt)
        raise RuntimeError(f"Feishu send failed: {last_error}")
```

- [ ] **Step 4: Run Feishu and regression tests**

Run: `python -m pytest tests/test_feishu.py -v && python -m pytest`

Expected: all tests PASS.

- [ ] **Step 5: Commit notifications**

```bash
git add src/codex_quota_monitor/feishu.py tests/test_feishu.py
git commit -m "feat: send signed feishu notifications"
```

---

### Task 6: One-run orchestration and CLI

**Files:**
- Create: `src/codex_quota_monitor/service.py`
- Create: `src/codex_quota_monitor/cli.py`
- Create: `src/codex_quota_monitor/__main__.py`
- Test: `tests/test_service.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: all Tasks 1–5 interfaces.
- Produces: `MonitorService.run(dry_run: bool = False) -> RunSummary`.
- Produces CLI commands: `run`, `dry-run`, `test-notification`, `status`.

- [ ] **Step 1: Write orchestration tests for baseline, match, dedupe, and outage**

```python
# tests/test_service.py
from datetime import UTC, datetime

from codex_quota_monitor.models import Post, Source
from codex_quota_monitor.service import MonitorService
from codex_quota_monitor.store import Store


class FakeFeed:
    def __init__(self, posts): self.posts = posts
    def fetch(self, source): return list(self.posts.get(source.handle, []))


class FakeFeishu:
    def __init__(self): self.sent = []
    def send(self, note): self.sent.append(note)


def test_first_run_baselines_then_new_match_sends_once(tmp_path) -> None:
    source = Source("OpenAI")
    old = Post("1", "OpenAI", "We reset Codex usage limits", datetime.now(UTC), "https://x.com/OpenAI/status/1")
    feed = FakeFeed({"OpenAI": [old]})
    feishu = FakeFeishu()
    store = Store(tmp_path / "db.sqlite", 3)
    store.initialize()
    service = MonitorService((source,), feed, store, feishu)

    service.run()
    assert feishu.sent == []

    new = Post("2", "OpenAI", "We have reset Codex usage limits across all plans", datetime.now(UTC), "https://x.com/OpenAI/status/2")
    feed.posts["OpenAI"] = [new, old]
    service.run()
    service.run()

    assert len(feishu.sent) == 1
    assert "已经重置" in feishu.sent[0].title
```

- [ ] **Step 2: Run and verify the missing service failure**

Run: `python -m pytest tests/test_service.py -v`

Expected: FAIL because `service.py` does not exist.

- [ ] **Step 3: Implement orchestration with per-source baselines and health transitions**

```python
# src/codex_quota_monitor/service.py
from dataclasses import dataclass
import logging

from .classifier import classify
from .feed import FeedError, RssHubClient
from .feishu import FeishuClient, health_notification, notification_for_post
from .models import Source
from .store import Store


@dataclass(frozen=True)
class RunSummary:
    fetched_sources: int
    new_posts: int
    matched_posts: int
    sent_posts: int


class MonitorService:
    def __init__(self, sources: tuple[Source, ...], feed: RssHubClient, store: Store, feishu: FeishuClient) -> None:
        self.sources = sources
        self.feed = feed
        self.store = store
        self.feishu = feishu
        self.trusted = frozenset(source.handle.casefold() for source in sources)

    def run(self, dry_run: bool = False) -> RunSummary:
        fetched = new_count = matched = sent = 0
        auth_failed = False
        if not dry_run:
            for item in self.store.pending():
                self.feishu.send(notification_for_post(item.post, item.decision))
                self.store.mark_pushed(item.post.post_id)
                sent += 1
        for source in self.sources:
            try:
                posts = self.feed.fetch(source)
                fetched += 1
                if dry_run:
                    for post in posts:
                        decision = classify(post, self.trusted)
                        logging.info("dry-run @%s %s matched=%s reason=%s", post.author, post.post_id, decision.matched, decision.reason)
                    continue
                self.store.record_source_result(source.handle, True)
                if not self.store.is_source_baselined(source.handle):
                    self.store.baseline_source(source.handle, posts)
                    continue
                for post in self.store.unseen(posts):
                    new_count += 1
                    decision = classify(post, self.trusted)
                    self.store.record_decision(post, decision)
                    if decision.matched:
                        matched += 1
                        self.feishu.send(notification_for_post(post, decision))
                        self.store.mark_pushed(post.post_id)
                        sent += 1
            except FeedError as exc:
                auth_failed = auth_failed or exc.auth_failed
                if not dry_run:
                    self.store.record_source_result(source.handle, False, str(exc))
                logging.error("source @%s failed: %s", source.handle, exc)
        if not dry_run:
            transition = self.store.update_health(fetched, len(self.sources), auth_failed)
            if transition.value != "none":
                self.feishu.send(health_notification(transition))
                self.store.ack_health(transition)
        return RunSummary(fetched, new_count, matched, sent)
```

- [ ] **Step 4: Implement the CLI and entry point**

```python
# src/codex_quota_monitor/cli.py
import argparse
import json
import logging
import os
from pathlib import Path

from .feed import RssHubClient
from .feishu import FeishuClient
from .models import Notification
from .service import MonitorService
from .settings import load_settings
from .store import Store


def build(config: Path) -> tuple[MonitorService, Store, FeishuClient]:
    settings = load_settings(config)
    store = Store(settings.database_path, settings.failure_threshold)
    store.initialize()
    feed = RssHubClient(settings.rsshub_base_url, settings.request_timeout_seconds, settings.request_retries, settings.feed_count)
    feishu = FeishuClient(settings.feishu_webhook_url, settings.feishu_signing_secret, settings.request_retries, settings.request_timeout_seconds)
    return MonitorService(settings.sources, feed, store, feishu), store, feishu


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/opt/codex-quota-monitor/config/sources.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("dry-run")
    sub.add_parser("status")
    sub.add_parser("test-notification")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    service, store, feishu = build(args.config)
    if args.command == "run":
        with store.run_lock():
            print(json.dumps(service.run().__dict__, ensure_ascii=False))
    elif args.command == "dry-run":
        with store.run_lock():
            print(json.dumps(service.run(dry_run=True).__dict__, ensure_ascii=False))
    elif args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
    else:
        feishu.send(Notification("Codex 额度监控｜系统测试", "飞书机器人签名和网络连接正常。", "blue"))
        print("test notification sent")
```

```python
# src/codex_quota_monitor/__main__.py
from .cli import main


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Add a CLI smoke test**

```python
# tests/test_cli.py
import subprocess
import sys


def test_module_help() -> None:
    result = subprocess.run([sys.executable, "-m", "codex_quota_monitor", "--help"], text=True, capture_output=True)
    assert result.returncode == 0
    assert "test-notification" in result.stdout
```

- [ ] **Step 6: Run service, CLI, and full tests**

Run: `python -m pytest tests/test_service.py tests/test_cli.py -v && python -m pytest`

Expected: all tests PASS.

- [ ] **Step 7: Commit orchestration**

```bash
git add src/codex_quota_monitor/service.py src/codex_quota_monitor/cli.py src/codex_quota_monitor/__main__.py tests/test_service.py tests/test_cli.py
git commit -m "feat: orchestrate hourly quota checks"
```

---

### Task 7: Native systemd deployment with node-service protection

**Files:**
- Create: `rsshub/package.json`
- Create: `deploy/preflight.sh`
- Create: `deploy/install.sh`
- Create: `deploy/run-monitor.sh`
- Create: `deploy/postflight.sh`
- Create: `deploy/codex-quota-monitor.service`
- Create: `deploy/codex-quota-monitor.timer`
- Create: `deploy/codex-quota-monitor.logrotate`
- Create: `Makefile`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: CLI from Task 6, system Python `/usr/bin/python3`, and `.env` from Task 1.
- Produces: isolated runtime under `/opt/codex-quota-monitor`, new units named only `codex-quota-monitor.*`, and `make preflight|install|run|dry-run|postflight|rollback`.

- [ ] **Step 1: Write deployment safety tests**

```python
# tests/test_deploy_files.py
from pathlib import Path


def test_scripts_never_touch_network_or_existing_services() -> None:
    scripts = "\n".join(path.read_text() for path in Path("deploy").glob("*.sh"))
    for forbidden in ("apt ", "apt-get", "iptables", "nft ", "ufw", "systemctl restart", "docker"):
        assert forbidden not in scripts
    assert "sing-box.service" in scripts
    assert "ssh.service" in scripts


def test_service_has_hard_limits_and_sandbox() -> None:
    unit = Path("deploy/codex-quota-monitor.service").read_text()
    assert "MemoryMax=384M" in unit
    assert "CPUQuota=30%" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit


def test_runner_always_kills_private_rsshub() -> None:
    runner = Path("deploy/run-monitor.sh").read_text()
    assert "trap cleanup EXIT INT TERM" in runner
    assert "kill \"$rsshub_pid\"" in runner
    assert "LISTEN_INADDR_ANY=0" in Path("deploy/codex-quota-monitor.service").read_text()
```

- [ ] **Step 2: Run and verify missing files fail**

Run: `python -m pytest tests/test_deploy_files.py -v`

Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Pin RSSHub and add protected-service preflight**

```json
{
  "private": true,
  "dependencies": {"rsshub": "1.0.0-master.4436842"}
}
```

```bash
#!/usr/bin/env bash
# deploy/preflight.sh
set -euo pipefail
root=/opt/codex-quota-monitor
mkdir -p "$root/data"
snapshot="$root/data/preflight.tsv"
: > "$snapshot"
services=(ssh.service sing-box.service cdn-subscription.service friend-clash-sub.service share-100gb-sub.service)
ports=(22 22222 2082 2086 2095 2052 8880)

if ss -H -ltn 'sport = :1200' | grep -q .; then
  echo "refusing install: port 1200 is already in use" >&2
  exit 1
fi
for service in "${services[@]}"; do
  test "$(systemctl is-active "$service")" = active
  path="$(systemctl show -p FragmentPath --value "$service")"
  hash="$(sha256sum "$path" | cut -d' ' -f1)"
  printf '%s\t%s\t%s\n' "$service" "$path" "$hash" >> "$snapshot"
done
for port in "${ports[@]}"; do
  ss -H -ltn "sport = :$port" | grep -q . || { echo "protected port $port is not listening" >&2; exit 1; }
done
free -m
df -h /
echo "preflight passed"
```

- [ ] **Step 4: Add a project-local installer with pinned Node checksum**

```bash
#!/usr/bin/env bash
# deploy/install.sh
set -euo pipefail
test "$(id -u)" = 0 || { echo "run as root" >&2; exit 1; }
root=/opt/codex-quota-monitor
node_version=22.20.0
archive="node-v${node_version}-linux-x64.tar.xz"
sha=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc

"$root/deploy/preflight.sh"
id codex-monitor >/dev/null 2>&1 || useradd --system --home-dir "$root" --shell /usr/sbin/nologin codex-monitor
install -d -o codex-monitor -g codex-monitor -m 0700 "$root/data" /var/log/codex-quota-monitor
install -d -o root -g root -m 0755 "$root/runtime" "$root/rsshub"

if [ ! -x "$root/runtime/node/bin/node" ]; then
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  curl --fail --location --silent --show-error "https://nodejs.org/dist/v${node_version}/${archive}" -o "$tmp"
  echo "$sha  $tmp" | sha256sum --check --status
  tar -xJf "$tmp" -C "$root/runtime"
  ln -sfn "node-v${node_version}-linux-x64" "$root/runtime/node"
fi

cd "$root/rsshub"
systemd-run --quiet --wait --pipe --collect --unit=codex-quota-monitor-install \
  --property=MemoryMax=512M --property=CPUQuota=40% --working-directory="$root/rsshub" \
  --setenv="PATH=$root/runtime/node/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  "$root/runtime/node/bin/npm" install --omit=dev --ignore-scripts --no-audit --no-fund
chmod 640 "$root/.env"
chown root:codex-monitor "$root/.env"
install -m 0644 "$root/deploy/codex-quota-monitor.service" /etc/systemd/system/
install -m 0644 "$root/deploy/codex-quota-monitor.timer" /etc/systemd/system/
install -m 0644 "$root/deploy/codex-quota-monitor.logrotate" /etc/logrotate.d/codex-quota-monitor
systemctl daemon-reload
echo "installed but timer remains disabled until postflight passes"
```

- [ ] **Step 5: Add trap-safe runner and postflight comparison**

```bash
#!/usr/bin/env bash
# deploy/run-monitor.sh
set -euo pipefail
root=/opt/codex-quota-monitor
mode="${1:-run}"
rsshub_pid=""
cleanup() {
  if [ -n "$rsshub_pid" ] && kill -0 "$rsshub_pid" 2>/dev/null; then
    kill "$rsshub_pid" 2>/dev/null || true
    wait "$rsshub_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

"$root/runtime/node/bin/node" "$root/rsshub/node_modules/rsshub/dist/index.mjs" &
rsshub_pid=$!
for _ in $(seq 1 30); do
  curl --fail --silent http://127.0.0.1:1200/healthz >/dev/null && break
  sleep 1
done
curl --fail --silent http://127.0.0.1:1200/healthz >/dev/null
PYTHONPATH="$root/src" /usr/bin/python3 -m codex_quota_monitor --config "$root/config/sources.json" "$mode"
```

```bash
#!/usr/bin/env bash
# deploy/postflight.sh
set -euo pipefail
root=/opt/codex-quota-monitor
snapshot="$root/data/preflight.tsv"
test -s "$snapshot"
while IFS=$'\t' read -r service path expected_hash; do
  test "$(systemctl is-active "$service")" = active
  test "$(sha256sum "$path" | cut -d' ' -f1)" = "$expected_hash"
done < "$snapshot"
for port in 22 22222 2082 2086 2095 2052 8880; do
  ss -H -ltn "sport = :$port" | grep -q . || exit 1
done
if ss -H -ltn 'sport = :1200' | grep -q .; then
  echo "private RSSHub remained running" >&2
  exit 1
fi
echo "postflight passed; existing node services are unchanged"
```

- [ ] **Step 6: Add sandboxed systemd units and log rotation**

```ini
# deploy/codex-quota-monitor.service
[Unit]
Description=Check trusted X accounts for Codex quota resets
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=codex-monitor
Group=codex-monitor
WorkingDirectory=/opt/codex-quota-monitor
EnvironmentFile=/opt/codex-quota-monitor/.env
Environment=PORT=1200
Environment=LISTEN_INADDR_ANY=0
Environment=CACHE_TYPE=memory
Environment=NODE_ENV=production
Environment=NODE_OPTIONS=--max-old-space-size=256
ExecStart=/opt/codex-quota-monitor/deploy/run-monitor.sh run
TimeoutStartSec=5min
MemoryMax=384M
CPUQuota=30%
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/codex-quota-monitor/data /var/log/codex-quota-monitor
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
StandardOutput=append:/var/log/codex-quota-monitor/monitor.log
StandardError=append:/var/log/codex-quota-monitor/monitor.log
```

```ini
# deploy/codex-quota-monitor.timer
[Unit]
Description=Run Codex quota monitor hourly
[Timer]
OnCalendar=hourly
Persistent=true
AccuracySec=1min
Unit=codex-quota-monitor.service
[Install]
WantedBy=timers.target
```

```text
# deploy/codex-quota-monitor.logrotate
/var/log/codex-quota-monitor/monitor.log {
    weekly
    maxsize 5M
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
```

- [ ] **Step 7: Add safe operator commands and rollback**

```makefile
# Makefile
.PHONY: test preflight install run dry-run test-notification status postflight enable resource-check rollback
test:
	python -m pytest
preflight:
	sudo ./deploy/preflight.sh
install:
	sudo ./deploy/install.sh
run:
	sudo systemctl start codex-quota-monitor.service
dry-run:
	sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; /opt/codex-quota-monitor/deploy/run-monitor.sh dry-run'
test-notification:
	sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor test-notification'
status:
	sudo -u codex-monitor bash -c 'set -a; source /opt/codex-quota-monitor/.env; set +a; PYTHONPATH=/opt/codex-quota-monitor/src /usr/bin/python3 -m codex_quota_monitor status'
postflight:
	sudo ./deploy/postflight.sh
enable:
	sudo systemctl enable --now codex-quota-monitor.timer
resource-check:
	systemctl show codex-quota-monitor.service -p MemoryPeak -p MemoryMax -p CPUUsageNSec
rollback:
	sudo systemctl disable --now codex-quota-monitor.timer 2>/dev/null || true
	sudo systemctl stop codex-quota-monitor.service 2>/dev/null || true
	sudo rm -f /etc/systemd/system/codex-quota-monitor.service /etc/systemd/system/codex-quota-monitor.timer /etc/logrotate.d/codex-quota-monitor
	sudo systemctl daemon-reload
```

- [ ] **Step 8: Test scripts, syntax, and forbidden-command invariant**

Run: `chmod +x deploy/*.sh && bash -n deploy/*.sh && python -m pytest tests/test_deploy_files.py -v`

Expected: shell syntax succeeds and 3 deployment tests PASS.

- [ ] **Step 9: Commit deployment**

```bash
git add rsshub deploy Makefile tests/test_deploy_files.py
git commit -m "ops: isolate monitor from existing node services"
```

---

### Task 8: Operator documentation and end-to-end acceptance

**Files:**
- Create: `README.md`
- Modify: `.env.example`
- Test: all tests and live dry-run commands.

**Interfaces:**
- Documents the exact VPS installation path `/opt/codex-quota-monitor` and all Make targets from Task 7.

- [ ] **Step 1: Write README with the complete operator flow**

```markdown
# Codex 额度重置监控

每小时按需启动 RSSHub，检查 `@OpenAI`、`@OpenAIDevs`、`@thsottiaux` 和 `@sama`，仅在官方明确表示 Codex 额度已重置、正在重置或计划重置时推送飞书。

## VPS 安装

1. 使用 `rsync` 将项目放到 `/opt/codex-quota-monitor`，不得覆盖 `/etc/s-box` 或现有 systemd unit。
2. 执行 `make preflight`；只有 SSH、sing-box、三个订阅服务和保护端口全部正常时才继续。
3. 执行 `cp .env.example .env && sudo chown root:codex-monitor .env && chmod 640 .env`；首次安装用户尚未创建时，先由 root 保持 `0600`，安装脚本创建用户后改为 `0640`。
4. 在浏览器登录专用 X 小号，从 `auth_token` Cookie 复制值到 `TWITTER_AUTH_TOKEN`；不要复制密码。
5. 在飞书群添加自定义机器人，启用签名校验，将 Webhook 和签名密钥写入 `.env`。
6. 执行 `make install`；脚本只下载项目私有 Node.js、安装私有 RSSHub 依赖和新增本项目 unit，不安装系统软件。
7. 执行 `make dry-run`，确认四个来源均可读取且不会推送业务消息。
8. 执行 `make test-notification`，确认飞书收到“系统测试”。
9. 执行 `make run` 建立四个来源的首次基线，再执行 `make postflight`。
10. 只有 postflight 通过后才执行 `make enable` 启用每小时定时器。

## 日常操作

- 手动检查：`make run`
- 只看判定：`make dry-run`
- 测试飞书：`make test-notification`
- 查看状态：`make status`
- 查看定时器：`systemctl status codex-quota-monitor.timer`
- 查看最近日志：`tail -n 100 /var/log/codex-quota-monitor/monitor.log`
- 立即运行一次：`systemctl start codex-quota-monitor.service`
- 验证原节点：`make postflight`
- 只移除本项目 unit：`make rollback`

## 更新 X Cookie

编辑 `.env` 中的 `TWITTER_AUTH_TOKEN`，保存新的 `auth_token` Cookie 值，然后执行 `make dry-run`。日志和 Git 中不得出现 Cookie 值。

## 资源验收

运行 `make resource-check`，确认 systemd 显示 `MemoryMax=402653184`（384 MiB）和 `CPUQuota=30%` 对应的限制。任务结束后 `pgrep -af '/opt/codex-quota-monitor/.+rsshub|codex_quota_monitor'` 不得显示残留进程。

## 节点保护边界

本项目不安装 Docker、不执行 apt upgrade、不修改 nftables/iptables/UFW/SSH，不重启或改写 `ssh.service`、`sing-box.service`、`cdn-subscription.service`、`friend-clash-sub.service`、`share-100gb-sub.service`。部署前后必须比较服务文件哈希和端口状态；任何差异都禁止启用 timer。

## 故障语义

没有命中时完全静默。四个来源连续三轮全部失败或 Cookie 明确失效时只告警一次；四个来源全部恢复后只通知一次。飞书发送失败的匹配帖子保留在 SQLite，下一轮优先重试。
```

- [ ] **Step 2: Run static and unit verification**

Run: `python -m pytest -v && git diff --check`

Expected: all tests PASS; `git diff --check` prints nothing.

- [ ] **Step 3: Run local code acceptance**

Run: `python -m pip install -e '.[test]' && python -m pytest -v` on the Mac workspace.

Expected: all local tests PASS; no production dependency other than the Python standard library is installed on the VPS.

- [ ] **Step 4: Run VPS resource acceptance before enabling the timer**

Run on VPS:

```bash
cd /opt/codex-quota-monitor
make preflight
make install
make dry-run
make resource-check
make postflight
```

Expected: preflight and postflight PASS; resource limit is 384 MiB/30%; port 1200 and project processes disappear after dry-run; all protected services and ports remain unchanged.

- [ ] **Step 5: Enable timer and verify one scheduled cycle**

Run: `make test-notification && make run && make postflight && make enable && sudo systemctl start codex-quota-monitor.service && make postflight && systemctl list-timers codex-quota-monitor.timer && tail -n 100 /var/log/codex-quota-monitor/monitor.log`.

Expected: the timer is active with a next-run timestamp; the manual baseline run succeeds; logs contain no secret values; no historical reset card is sent.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md .env.example
git commit -m "docs: add quota monitor operations guide"
```

- [ ] **Step 7: Final verification and handoff**

Run: `python -m pytest -v && git status --short && git log --oneline -8`.

Expected: all tests PASS; only intentional local `.env` and `data/` files are ignored; the log shows one focused commit per task.
