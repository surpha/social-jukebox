import asyncio
import json
import uuid

import pytest

from app.engine import IDLE_POLL, LOCK_IN_MS, MAX_ON_DECK_MISSES, MIN_POLL, SpaceEngine


def track(tid: str, duration=200_000, artists=None) -> dict:
    return {
        "id": tid,
        "type": "track",
        "name": f"Song {tid}",
        "duration_ms": duration,
        "artists": artists or [{"id": f"artist-{tid}", "name": f"Artist {tid}"}],
        "album": {"images": [{"url": f"https://img/{tid}"}]},
    }


class FakeSpotify:
    def __init__(self):
        self.current: dict | None = None
        self.upcoming: list[dict] = []
        self.added: list[str] = []
        self.fail_add = False

    def play(self, tid: str, progress: int, playing=True, duration=200_000):
        self.current = {"item": track(tid, duration), "progress_ms": progress, "is_playing": playing}

    async def playback(self):
        return self.current

    async def queue(self):
        return self.upcoming

    async def add_to_queue(self, track_id):
        if self.fail_add:
            raise RuntimeError("no device")
        self.added.append(track_id)
        self.upcoming.insert(0, track(track_id))

    async def artist_top_tracks(self, artist_id):
        return [track("rec1"), track("rec2")]


class FakeStore:
    def __init__(self, items=None):
        self.items = {i["id"]: dict(i) for i in (items or [])}

    def add(self, tid: str, votes=1):
        item = {"id": f"id-{tid}", "track_id": tid, "name": f"Song {tid}", "artist": "A", "album_art": "",
                "duration_ms": 1, "vote_count": votes, "status": "pending"}
        self.items[item["id"]] = item
        return item

    def _public(self, i):
        return {k: v for k, v in i.items() if k != "status"}

    async def load_pending(self):
        pending = [i for i in self.items.values() if i["status"] == "pending"]
        return [self._public(i) for i in sorted(pending, key=lambda i: -i["vote_count"])]

    async def load_on_deck(self):
        queued = [i for i in self.items.values() if i["status"] == "queued"]
        return self._public(queued[0]) if queued else None

    async def mark_queued(self, item_id):
        item = self.items.get(item_id)
        if not item or item["status"] != "pending":
            return False
        item["status"] = "queued"
        return True

    async def revert_queued(self, item_id):
        self.items[item_id]["status"] = "pending"

    async def delete_item(self, item_id):
        self.items.pop(item_id, None)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def make_engine(store=None, spotify=None):
    store = store or FakeStore()
    spotify = spotify or FakeSpotify()
    clock = Clock()
    engine = SpaceEngine(uuid.uuid4(), "Test", spotify, store, clock=clock)
    engine.pending = await store.load_pending()
    engine.on_deck = await store.load_on_deck()
    return engine, spotify, store, clock


async def test_does_not_lock_in_early_and_wakes_at_lock_in_time():
    engine, sp, store, _ = await make_engine()
    store.add("aaa")
    engine.pending = await store.load_pending()
    sp.play("cur", progress=100_000)  # 100s remaining

    delay = await engine.tick()

    assert sp.added == []
    assert engine.on_deck is None
    assert delay == IDLE_POLL  # capped
    sp.play("cur", progress=200_000 - LOCK_IN_MS - 3000)
    delay = await engine.tick()
    assert delay == pytest.approx(3.0)


async def test_locks_in_top_voted_near_end_of_track():
    engine, sp, store, _ = await make_engine()
    store.add("low", votes=1)
    store.add("top", votes=5)
    engine.pending = await store.load_pending()
    sp.play("cur", progress=200_000 - 15_000)

    await engine.tick()

    assert sp.added == ["top"]
    assert engine.on_deck["track_id"] == "top"
    assert [p["track_id"] for p in engine.pending] == ["low"]
    assert store.items["id-top"]["status"] == "queued"


async def test_on_deck_removed_once_it_starts_playing():
    engine, sp, store, _ = await make_engine()
    store.add("top")
    engine.pending = await store.load_pending()
    sp.play("cur", progress=190_000)
    await engine.tick()

    sp.play("top", progress=500)
    await engine.tick()

    assert engine.on_deck is None
    assert "id-top" not in store.items


async def test_on_deck_kept_while_still_in_spotify_queue():
    engine, sp, store, _ = await make_engine()
    store.add("top")
    engine.pending = await store.load_pending()
    sp.play("cur", progress=190_000)
    await engine.tick()

    sp.play("other", progress=1000)  # host played something else; ours is still queued
    await engine.tick()

    assert engine.on_deck["track_id"] == "top"


