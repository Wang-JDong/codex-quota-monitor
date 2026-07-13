# Banked Reset Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably notify trusted banked Codex reset grants and backfill the known missed post exactly once.

**Architecture:** Preserve full RSSHub main text at ingestion, classify banked grants as a dedicated status, and reuse the existing delivery claim state machine through an idempotent targeted reprocessing command. No additional runtime service is introduced.

**Tech Stack:** Python 3.12, SQLite, pytest, uv, systemd, RSSHub, Feishu webhook

## Global Constraints

- Only configured trusted X handles may match.
- Quoted text and retweets may not supply classification evidence.
- Existing at-most-once delivery semantics must remain intact.
- No new daemon or material VPS resource use.
- Existing proxy and subscription services must remain active.

---

### Task 1: Preserve the complete main post text

**Files:**
- Modify: `src/codex_quota_monitor/feed.py`
- Test: `tests/test_feed.py`

**Interfaces:**
- Consumes: RSSHub title and HTML description fields.
- Produces: `Post.text` containing the fuller original main text while quote fields remain isolated.

- [ ] Add a JSON-feed regression test using the real truncated title and full banked-reset description.
- [ ] Run that test and confirm it fails because `Post.text` ends at `milesto...`.
- [ ] Add a small text-selection helper that prefers a longer nonempty description main text.
- [ ] Run feed tests and confirm quote/retweet behavior remains green.

### Task 2: Classify and render banked reset grants

**Files:**
- Modify: `src/codex_quota_monitor/models.py`
- Modify: `src/codex_quota_monitor/classifier.py`
- Modify: `src/codex_quota_monitor/feishu.py`
- Test: `tests/test_classifier.py`
- Test: `tests/test_feishu.py`

**Interfaces:**
- Produces: `ResetStatus.BANKED_AVAILABLE` with value `banked_available`.
- Produces: a Feishu card labeled `可保存重置次数已发放`.

- [ ] Add a failing classifier test for the full known announcement and negative tests for tutorials, referrals, hypothetical language, and periodic mechanisms.
- [ ] Add a failing Feishu copy test for the new dedicated status.
- [ ] Implement the enum, explicit banked-grant patterns, reason, matched rules, and notification label.
- [ ] Run classifier and Feishu tests until green without weakening existing exclusions.

### Task 3: Add idempotent targeted reprocessing

**Files:**
- Modify: `src/codex_quota_monitor/store.py`
- Modify: `src/codex_quota_monitor/service.py`
- Modify: `src/codex_quota_monitor/cli.py`
- Test: `tests/test_store.py`
- Test: `tests/test_service.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `Store.promote_unmatched(post, decision) -> bool`.
- Produces: `MonitorService.reprocess(post_id) -> ReprocessSummary`.
- Produces: CLI command `reprocess-post <post_id>`.

- [ ] Add failing store tests proving only an existing unmatched row can be atomically promoted and repeated promotion is false.
- [ ] Add failing service tests proving an exact fetched post is sent once and a repeat does not resend.
- [ ] Add a failing CLI test proving the command is locked and emits JSON.
- [ ] Implement the minimal store, service, and CLI paths and run their focused tests.

### Task 4: Documentation and complete local verification

**Files:**
- Modify: `README.md`
- Modify: `docs/product/PRD.md`
- Modify: `docs/architecture/ARCHITECTURE.md`
- Modify: `docs/operations/RUNBOOK.md`
- Modify: `CHANGELOG.md`

- [ ] Document banked-reset semantics and the targeted reprocessing runbook.
- [ ] Run formatting/linting, all pytest tests, `uv lock --check`, CLI help, Node checks, shell syntax checks, and secret scans.
- [ ] Review the diff against the design and commit the verified change.

### Task 5: Safe VPS deployment and one-time backfill

**Files:**
- Deploy the verified repository source to `/opt/codex-quota-monitor` without replacing `.env` or the SQLite database.

- [ ] Capture protected-service, resource-cap, timer, and port state before deployment.
- [ ] Deploy source and install from the locked environment without adding services.
- [ ] Run a VPS dry run and confirm four sources fetch successfully.
- [ ] Run `reprocess-post 2076735790567338203` once and confirm `sent`, `attempt_count=1`, full stored text, and `banked_available`.
- [ ] Repeat the command and confirm no second delivery attempt.
- [ ] Verify the timer, resource caps, protected services, and ports after deployment.
- [ ] Push the verified commit and compare the remote SHA.
