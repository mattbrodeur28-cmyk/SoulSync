"""Chunking, progress and cancellation for the wishlist library-cleanup pass.

``remove_tracks_already_in_library`` walks every wishlist entry against the
library. On a 7000-track wishlist that pass is long enough to look hung, so it
now runs in chunks, reports progress per chunk, and can be stopped part-way.

It is NOT batched. Switching it to the
``check_track_exists(candidate_tracks=...)`` path was tried and measured at ~26x
SLOWER (400 probes / 6000-track library: 7.4s per-query vs 194s batched),
because a wishlist's entries have ~unique artists so there is no candidate reuse
to amortize. These tests pin the chunking behaviour only; the performance note
lives in the function's docstring so the next person does not repeat it.
"""

from __future__ import annotations

from core.wishlist import processing


class _Profiles:
    def get_all_profiles(self):
        return [{"id": 1}]


class _WishlistService:
    def __init__(self, tracks):
        self._tracks = tracks
        self.removed = []

    def get_wishlist_tracks_for_download(self, profile_id=None):
        return list(self._tracks)

    def mark_track_download_result(self, spotify_track_id, success=True):
        self.removed.append(spotify_track_id)
        return True


class _Database:
    """Counts per-track lookups so chunk boundaries are observable."""

    def __init__(self, owned_titles=()):
        self.checks = 0
        self.owned = set(owned_titles)

    def check_track_exists(self, track_name, artist_name, confidence_threshold=0.7,
                           server_source=None, album=None):
        self.checks += 1
        if track_name in self.owned:
            return {"id": f"db-{track_name}"}, 0.95
        return None, 0.0


def _wishlist(n):
    return [
        {"name": f"Song {i}", "artists": [{"name": f"Artist {i}"}],
         "spotify_track_id": f"sp-{i}", "id": f"sp-{i}", "album": {"name": f"Album {i}"}}
        for i in range(n)
    ]


def test_progress_callback_reports_each_chunk():
    svc = _WishlistService(_wishlist(25))
    db = _Database(owned_titles={"Song 0", "Song 11"})
    seen = []

    removed = processing.remove_tracks_already_in_library(
        svc, _Profiles(), db, "test", chunk_size=10,
        progress_callback=lambda done, total, rm: seen.append((done, total, rm)))

    assert [s[0] for s in seen] == [10, 20, 25]     # monotonic, ends at total
    assert all(s[1] == 25 for s in seen)
    assert seen[-1][2] == 2
    assert removed == 2


def test_should_stop_halts_between_chunks_and_keeps_partial_work():
    """A long pass must be interruptible without losing what it already removed —
    removals commit per track, so re-running resumes."""
    svc = _WishlistService(_wishlist(100))
    db = _Database(owned_titles={f"Song {i}" for i in range(100)})
    calls = {"n": 0}

    def _stop():
        calls["n"] += 1
        return calls["n"] > 2          # allow two chunks, then stop

    removed = processing.remove_tracks_already_in_library(
        svc, _Profiles(), db, "test", chunk_size=10, should_stop=_stop)

    assert removed == 20               # exactly the two chunks that ran
    assert len(svc.removed) == 20
    assert db.checks == 20             # no work done after the stop


def test_progress_callback_errors_do_not_abort_cleanup():
    """A broken UI callback must not cost the user their cleanup run."""
    svc = _WishlistService(_wishlist(20))
    db = _Database(owned_titles={"Song 5"})

    def _boom(done, total, removed):
        raise RuntimeError("UI went away")

    removed = processing.remove_tracks_already_in_library(
        svc, _Profiles(), db, "test", chunk_size=5, progress_callback=_boom)

    assert removed == 1


def test_every_track_is_still_checked_regardless_of_chunk_size():
    """Chunking is a reporting boundary, not a filter — a ragged final chunk
    must not drop entries."""
    for chunk_size in (1, 7, 25, 1000):
        svc = _WishlistService(_wishlist(25))
        db = _Database()
        processing.remove_tracks_already_in_library(
            svc, _Profiles(), db, "test", chunk_size=chunk_size)
        assert db.checks == 25, f"chunk_size={chunk_size} checked {db.checks}/25"


def test_zero_chunk_size_does_not_hang():
    """chunk_size=0 would be an infinite range step; it is clamped to 1."""
    svc = _WishlistService(_wishlist(5))
    db = _Database()
    processing.remove_tracks_already_in_library(
        svc, _Profiles(), db, "test", chunk_size=0)
    assert db.checks == 5


def test_empty_wishlist_reports_nothing_and_returns_zero():
    svc = _WishlistService([])
    db = _Database()
    seen = []
    removed = processing.remove_tracks_already_in_library(
        svc, _Profiles(), db, "test",
        progress_callback=lambda *a: seen.append(a))
    assert removed == 0
    assert seen == []


def test_artist_display_name_handles_both_payload_shapes():
    assert processing._artist_display_name("Plain") == "Plain"
    assert processing._artist_display_name({"name": "Dict"}) == "Dict"
    assert processing._artist_display_name(42) == "42"
