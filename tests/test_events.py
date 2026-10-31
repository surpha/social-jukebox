import json

from app.engine import engine_manager
from app.routers.queue import _event_stream
from tests.test_engine import make_engine


async def test_event_stream_sends_snapshot_then_updates_then_ends_on_stop():
    engine, sp, store, _ = await make_engine()
    sp.play("cur", progress=1000)
    await engine.tick()
    engine_manager._engines[engine.space_id] = engine
    stream = _event_stream(engine.space_id)
    try:
        assert await stream.__anext__() == "retry: 3000\n\n"
        first = await stream.__anext__()
        assert first.startswith("data: ")
        assert json.loads(first[6:])["now_playing"]["track_id"] == "cur"

        store.add("new")
        engine.pending = await store.load_pending()
        engine.publish()
        update = await stream.__anext__()
        assert [q["track_id"] for q in json.loads(update[6:])["queue"]] == ["new"]
        assert engine.subscriber_count == 1
    finally:
        engine_manager._engines.pop(engine.space_id, None)
        await stream.aclose()
    assert engine.subscriber_count == 0
