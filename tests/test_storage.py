from flip_finder.models import Listing
from flip_finder.storage import SeenStore


def test_seen_analysis_and_retry_state(tmp_path) -> None:
    item = Listing("test", "https://example.com/1", "one", "Test item", 10)
    with SeenStore(tmp_path / "test.sqlite3") as store:
        assert not store.is_seen("one")
        store.schedule_retry("one", "temporary AI failure")
        assert store.pending_count() == 1
        store.save_analysis("one", {"status": "manual_check"})
        store.mark_seen(item)
        assert store.is_seen("one")
        assert store.pending_count() == 0
