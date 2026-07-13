# Banked Reset Monitoring Design

## Goal

Detect trusted announcements that grant a banked Codex quota reset, preserve the complete public post text, notify Feishu with wording distinct from an automatic quota reset, and safely backfill a previously stored missed post exactly once.

## Product semantics

- Add `banked_available` as a distinct reset status.
- Render it in Chinese as `可保存重置次数已发放`.
- Do not describe a banked reset as quota that has already been automatically replenished.
- Continue accepting announcements only from configured trusted X handles and only from their original post text.
- Continue rejecting questions, requests, referrals, periodic reset descriptions, and reset-mechanism explanations.

## Data flow

1. The RSSHub adapter validates the configured route, status URL, and author as before.
2. When the feed title is truncated but the HTML description contains longer main text, the adapter stores the description main text. Quoted content remains separate and cannot supply classification evidence.
3. The classifier requires Codex, quota or usage context, `banked reset`, and explicit grant language such as `have added` before returning `banked_available`.
4. The Feishu card uses a dedicated label and includes the full trusted original text and X link.
5. An operator-only `reprocess-post <post_id>` command fetches the current trusted feeds, locates the exact post, reclassifies it, and atomically promotes an existing unmatched database row to `pending`. The normal delivery claim state machine then sends it.

## Idempotency and failure handling

- Reprocessing may update only an existing row whose `matched` value is false.
- The promotion and content replacement happen in one SQLite transaction.
- A second reprocess attempt returns unchanged and cannot claim a sent row again.
- Feishu timeout/unknown-outcome behavior remains at-most-once: the row becomes `uncertain` and is not resent automatically.
- If the post is absent from current feeds or still does not classify, the command fails without changing stored state.

## Resource and deployment constraints

- No new daemon, browser, API subscription, or background process.
- The hourly systemd timer and existing CPU/memory caps remain unchanged.
- Deployment must verify SSH, sing-box, subscription services, monitor status, and RSSHub/monitor ports before and after the change.

## Verification

- Regression tests cover the real banked-reset wording, truncated-title/full-description parsing, dedicated Feishu copy, false-positive exclusions, atomic promotion, and repeated reprocessing.
- The full local test suite, lock-file check, shell syntax checks, VPS dry run, one targeted production backfill, database delivery state, timer state, resource caps, protected services, and port cleanup must all be verified.
