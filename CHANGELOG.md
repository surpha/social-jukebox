# Changelog

All notable changes to this project are documented here. Add an entry under **Unreleased**
for every notable change (see the documentation maintenance protocol in [AGENTS.md](AGENTS.md)).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- Agent & architecture documentation: `ARCHITECTURE.md`, `AGENTS.md`, `CLAUDE.md`,
  `HARNESS.md`, `.github/copilot-instructions.md`, and this `CHANGELOG.md`.
- `GET /health` liveness endpoint (no DB hit) for uptime pings to keep the Render free tier awake.

### Changed
- Owner dashboard "master view" now shows the downvote button alongside upvote (parity with the
  guest page), with mutual up/down clearing. Uses the existing `has_downvoted` queue data.

### Fixed
- Worker no longer silently stops queueing after a track repeats. Finished tracks were marked
  `status='played'`, but the unique `(space_id, track_id, status)` constraint only allows one row
  per track+status, so replaying/re-voting a track made the `UPDATE ... SET status='played'` throw
  `UniqueViolationError`. That aborted the whole poll **before** the add-to-queue step, so nothing
  new was ever queued. Finished items (and their votes) are now **deleted** instead of marked
  played, which removes the collision and prevents unbounded row growth.
- On-deck locking is now reliable. The worker keeps exactly one top-voted track locked in Spotify
  as the next song and only frees that slot when the track actually starts playing or is confirmed
  gone. Previously it released the slot on a single eventually-consistent `queue()` snapshot, which
  — when Spotify autoplay made the snapshot non-empty before a just-added track had propagated —
  wrongly marked the locked song skipped and queued a second one (double-queue/desync). The slot
  now self-heals: a leftover/stale `queued` row that Spotify never reports is released after a few
  reliable snapshots so it can never permanently block queueing, and leftover on-deck rows are
  cleared when a worker starts. `GET /queue` is read-only for the on-deck item (the worker owns its
  lifecycle) and no longer deletes queued rows on a stale snapshot.
- "Up next" no longer flaps/switches constantly. `GET /queue` previously deleted the queued item
  whenever Spotify's eventually-consistent `queue()` snapshot momentarily omitted it, causing the
  worker to re-queue a different track each poll. It is now non-destructive: it only prunes a
  queued item when a reliable (non-empty) Spotify snapshot genuinely omits it, uses deterministic
  ordering, and excludes the now-playing track.
- Worker no longer queues the same song multiple times. It previously gated re-queuing on a
  loop-local flag that reset on every detected "track change"; Spotify playback noise (ads, device
  switches, API hiccups) flipped the current track back and forth, re-pushing the top song 5–6
  times. Queueing now gates on DB state (one on-deck `queued` track at a time) and skips adds for
  tracks already in Spotify's queue.

### Notes
- Baseline captured at branch `feature/persistent-spaces-dj`. Prior history (Spotify-only auth,
  downvotes, clear-queue-on-deactivate, persistent spaces/DJ) predates this changelog.
