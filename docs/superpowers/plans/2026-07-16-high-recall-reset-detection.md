# High-Recall Codex Reset Detection Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints. Each task is test-first and ends with an independently verifiable result.

Goal: Replace closed-world reset matching with a high-recall, evidence-based classifier that sends a clearly labeled possible_reset Feishu notification for trusted but unfamiliar reset language, while preserving hard safety filters and at-most-once delivery.

Architecture: Keep the existing private RSSHub and four-account allowlist. Refactor classification into hard source/content filtering, normalized evidence collection, candidate gating, and status/confidence assignment. Persist the new status and evidence metadata in SQLite, add bounded unmatched reprocessing, and render high- and low-confidence notifications differently.

Tech Stack: Python 3.12 standard library, SQLite, RSSHub/Node.js adapter, systemd oneshot/timer, Feishu signed webhook, pytest, uv, GitHub Actions.

## Global Constraints

- Trusted authors remain exactly @OpenAI, @OpenAIDevs, @thsottiaux, and @sama.
- Original posts are the only evidence; retweets and quoted text never supply classification evidence.
- possible_reset is a low-confidence signal, never wording that claims the quota definitely reset.
- Production continues using only Python standard-library code; no LLM, model server, or new resident process.
- systemd limits remain MemoryMax=384M and CPUQuota=30%; the timer remains every 30 minutes.
- SQLite delivery claim/state transitions remain at-most-once; uncertain outcomes remain manual-resolution states.
- Deployment must not modify or restart SSH, proxy, subscription, CDN, firewall, or other protected node services.
- Secrets remain only in the VPS .env; never put Cookie, Webhook, or signing-secret values in code, tests, logs, or notifications.
- Every task follows red-green-refactor and creates a focused commit before the next task.

## File Map

- src/codex_quota_monitor/models.py: add POSSIBLE_RESET, Confidence, and decision metadata.
- src/codex_quota_monitor/classifier.py: normalize text, collect evidence, apply hard exclusions, gate candidates, assign state/confidence.
- src/codex_quota_monitor/feishu.py: render low-confidence copy and evidence.
- src/codex_quota_monitor/store.py: migrate SQLite metadata, round-trip decisions, query bounded unmatched history, update reclassification metadata.
- src/codex_quota_monitor/service.py: reclassify current-feed/history candidates and preserve delivery idempotency.
- src/codex_quota_monitor/cli.py: add bounded reprocess-unmatched command.
- deploy/reprocess-unmatched.sh: run bounded reprocessing in an isolated capped transient unit.
- tests/test_classifier.py, tests/test_feishu.py, tests/test_store.py, tests/test_service.py, tests/test_cli.py, tests/test_deploy_files.py: regression and safety coverage.
- docs/operations/RUNBOOK.md, docs/operations/DEPLOYMENT.md, CHANGELOG.md: operator and release documentation.

## Decision Contract

All tasks use this exact contract:

    class ResetStatus(StrEnum):
        COMPLETED = "completed"
        IN_PROGRESS = "in_progress"
        PLANNED = "planned"
        BANKED_AVAILABLE = "banked_available"
        POSSIBLE_RESET = "possible_reset"

    class Confidence(StrEnum):
        HIGH = "high"
        LOW = "low"

    @dataclass(frozen=True)
    class Decision:
        matched: bool
        status: ResetStatus | None
        reason: str
        matched_rules: tuple[str, ...] = ()
        confidence: Confidence = Confidence.HIGH
        candidate_reason: tuple[str, ...] = ()

Existing four-argument Decision calls remain valid through defaults. matched=True means eligible for a notification candidate; it does not mean that the quota definitely reset. POSSIBLE_RESET must always use Confidence.LOW.

---

### Task 1: Add the model contract and classifier red-green tests

Files:
- Modify: src/codex_quota_monitor/models.py
- Modify: tests/test_classifier.py

Interfaces:
- Produces ResetStatus.POSSIBLE_RESET, Confidence, and the Decision fields above.
- Existing four-argument Decision calls remain valid.

- [ ] Step 1: Write failing tests for the new fields and exact real-world miss.

