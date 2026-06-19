# SiriusXM Env Login Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add Option A SiriusXM login support that reads credentials from an environment file and refreshes HLS stream URLs when needed.

**Architecture:** Keep speaker-facing playback on the existing local HLS proxy. Add a focused SiriusXM session module that owns credential loading, login/session state, and stream URL resolution, then call it from the playlist handler when the stored URL is missing or rejected.

**Tech Stack:** Python standard library HTTP/cookies/SQLite, existing `unittest` tests, systemd environment file.

---

### Task 1: SiriusXM Config And Session Unit

**Files:**
- Create: `sixback_ubuntu/sixback_ubuntu/siriusxm.py`
- Test: `sixback_ubuntu/tests/test_siriusxm_auth.py`

- [x] Write failing tests for env file parsing, redacted status, and refresh URL extraction from SiriusXM-like JSON.
- [x] Run `python -m unittest sixback_ubuntu.tests.test_siriusxm_auth` and verify the tests fail because `sixback_ubuntu.siriusxm` is missing.
- [x] Implement credential loading, a `SiriusXmSession` class, safe status reporting, and stream URL extraction helpers.
- [x] Run the new unit tests and verify they pass.

### Task 2: SQLite Metadata For Refresh State

**Files:**
- Modify: `sixback_ubuntu/sixback_ubuntu/db.py`
- Test: `sixback_ubuntu/tests/test_siriusxm_auth.py`

- [x] Write failing tests proving channel refresh metadata can be stored without changing existing channel callers.
- [x] Add optional `stream_expires_at`, `last_refresh_at`, and `last_refresh_error` columns with migration-safe `ALTER TABLE` calls.
- [x] Add a narrow `update_siriusxm_stream_status` method.
- [x] Run the SiriusXM auth tests and existing HLS proxy tests.

### Task 3: Playlist Lazy Refresh

**Files:**
- Modify: `sixback_ubuntu/sixback_ubuntu/server.py`
- Test: `sixback_ubuntu/tests/test_siriusxm_auth.py`

- [x] Write failing tests for deciding when to refresh and for avoiding password/token leakage in error summaries.
- [x] Initialize the SiriusXM session from `/etc/sixback-ubuntu/siriusxm.env` by default, with an env override for tests.
- [x] In `/siriusxm/proxy/{station}/playlist.m3u8`, login/refresh when the stored URL is missing or playlist fetch returns HTTP 401/403.
- [x] Store the refreshed URL and continue through the existing HLS trimming/rewrite path.

### Task 4: API, Admin UI, And Docs

**Files:**
- Modify: `sixback_ubuntu/sixback_ubuntu/server.py`
- Modify: `sixback_ubuntu/README.md`
- Modify: `INSTALL_FROM_GITHUB.md`

- [x] Add `GET /api/siriusxm/session`, `POST /api/siriusxm/session/login`, and `POST /api/siriusxm/channels/{station_id}/refresh`.
- [x] Extend the admin UI with redacted SiriusXM session status and refresh buttons.
- [x] Document `/etc/sixback-ubuntu/siriusxm.env`, systemd `EnvironmentFile=`, file permissions, deployment, and troubleshooting.

### Task 5: Verification And Publish

**Files:**
- Run verification over the repo.

- [x] Run `python -m unittest sixback_ubuntu.tests.test_hls_proxy sixback_ubuntu.tests.test_siriusxm_auth`.
- [x] Run `python -m compileall sixback_ubuntu\sixback_ubuntu sixback_ubuntu\tools sixback_ubuntu\tests`.
- [x] Run `git diff --check`.
- [x] Remove generated `__pycache__` directories.
- [x] Review `git diff`, commit, and push to `main`.
