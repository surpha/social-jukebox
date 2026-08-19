# AGENTS.md — Working agreement for AI agents & contributors

This is the **source of truth** for how to work in this repo. Other agent files
([CLAUDE.md](CLAUDE.md), [.github/copilot-instructions.md](.github/copilot-instructions.md)) point back here.

## Project snapshot
Social Jukebox — a FastAPI app where a host links Spotify and guests vote (via QR code, no
account) on what plays next. The top-voted song is auto-queued into the host's live Spotify
playback by a background worker. Full technical detail lives in [ARCHITECTURE.md](ARCHITECTURE.md).

## Environment & commands

```bash
# One-time setup
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values

# Run locally (http://localhost:8000)
uvicorn app.main:app --reload
```

- Python **3.11.9** (see `.python-version`).
- No test suite yet. If you add tests, use `pytest` and document the command here.
- Deployment is Render via `render.yaml`; start command is
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Codebase conventions
- **Async everywhere**: routes, DB access (`AsyncSession`), and the worker are async. Never
  call blocking DB APIs. Use `await db.execute(select(...))`.
- **DB sessions** come from the `get_db` dependency (`Depends(get_db)`); the worker uses
  `async_session()` directly.
- **Schema changes**: there is no Alembic. Add new columns as idempotent
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements inside `init_db()` in
  [app/database.py](app/database.py), in addition to updating the model.
- **Spotify access**: always go through `get_spotify_client_for_user(user, db)` so refreshed
  tokens get persisted. Tokens are Fernet-encrypted; never store them in plaintext.
- **Auth**: Spotify-only. Protected routes depend on `get_current_user`. Guest queue routes are
  intentionally unauthenticated and identify voters via the `voter_id` cookie.
- **Config**: read settings through `get_settings()`; add new env vars to `config.py`,
  `.env.example`, and `render.yaml` together.
- **Frontend**: server-rendered Jinja2 + vanilla JS + Tailwind CDN. Keep it dependency-free
  (no build step). Templates poll the API every ~3s.
- Keep edits minimal and focused; match the existing style. No new frameworks or build tooling
  without agreement.

## Security musts
- Never commit `.env` or real credentials.
- Keep Spotify tokens encrypted at rest.
- Rotating `SECRET_KEY` invalidates all stored Spotify tokens (it derives the Fernet key) — flag
  this if you touch auth/crypto.

## Documentation maintenance protocol (READ THIS)
These docs must stay accurate. **On every change, update docs in the same commit as the code.**

When you change… | Update…
---|---
Models / DB schema / migrations | [ARCHITECTURE.md](ARCHITECTURE.md) §5, §8 and the migration block in `init_db()`
Routes / endpoints | [ARCHITECTURE.md](ARCHITECTURE.md) §6 and the endpoint list in [README.md](README.md)
Env vars / config | `config.py`, `.env.example`, `render.yaml`, [ARCHITECTURE.md](ARCHITECTURE.md) §9
Auth / Spotify token flow | [ARCHITECTURE.md](ARCHITECTURE.md) §6.1, §7
Worker behavior | [ARCHITECTURE.md](ARCHITECTURE.md) §6.5
Build / run / deploy | this file + [HARNESS.md](HARNESS.md)
Any notable change | add an entry to [CHANGELOG.md](CHANGELOG.md)

Checklist before finishing a task:
1. [ ] Code change complete and consistent with existing patterns.
2. [ ] Relevant docs above updated.
3. [ ] `CHANGELOG.md` has a new `Unreleased` entry.
4. [ ] New env vars added in all three places (`config.py`, `.env.example`, `render.yaml`).
5. [ ] No secrets committed.

## Known gaps (see ARCHITECTURE.md §10)
README documents removed endpoints; `SPOTIFY_LOGIN_REDIRECT_URI` missing from
`.env.example`/`render.yaml`; no tests; Spotify recommendations API partially deprecated.