Add tests equivalent to:

    def test_current_real_world_reset_language_is_planned() -> None:
        decision = classify(
            post(
                "Another reset for our Codex and ChatGPT Work users. "
                "Should have that sweet 100% weekly usage limit back in a few minutes."
            ),
            TRUSTED,
        )
        assert decision.matched is True
        assert decision.status is ResetStatus.PLANNED
        assert decision.confidence is Confidence.HIGH

    def test_unfamiliar_trusted_reset_context_becomes_possible_reset() -> None:
        decision = classify(
            post("Codex usage limits may be restored after the team finishes checking."),
            TRUSTED,
        )
        assert decision.matched is True
        assert decision.status is ResetStatus.POSSIBLE_RESET
        assert decision.confidence is Confidence.LOW

Add negative cases for reset questions, explicit negation, tutorials, promotions, untrusted authors, retweets, and quoted-only evidence. Assert candidate_reason contains stable names such as product, limit, action, and time for positives.

- [ ] Step 2: Run the focused tests and verify red.

    uv run --no-sync pytest -q tests/test_classifier.py

Expected: failure because POSSIBLE_RESET, Confidence, and the new evidence behavior do not exist. Do not change production code before observing this failure.

- [ ] Step 3: Add the model types with backward-compatible defaults.

Implement the exact contract above in models.py. Keep Post, Source, Notification, and health state APIs unchanged.

- [ ] Step 4: Run the focused tests again and verify only classifier behavior remains red.

    uv run --no-sync pytest -q tests/test_classifier.py

Expected: import/model failures are gone; evidence-based assertions still fail.

- [ ] Step 5: Commit.

    git add src/codex_quota_monitor/models.py tests/test_classifier.py
    git commit -m "test: define high-recall reset decision contract"

### Task 2: Implement normalized evidence classification

Files:
- Modify: src/codex_quota_monitor/classifier.py
- Test: tests/test_classifier.py

Interfaces:
- Produces a Decision with stable matched_rules, confidence, and candidate_reason values.
- Defines CLASSIFIER_VERSION = "3" for persistence and rule-upgrade detection.
- Private helpers have these signatures:
  normalize(text: str) -> str
  collect_evidence(text: str) -> dict[str, tuple[str, ...]]
  hard_exclusion(text: str) -> tuple[str, ...] | None
  is_candidate(evidence: dict[str, tuple[str, ...]]) -> bool
  infer_state(text: str, evidence: dict[str, tuple[str, ...]]) -> ResetStatus

- [ ] Step 1: Add table-driven tests.

Cover:
- We are once again resetting the usage limits for all Codex users -> IN_PROGRESS.
- Codex usage limits are back to 100% -> COMPLETED.
- Another reset is coming in a few hours for ChatGPT Work and Codex -> PLANNED.
- Codex may have its weekly usage restored after the investigation -> POSSIBLE_RESET.
- The real post 2077607697487188198 -> PLANNED.
- Questions, negations, tutorials, promotions, untrusted authors, retweets, and quote-only evidence -> unmatched.

Assert high-confidence reason explicit_codex_limit_reset and low-confidence reason possible_codex_limit_reset.

- [ ] Step 2: Run the classifier tests and verify red.

    uv run --no-sync pytest -q tests/test_classifier.py

Expected: new cases fail with reset_state_not_explicit or missing-pattern behavior.

- [ ] Step 3: Implement four small classifier stages.

Use the approved rule groups:
- Candidate gate: product + limit + action, or product + action + time/recovery.
- Banked reset requires banked language plus a grant verb.
- State precedence: banked, completed, in-progress, planned, possible.
- Hard exclusions run before candidate scoring and only exclude explicit negation, reset questions, conditional/wish language, failure, promotions, or mechanism explanations.
- Do not use a broad gap that can cross an entire post; do not use quoted text or retweet metadata as evidence.

- [ ] Step 4: Run focused tests and verify green.

    uv run --no-sync pytest -q tests/test_classifier.py

Expected: all classifier positives and negatives pass, including the real post.

- [ ] Step 5: Commit.

    git add src/codex_quota_monitor/classifier.py tests/test_classifier.py
    git commit -m "feat: detect high-recall quota reset candidates"

### Task 3: Persist classification version, confidence, and evidence

Files:
- Modify: src/codex_quota_monitor/store.py
- Test: tests/test_store.py