async def test_on_deck_dropped_after_repeated_misses():
    engine, sp, store, _ = await make_engine()
    store.add("top")
    engine.pending = await store.load_pending()
    sp.play("cur", progress=190_000)
    await engine.tick()

    sp.upcoming = []  # removed from Spotify's queue
    sp.play("other", progress=1000)
    for _ in range(MAX_ON_DECK_MISSES - 1):
        await engine.tick()
        assert engine.on_deck is not None
    await engine.tick()

    assert engine.on_deck is None
    assert "id-top" not in store.items


async def test_failed_spotify_add_reverts_to_pending():
    engine, sp, store, _ = await make_engine()
    store.add("top")
    engine.pending = await store.load_pending()
    sp.fail_add = True
    sp.play("cur", progress=190_000)

    with pytest.raises(RuntimeError):
        await engine.tick()

    assert engine.on_deck is None
    assert store.items["id-top"]["status"] == "pending"


async def test_requesting_the_current_song_does_not_drop_it_immediately():
    engine, sp, store, _ = await make_engine()
    sp.play("cur", progress=150_000)
    await engine.tick()
    store.add("cur")
    engine.pending = await store.load_pending()

    sp.play("cur", progress=185_000)
    await engine.tick()  # locks in a replay of the same track
    assert engine.on_deck["track_id"] == "cur"
    sp.play("cur", progress=186_000)
    await engine.tick()
    assert engine.on_deck is not None

    sp.play("cur", progress=1_000)  # replay started
    await engine.tick()
    assert engine.on_deck is None


async def test_idle_when_nothing_playing():
    engine, sp, *_ = await make_engine()
    sp.current = None
    assert await engine.tick() == IDLE_POLL
    assert engine.now_playing is None


async def test_no_busy_polling_near_end_with_empty_queue():
    engine, sp, *_ = await make_engine()
    sp.play("cur", progress=200_000 - 10_000)
    delay = await engine.tick()
    assert delay == IDLE_POLL  # nothing to lock in, so just wait (capped) for the track change
    sp.play("cur", progress=200_000 - 1_000)
    assert await engine.tick() == pytest.approx(1.5)
    sp.play("cur", progress=200_000 - 100)
    assert await engine.tick() == MIN_POLL


async def test_publish_only_on_visible_change_or_seek():
    engine, sp, store, clock = await make_engine()
    sp.play("cur", progress=10_000)
    await engine.tick()
    sub = engine.subscribe()
    sub.event.clear()

    clock.t += 3
    sp.play("cur", progress=13_000)  # natural progress: no push
    await engine.tick()
    assert not sub.event.is_set()

    clock.t += 1
    sp.play("cur", progress=90_000)  # seek: push
    await engine.tick()
    assert sub.event.is_set()
    assert json.loads(sub.latest)["now_playing"]["progress_ms"] == 90_000

    sub.event.clear()
    store.add("new")
    engine.pending = await store.load_pending()
    engine.publish()
    assert sub.event.is_set()
    assert [q["track_id"] for q in json.loads(sub.latest)["queue"]] == ["new"]


async def test_payload_interpolates_progress():
    engine, sp, _, clock = await make_engine()
    sp.play("cur", progress=10_000)
    await engine.tick()
    clock.t += 2.5
    assert json.loads(engine.payload())["now_playing"]["progress_ms"] == 12_500


async def test_spotify_queue_excludes_tracks_already_shown():
    engine, sp, store, _ = await make_engine()
    sp.upcoming = [track("cur"), track("dup"), track("fresh")]
    store.add("dup")
    engine.pending = await store.load_pending()
    sp.play("cur", progress=1000)
    await engine.tick()
    assert [t["track_id"] for t in engine.state()["spotify_queue"]] == ["fresh"]


async def test_queue_loop_coalesces_and_publishes():
    engine, sp, store, _ = await make_engine()
    sub = engine.subscribe()
    sub.event.clear()
    task = asyncio.create_task(engine._queue_loop())
    try:
        store.add("x")
        engine.queue_changed()
        engine.queue_changed()
        await asyncio.sleep(0.3)
        assert [q["track_id"] for q in engine.pending] == ["x"]
        assert sub.event.is_set()
    finally:
        task.cancel()


async def test_recommendations_exclude_current_track():
    engine, sp, *_ = await make_engine()
    sp.play("rec1", progress=1000)
    await engine.tick()
    await engine._recs_task
    assert [r["track_id"] for r in engine.recommendations] == ["rec2"]


async def test_stop_releases_subscribers():
    engine, *_ = await make_engine()
    await engine.start()
    sub = engine.subscribe()
    sub.event.clear()
    await engine.stop()
    assert sub.event.is_set() and sub.latest is None
