import os
import tempfile

# Never let tests touch the real database from .env: use a throwaway local Postgres when available.
os.environ["APP_URL"] = "http://test"
try:
    import pgserver

    _pg = pgserver.get_server(tempfile.mkdtemp(prefix="sj-test-pg-"), cleanup_mode="delete")
    _socket_dir = _pg.get_uri().split("host=")[1]
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://postgres@/postgres?host={_socket_dir}"
    HAVE_TEST_DB = True
except Exception:
    os.environ["DATABASE_URL"] = "postgresql+asyncpg://invalid@127.0.0.1:1/none"
    HAVE_TEST_DB = False