Interfaces:
- Store.initialize() migrates existing databases without deleting or rewriting delivery state.
- Store.unmatched_since(since: datetime, limit: int) -> list[Post] returns only matched=0 rows ordered by published_at and bounded by limit.
- Store.refresh_unmatched(post: Post, decision: Decision, classifier_version: str) -> bool updates an existing unmatched row without creating delivery unless decision.matched is true.
- Store.promote_unmatched() and pending() round-trip the new metadata.

- [ ] Step 1: Write migration and round-trip tests.

Cover:
1. A legacy posts table gains classification_version, confidence, and candidate_reason safe defaults.
2. A low-confidence decision survives record_decision() and pending() with POSSIBLE_RESET, Confidence.LOW, and its evidence tuple.
3. unmatched_since() honors timestamp and limit.
4. refresh_unmatched() updates still-unmatched rows and promote_unmatched() changes a row exactly once.
5. Existing in_flight, uncertain, sent, and permanent_failed states remain unchanged after migration.

- [ ] Step 2: Run focused tests and verify red.

    uv run --no-sync pytest -q tests/test_store.py

Expected: failures for missing columns/methods and metadata round-trip.

- [ ] Step 3: Add additive SQLite migrations.

Add these columns:

    classification_version TEXT NOT NULL DEFAULT '1'
    confidence TEXT NOT NULL DEFAULT 'high'
    candidate_reason TEXT NOT NULL DEFAULT '[]'

Write candidate_reason as JSON, preserve matched_rules, and include new decision metadata in the content hash used for reclassification. Existing sent rows must not be returned by unmatched_since().

- [ ] Step 4: Implement bounded query and metadata updates.

unmatched_since() must use a parameterized UTC ISO timestamp and SQL LIMIT. refresh_unmatched() updates only matched=0, sets the current classifier version, and leaves delivery columns untouched when still unmatched. promote_unmatched() sets pending delivery and resets only fields required by the existing promotion contract.

- [ ] Step 5: Run focused and full tests.

    uv run --no-sync pytest -q tests/test_store.py
    uv run --no-sync pytest -q

Expected: all tests pass with no delivery-state regressions.

- [ ] Step 6: Commit.

    git add src/codex_quota_monitor/store.py tests/test_store.py
    git commit -m "feat: persist reset classification evidence"

### Task 4: Add confidence-aware Feishu notifications

Files:
- Modify: src/codex_quota_monitor/feishu.py
- Test: tests/test_feishu.py

Interfaces:
- notification_for_post(post, decision) remains the public formatter.
- POSSIBLE_RESET maps to 可能是额度重置，请确认.
- Low-confidence cards use distinct copy and include evidence; high-confidence cards keep existing copy.

- [ ] Step 1: Write failing notification tests.

Assert that a low-confidence decision produces title Codex 额度重置通知｜可能是额度重置，请确认 and a body containing source, Beijing time, original URL, original text, possible_reset, and evidence. Assert existing four high-confidence titles are unchanged.

- [ ] Step 2: Run focused tests and verify red.

    uv run --no-sync pytest -q tests/test_feishu.py

Expected: failure because POSSIBLE_RESET has no label and evidence is absent.

- [ ] Step 3: Implement minimal confidence-aware formatting.

Add the label and a conditional body block. Use classifier evidence already in Decision; do not add a network call or put secrets in the message. Keep the existing 1,500-character cap and X link.

- [ ] Step 4: Run focused and full tests.

    uv run --no-sync pytest -q tests/test_feishu.py
    uv run --no-sync pytest -q

- [ ] Step 5: Commit.

    git add src/codex_quota_monitor/feishu.py tests/test_feishu.py
    git commit -m "feat: label possible reset notifications"

### Task 5: Reclassify current feeds and bounded unmatched history

Files:
- Modify: src/codex_quota_monitor/service.py
- Modify: src/codex_quota_monitor/cli.py
- Test: tests/test_service.py
- Test: tests/test_cli.py

Interfaces:
- Add ReprocessBatchSummary(scanned: int, changed: int, sent: int, skipped: int).
- Add MonitorService.reprocess_unmatched(days: int = 7, limit: int = 100) -> ReprocessBatchSummary.
- Add CLI command: codex-quota-monitor reprocess-unmatched --days 7 --limit 100.

- [ ] Step 1: Write service tests.

