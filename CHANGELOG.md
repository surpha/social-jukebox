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

### Notes
- Baseline captured at branch `feature/persistent-spaces-dj`. Prior history (Spotify-only auth,
  downvotes, clear-queue-on-deactivate, persistent spaces/DJ) predates this changelog.
