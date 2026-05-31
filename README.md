# Social Jukebox 🎵

Democratize the music in your space. Let guests vote on what plays next.

## How It Works

1. **Owner** signs up, links their Spotify Premium account, and creates a Music Space
2. **Guests** scan a QR code to join — no app download or account needed
3. **Everyone votes** on what plays next. The most popular song wins

## Tech Stack

- **Backend:** FastAPI (Python 3.11+)
- **Database:** Supabase (PostgreSQL)
- **Music:** Spotify Web API via `spotipy`
- **Frontend:** Vanilla HTML/JS + Tailwind CSS (CDN)
- **Deployment:** Render (free tier)

## Local Development

### Prerequisites

- Python 3.11+
- A [Spotify Developer App](https://developer.spotify.com/dashboard)
- A [Supabase](https://supabase.com) project (free tier)

### Setup

```bash
# Clone the repo
git clone https://github.com/your-username/social-jukebox.git
cd social-jukebox

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env file and fill in your values
cp .env.example .env
```

### Configure `.env`

```env
DATABASE_URL=postgresql+asyncpg://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8000/api/spotify/callback
SECRET_KEY=generate-with-openssl-rand-hex-32
APP_URL=http://localhost:8000
```

### Spotify Developer App Settings

In your [Spotify Developer Dashboard](https://developer.spotify.com/dashboard):
1. Create a new app
2. Set Redirect URI to: `http://localhost:8000/api/spotify/callback`
3. Note the Client ID and Client Secret

### Run

```bash
uvicorn app.main:app --reload
```

Visit: http://localhost:8000

## Deployment (Render)

1. Push to GitHub
2. Connect repo to [Render](https://render.com)
3. Deploy using `render.yaml` (Blueprint)
4. Set environment variables in Render dashboard
5. Update `SPOTIFY_REDIRECT_URI` and `APP_URL` to match your Render URL

## Project Structure

```
social-jukebox/
├── app/
│   ├── main.py              # FastAPI app, lifespan, middleware
│   ├── config.py            # Environment config (pydantic-settings)
│   ├── database.py          # Async SQLAlchemy engine
│   ├── models.py            # ORM models (User, Space, QueueItem, Vote)
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── auth.py              # JWT + password hashing
│   ├── worker.py            # Background playback polling
│   ├── routers/
│   │   ├── auth_routes.py   # Signup, login, Google OAuth
│   │   ├── spotify.py       # Spotify OAuth linking
│   │   ├── spaces.py        # Space CRUD + QR codes
│   │   ├── queue.py         # Guest search, add, vote
│   │   └── pages.py         # HTML page serving
│   └── templates/
│       ├── landing.html     # Public homepage
│       ├── dashboard.html   # Owner dashboard
│       └── space.html       # Guest voting interface
├── requirements.txt
├── .env.example
├── render.yaml
└── README.md
```

## API Endpoints

### Auth
- `POST /api/auth/signup` — Create account
- `POST /api/auth/login` — Login
- `POST /api/auth/google` — Google OAuth
- `GET /api/auth/me` — Current user profile

### Spotify
- `GET /api/spotify/link` — Get Spotify auth URL
- `GET /api/spotify/callback` — Handle OAuth callback
- `GET /api/spotify/status` — Check if Spotify is linked

### Spaces (requires auth)
- `POST /api/spaces` — Create a space
- `GET /api/spaces` — List your spaces
- `PATCH /api/spaces/{code}/activate` — Start polling
- `PATCH /api/spaces/{code}/deactivate` — Stop polling
- `DELETE /api/spaces/{code}` — Delete space
- `GET /api/spaces/{code}/qr` — Download QR code PNG

### Queue (no auth — for guests)
- `GET /api/spaces/{code}/search?q=` — Search Spotify
- `POST /api/spaces/{code}/add` — Add track to queue
- `POST /api/spaces/{code}/vote` — Upvote a track
- `GET /api/spaces/{code}/queue` — Get now playing + queue

## License

MIT