Test that an existing unmatched real-world post is promoted and sent once; a second call reports changed=0 and sent=0; an old post outside the window is skipped; sent/matched rows are untouched; and one source failure does not block other sources.

- [ ] Step 2: Run service tests and verify red.

    uv run --no-sync pytest -q tests/test_service.py

Expected: missing batch method or persistence behavior causes failure.

- [ ] Step 3: Implement bounded reprocessing.

The method must validate days 1..31 and limit 1..100, query only unmatched rows newer than the UTC window, fetch each source at most once, index returned posts by post_id, reclassify only rows present in the feed, promote/send only when matched and atomically claimed, count missing/still-negative rows as skipped, and never reclassify rows with an existing successful/uncertain/permanent delivery state.

Keep existing single-post reprocess() and delegate promotion/send to a shared internal helper.

- [ ] Step 4: Add CLI parsing and dispatch tests.

Test defaults days=7 and limit=100, rejection of out-of-range values, JSON summary output, and preservation of all existing commands. Dispatch under the existing Store.run_lock().

- [ ] Step 5: Implement the command and run tests.

    uv run --no-sync pytest -q tests/test_service.py tests/test_cli.py
    uv run --no-sync pytest -q

- [ ] Step 6: Commit.

    git add src/codex_quota_monitor/service.py src/codex_quota_monitor/cli.py tests/test_service.py tests/test_cli.py
    git commit -m "feat: reprocess unmatched reset candidates safely"

### Task 6: Add the capped transient deployment command and operator documentation

Files:
- Create: deploy/reprocess-unmatched.sh
- Modify: tests/test_deploy_files.py
- Modify: docs/operations/RUNBOOK.md
- Modify: docs/operations/DEPLOYMENT.md
- Modify: CHANGELOG.md

Interfaces:
- Root-only ./deploy/reprocess-unmatched.sh [--days 7] [--limit 100].
- Refresh query IDs in the existing root-only transient unit, then run the CLI as codex-monitor in a second transient unit.

- [ ] Step 1: Write deployment-script tests.

Assert set -euo pipefail, root requirement, project-only systemd-run units, MemoryMax=384M, CPUQuota=30%, NoNewPrivileges, PrivateTmp, ProtectSystem=strict, ProtectHome, loopback RSSHUB_BASE_URL, strict numeric bounds, and no protected-service stop or public port.

- [ ] Step 2: Run focused deployment tests and verify red.

    uv run --no-sync pytest -q tests/test_deploy_files.py

- [ ] Step 3: Implement the transient command.

Mirror deploy/reprocess-post.sh safety boundaries. Keep a fixed transient unit name so concurrent backfills fail safely. Pass validated days and limit to the CLI without exposing secrets in process arguments.

- [ ] Step 4: Document release and review procedures.

Add the command to RUNBOOK.md and DEPLOYMENT.md, document JSON fields scanned/changed/sent/skipped, repeat idempotency, low-confidence card review, and the prohibition on direct SQLite edits. Add a CHANGELOG entry. The release sequence runs bounded reprocessing after code rollout and before confirming the next timer cycle.

- [ ] Step 5: Run docs/deployment tests and commit.

    uv run --no-sync pytest -q tests/test_deploy_files.py tests/test_documentation.py tests/test_product_docs.py
    git add deploy/reprocess-unmatched.sh tests/test_deploy_files.py docs/operations/RUNBOOK.md docs/operations/DEPLOYMENT.md CHANGELOG.md
    git commit -m "ops: add bounded unmatched reset reprocessing"

### Task 7: Add rule-version rollout and current-feed refresh

Files:
- Modify: src/codex_quota_monitor/service.py
- Modify: src/codex_quota_monitor/store.py
- Test: tests/test_service.py
- Test: tests/test_store.py
- Modify: docs/operations/RUNBOOK.md

Interfaces:
- Every classified post records CLASSIFIER_VERSION.
- On a classifier-version change, unmatched rows visible in the current feed are refreshed once; the bounded deployment command handles the recent-history window.

- [ ] Step 1: Write version-rollout tests.

Test that a row written under version 1 is re-evaluated once when version 3 is active, an unchanged negative row records version 3 without entering delivery, and matched/sent rows are never re-evaluated.

- [ ] Step 2: Run focused tests and verify red.

    uv run --no-sync pytest -q tests/test_service.py tests/test_store.py

- [ ] Step 3: Implement version-aware current-feed refresh.

At the end of each source fetch, use stored classification_version to decide whether an existing unmatched row needs one refresh. Keep it bounded by feed_count and use the same atomic promotion/send path as new posts.

- [ ] Step 4: Run the complete local gate.

    set -e
    uv run --no-sync pytest -q
    uv lock --check
    uv run --no-sync codex-quota-monitor --help >/dev/null
    uv run --no-sync python -m compileall -q src tests
    find deploy -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
    node --check rsshub/server.mjs
    git diff --check

Expected: all tests pass and every command exits 0.

- [ ] Step 5: Commit.

    git add src/codex_quota_monitor/service.py src/codex_quota_monitor/store.py tests/test_service.py tests/test_store.py docs/operations/RUNBOOK.md
    git commit -m "feat: make classifier upgrades reprocess candidates"

### Task 8: CI, VPS dry-run, backfill, and production acceptance

Files:
- No source files; deploy the committed tree using existing safe procedures.

Before running the commands below, export the VPS address locally (do not commit
the value to the repository):

    export VPS_HOST="<your-vps-host>"

- [ ] Step 1: Push and watch GitHub Actions.

    git push origin main
    RUN_ID="$(gh run list --repo Wang-JDong/codex-quota-monitor --workflow CI --branch main --limit 1 --json databaseId --jq '.[0].databaseId')"
    gh run watch "$RUN_ID" --repo Wang-JDong/codex-quota-monitor --exit-status

Do not deploy if CI is not green.

- [ ] Step 2: Capture VPS preflight.

    ssh root@"$VPS_HOST" 'cd /opt/codex-quota-monitor && ./deploy/preflight.sh'

Stop if protected services, listener snapshots, or resource conditions differ.

- [ ] Step 3: Sync only project files, excluding .env, data/, .git/, and .worktrees/.

Use the documented rsync command. Never overwrite /opt/codex-quota-monitor/data/monitor.db.

- [ ] Step 4: Run isolated dry-run and acceptance checks.

    ssh root@"$VPS_HOST" 'cd /opt/codex-quota-monitor && ./deploy/dry-run.sh && ./deploy/postflight.sh && ./deploy/resource-check.sh'

Confirm four sources fetch, the real-world post is planned, unfamiliar candidates are possible_reset, and no project process/port remains after cleanup.

- [ ] Step 5: Run bounded history reprocessing twice.

    ssh root@"$VPS_HOST" 'cd /opt/codex-quota-monitor && ./deploy/reprocess-unmatched.sh --days 7 --limit 100'
    ssh root@"$VPS_HOST" 'cd /opt/codex-quota-monitor && ./deploy/reprocess-unmatched.sh --days 7 --limit 100'

Expected: first run may change/send eligible missed posts; second run reports zero new changes/sends. Inspect Feishu for one copy per post before resolving uncertain delivery.

- [ ] Step 6: Verify production health and protected services.

    ssh root@"$VPS_HOST" 'systemctl show codex-quota-monitor.timer -p ActiveState -p SubState -p LastTriggerUSec -p NextElapseUSecRealtime'
    ssh root@"$VPS_HOST" 'cd /opt/codex-quota-monitor && ./deploy/postflight.sh && ./deploy/resource-check.sh'

Verify timer active/waiting, MemoryMax=402653184, CPUQuotaPerSecUSec=300ms, protected services/ports unchanged, and the real missed post has matched=1, expected status, and one successful delivery attempt.

## Final Review Checklist

- [ ] Spec sections 1–4 are covered by Tasks 1–2.
- [ ] Evidence groups, hard exclusions, and possible_reset are covered by Tasks 1–2 and 4.
- [ ] SQLite metadata, rule versioning, bounded reprocessing, and idempotency are covered by Tasks 3, 5, and 7.
- [ ] Low-confidence Feishu copy and original links are covered by Task 4.
- [ ] Deployment isolation, 30-minute timer, and resource caps are covered by Tasks 6 and 8.
- [ ] Real missed post 2077607697487188198 is a regression fixture and VPS acceptance case.
- [ ] No step relies on an LLM or broad external API.
- [ ] No placeholders, destructive commands, or unbounded database scans are present.
