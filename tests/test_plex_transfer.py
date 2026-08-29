"""Tests for Plex-to-Plex playlist and rating transfer.

This code WRITES to a live Plex server, so the safety rules matter more than the
happy path: dry-run must write nothing, a rating must never be cleared, and a
re-run over unchanged state must issue no writes. Those are pinned first.

No live server is involved — both sides are fakes shaped like the PlexClient
surface the transfer actually uses (``get_all_tracks``, ``search_tracks``,
``get_all_playlists``, ``create_playlist``, ``set_track_rating``).
"""

from __future__ import annotations

from typing import List, Optional

from core.media_server.types import PlaylistInfo, TrackInfo
from core.plex_transfer import PlexToPlexTransfer, TransferReport


class FakeTrack:
    """Stands in for a raw plexapi Track on the destination."""

    def __init__(self, title, artist, guid=None, rating=None, duration=200000):
        self.title = title
        self._artist = artist
        self.guid = guid
        self.userRating = rating
        self.duration = duration
        self.ratingKey = f"rk-{title}"
        self.rate_calls: List[Optional[float]] = []

    # plexapi exposes artist() as a callable
    def artist(self):
        return type("A", (), {"title": self._artist})()

    def rate(self, value=None):
        self.rate_calls.append(value)
        self.userRating = value


def _ti(title, artist, guid=None, rating=None, duration=200000):
    return TrackInfo(id=f"id-{title}", title=title, artist=artist, album="Alb",
                     duration=duration, rating=rating, guid=guid)


class FakeServer:
    """Minimal PlexClient stand-in."""

    def __init__(self, tracks=None, playlists=None):
        self._tracks = tracks or []
        self._playlists = playlists or []
        self.created = []
        self.rating_writes = []

    def get_all_tracks(self):
        return list(self._tracks)

    def get_all_playlists(self):
        return list(self._playlists)

    def search_tracks(self, title, artist, limit=15):
        out = []
        for t in self._tracks:
            if title.lower() in (t.title or '').lower():
                ti = _ti(t.title, t._artist, guid=t.guid, rating=t.userRating)
                ti._original_plex_track = t
                out.append(ti)
        return out[:limit]

    def create_playlist(self, name, tracks):
        self.created.append((name, list(tracks)))
        return True

    def set_track_rating(self, track, rating):
        if rating is None:
            return False
        self.rating_writes.append((track.title, rating))
        track.rate(rating)
        return True


# ---------------------------------------------------------------------------
# Safety rules
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing():
    src = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=8.0)])
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=None)])
    t = PlexToPlexTransfer(src, dst)

    report = t.transfer_ratings()            # dry_run defaults True

    assert report.dry_run is True
    assert report.written == 1               # what WOULD be written
    assert dst.rating_writes == []           # ...but nothing was
    assert dst._tracks[0].rate_calls == []


def test_dry_run_is_the_default_for_playlists():
    pl = PlaylistInfo(id="1", title="Mix", description=None, duration=0,
                      leaf_count=1, tracks=[_ti("Song", "Artist", guid="g1")])
    src = FakeServer(playlists=[pl])
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1")])

    report = PlexToPlexTransfer(src, dst).transfer_playlists()

    assert report.dry_run is True
    assert dst.created == []


def test_unrated_source_track_never_clears_a_destination_rating():
    """The destination keeps its rating when the source has none."""
    src = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=None)])
    dst_track = FakeTrack("Song", "Artist", guid="g1", rating=6.0)
    dst = FakeServer(tracks=[dst_track])

    report = PlexToPlexTransfer(src, dst).transfer_ratings(dry_run=False)

    assert report.considered == 0            # not even a candidate
    assert dst_track.userRating == 6.0
    assert dst_track.rate_calls == []


def test_rerun_over_equal_ratings_issues_no_writes():
    """Idempotence: the second pass must be a no-op."""
    src = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=10.0)])
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=10.0)])

    report = PlexToPlexTransfer(src, dst).transfer_ratings(dry_run=False)

    assert report.skipped_same == 1
    assert report.written == 0
    assert dst.rating_writes == []


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_guid_match_wins_over_a_same_titled_other_recording():
    """guid is authoritative — it must not be overridden by title similarity."""
    right = FakeTrack("Song", "Artist", guid="g1")
    decoy = FakeTrack("Song", "Someone Else", guid="g2")
    src = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=7.0)])
    dst = FakeServer(tracks=[decoy, right])

    t = PlexToPlexTransfer(src, dst)
    target, how = t.resolve(_ti("Song", "Artist", guid="g1"))

    assert how == 'guid'
    assert target is right


def test_falls_back_to_fuzzy_when_guid_is_absent():
    src = FakeServer()
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist")])
    t = PlexToPlexTransfer(src, dst)

    target, how = t.resolve(_ti("Song", "Artist"))

    assert target is not None
    assert how.startswith('fuzzy:')


def test_below_threshold_stays_unmatched():
    """A high threshold must not be talked into a bad match."""
    src = FakeServer()
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist")])
    t = PlexToPlexTransfer(src, dst, match_threshold=1.01)   # unreachable

    target, how = t.resolve(_ti("Song", "Artist"))

    assert target is None
    assert how == 'unmatched'


def test_guid_index_is_built_once_not_per_track():
    """A sweep per track is what makes this class of pass take hours."""
    calls = {"n": 0}
    dst = FakeServer(tracks=[FakeTrack(f"S{i}", "Artist", guid=f"g{i}") for i in range(5)])
    real = dst.get_all_tracks

    def counting():
        calls["n"] += 1
        return real()

    dst.get_all_tracks = counting
    src = FakeServer(tracks=[FakeTrack(f"S{i}", "Artist", guid=f"g{i}", rating=5.0)
                             for i in range(5)])

    PlexToPlexTransfer(src, dst).transfer_ratings(dry_run=False)

    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_unmatched_tracks_are_reported_not_silently_dropped():
    pl = PlaylistInfo(id="1", title="Mix", description=None, duration=0, leaf_count=2,
                      tracks=[_ti("Present", "Artist", guid="g1"),
                              _ti("Absent", "Nobody")])
    src = FakeServer(playlists=[pl])
    dst = FakeServer(tracks=[FakeTrack("Present", "Artist", guid="g1")])

    report = PlexToPlexTransfer(src, dst).transfer_playlists(dry_run=False)

    assert report.matched == 1
    assert report.unmatched_count == 1
    assert "Absent" in report.unmatched[0]
    # The playlist is still created, with only the resolvable track.
    name, tracks = dst.created[0]
    assert name == "Mix" and len(tracks) == 1
    assert report.playlists[0]['missing'] == ["Absent — Nobody"]


def test_playlist_with_nothing_resolvable_is_skipped_not_created_empty():
    pl = PlaylistInfo(id="1", title="Ghost", description=None, duration=0,
                      leaf_count=1, tracks=[_ti("Absent", "Nobody")])
    src = FakeServer(playlists=[pl])
    dst = FakeServer(tracks=[])

    report = PlexToPlexTransfer(src, dst).transfer_playlists(dry_run=False)

    assert dst.created == []
    assert report.playlists[0]['written'] is False


def test_names_filter_limits_which_playlists_transfer():
    pls = [
        PlaylistInfo(id="1", title="Keep", description=None, duration=0, leaf_count=1,
                     tracks=[_ti("Song", "Artist", guid="g1")]),
        PlaylistInfo(id="2", title="Ignore", description=None, duration=0, leaf_count=1,
                     tracks=[_ti("Song", "Artist", guid="g1")]),
    ]
    src = FakeServer(playlists=pls)
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1")])

    PlexToPlexTransfer(src, dst).transfer_playlists(dry_run=False, names=["Keep"])

    assert [n for n, _ in dst.created] == ["Keep"]


def test_report_summary_flags_dry_run_clearly():
    r = TransferReport(operation='ratings', dry_run=True, considered=3, matched=2, written=2)
    assert "DRY RUN" in r.summary()
    assert "nothing written" in r.summary()
    assert TransferReport(operation='ratings', dry_run=False).summary().count("applied") == 1


def test_report_serializes_for_the_api():
    r = TransferReport(operation='playlists', dry_run=True, unmatched=["a", "b"])
    d = r.to_dict()
    assert d['unmatched_count'] == 2
    assert d['operation'] == 'playlists'
    assert 'summary' in d


# ---------------------------------------------------------------------------
# PlexClient: second-server connection + rating write
# ---------------------------------------------------------------------------


def test_plex_client_uses_injected_config_for_the_second_server(monkeypatch):
    """PlexClient(config=...) must target the injected server, so a transfer can
    hold two live connections without touching active_media_server."""
    import core.plex_client as pc

    seen = {}

    def _fake_server(base_url, token, timeout=None):
        seen['base_url'] = base_url
        seen['token'] = token
        raise RuntimeError("stop before network")   # _setup_client swallows this

    monkeypatch.setattr(pc, "PlexServer", _fake_server)

    client = pc.PlexClient(config={'base_url': 'http://second:32400', 'token': 'tok-B'})
    client._setup_client()

    assert seen == {'base_url': 'http://second:32400', 'token': 'tok-B'}


def test_plex_client_without_config_still_reads_global_settings(monkeypatch):
    """The default must stay unchanged — every existing call site passes nothing."""
    import core.plex_client as pc

    seen = {}

    def _fake_server(base_url, token, timeout=None):
        seen['base_url'] = base_url
        raise RuntimeError("stop before network")

    monkeypatch.setattr(pc, "PlexServer", _fake_server)
    monkeypatch.setattr(pc.config_manager, "get_plex_config",
                        lambda: {'base_url': 'http://primary:32400', 'token': 'tok-A'})

    pc.PlexClient()._setup_client()

    assert seen['base_url'] == 'http://primary:32400'


def test_set_track_rating_refuses_none_so_it_cannot_clear():
    """plexapi's rate(None) CLEARS the rating. Transfer must never do that."""
    import core.plex_client as pc

    track = FakeTrack("Song", "Artist", rating=9.0)
    assert pc.PlexClient().set_track_rating(track, None) is False
    assert track.rate_calls == []
    assert track.userRating == 9.0


def test_set_track_rating_writes_a_real_value():
    import core.plex_client as pc

    track = FakeTrack("Song", "Artist", rating=None)
    assert pc.PlexClient().set_track_rating(track, 10.0) is True
    assert track.rate_calls == [10.0]


def test_set_track_rating_reports_failure_instead_of_raising():
    import core.plex_client as pc

    class Boom:
        title = "Song"

        def rate(self, value=None):
            raise RuntimeError("plex said no")

    assert pc.PlexClient().set_track_rating(Boom(), 5.0) is False


# ---------------------------------------------------------------------------
# Regressions from the first live run: the transfer moved nothing
# ---------------------------------------------------------------------------


def test_get_all_tracks_connects_first_instead_of_reporting_an_empty_library():
    """A fresh PlexClient has server=None until something dials it.

    ``_can_query()`` alone answers False there, so the sweep returned [] and a
    transfer over two brand-new clients reported considered=0/matched=0 — a
    silent no-op that looks exactly like a correct run over an empty library.
    """
    import core.plex_client as pc

    client = pc.PlexClient(config={'base_url': 'http://second:32400', 'token': 't'})
    calls = []

    def _connect():
        calls.append('ensure_connection')
        client.server = object()
        client.music_library = object()
        return True

    client.ensure_connection = _connect
    client._all_tracks = lambda: ['track-a', 'track-b']

    assert client.get_all_tracks() == ['track-a', 'track-b']
    assert calls == ['ensure_connection']


def test_search_tracks_connects_first_so_the_fuzzy_fallback_works():
    """Same gap on the no-guid fallback path the transfer leans on."""
    import core.plex_client as pc

    client = pc.PlexClient(config={'base_url': 'http://second:32400', 'token': 't'})
    client._all_tracks = lambda: []

    # Not connected and unable to connect: an empty result, not an exception.
    client.ensure_connection = lambda: False
    assert client.search_tracks("Song", "Artist") == []

    connected = []
    client.ensure_connection = lambda: (connected.append(1), True)[1]
    client.music_library = object()
    client.server = object()
    client._search_artists_by_name = lambda **kw: []
    client._search_general = lambda **kw: []
    assert client.search_tracks("Song", "Artist") == []
    assert connected == [1]


def test_user_rating_transfers_not_the_agent_rating():
    """A raw plexapi Track has BOTH userRating (stars) and rating (the agent's
    critic score). Reading ``rating`` first copied a critic score as if the user
    had set it, and made an unrated track look rated."""
    from core.plex_transfer import _rating_of

    class RawTrack:
        userRating = None      # the user never rated it
        rating = 8.0           # ...but the music agent scored it

    assert _rating_of(RawTrack()) is None

    rated = RawTrack()
    rated.userRating = 4.0
    assert _rating_of(rated) == 4.0

    # TrackInfo has no userRating; its `rating` IS the user rating.
    assert _rating_of(_ti("Song", "Artist", rating=6.0)) == 6.0


def test_unrated_source_track_with_an_agent_rating_is_never_transferred():
    """End to end: the agent score must not reach the destination."""

    class AgentRatedTrack(FakeTrack):
        rating = 9.0           # agent score, alongside userRating=None

    src = FakeServer(tracks=[AgentRatedTrack("Song", "Artist", guid="g1", rating=None)])
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=None)])

    report = PlexToPlexTransfer(src, dst).transfer_ratings(dry_run=False)

    assert report.considered == 0
    assert dst.rating_writes == []


def test_artist_lookup_does_not_fetch_per_track():
    """``Track.artist()`` is an HTTP fetch per track; grandparentTitle is already
    on the listing. Thousands of round trips is the difference between a run
    that finishes and one the worker timeout kills."""
    from core.plex_transfer import _artist_of

    class RawTrack:
        grandparentTitle = "Artist"

        def artist(self):
            raise AssertionError("artist() must not be called")

    assert _artist_of(RawTrack()) == "Artist"

    # Still falls back when the listing didn't carry it.
    assert _artist_of(FakeTrack("Song", "Fallback Artist")) == "Fallback Artist"


def test_report_names_the_servers_and_libraries_it_touched():
    """Zeros from the wrong library look identical to zeros from no data."""

    class FakeClient(FakeServer):
        def __init__(self, name, library, **kw):
            super().__init__(**kw)
            self.server = type("S", (), {"friendlyName": name})()
            self.music_library = type("L", (), {"title": library})()
            self._all_libraries_mode = False

    src = FakeClient("Home", "Music", tracks=[FakeTrack("Song", "Artist", guid="g1", rating=8.0)])
    dst = FakeClient("Cabin", "My Music", tracks=[FakeTrack("Song", "Artist", guid="g1")])

    report = PlexToPlexTransfer(src, dst).transfer_ratings()

    assert report.source_server == "Home / Music"
    assert report.dest_server == "Cabin / My Music"
    assert report.to_dict()["dest_server"] == "Cabin / My Music"


def test_build_transfer_refuses_an_unreachable_server(monkeypatch):
    """Better a named error than a report of zeros."""
    import core.plex_transfer as pt

    monkeypatch.setattr(pt.config_manager, "get_plex_config",
                        lambda: {'base_url': 'http://a:32400', 'token': 'a'})
    monkeypatch.setattr(pt.config_manager, "get_plex_secondary_config",
                        lambda: {'base_url': 'http://b:32400', 'token': 'b'})

    import core.plex_client as pc
    monkeypatch.setattr(pc.PlexClient, "is_connected", lambda self: False)

    try:
        pt.build_transfer()
    except pt.TransferUnavailable as e:
        assert "Could not connect" in str(e)
    else:
        raise AssertionError("expected TransferUnavailable")


def test_build_transfer_refuses_a_server_with_no_music_library(monkeypatch):
    import core.plex_transfer as pt
    import core.plex_client as pc

    monkeypatch.setattr(pt.config_manager, "get_plex_config",
                        lambda: {'base_url': 'http://a:32400', 'token': 'a'})
    monkeypatch.setattr(pt.config_manager, "get_plex_secondary_config",
                        lambda: {'base_url': 'http://b:32400', 'token': 'b'})
    monkeypatch.setattr(pc.PlexClient, "is_connected", lambda self: True)
    monkeypatch.setattr(pc.PlexClient, "is_fully_configured", lambda self: False)

    try:
        pt.build_transfer()
    except pt.TransferUnavailable as e:
        assert "no music library" in str(e)
    else:
        raise AssertionError("expected TransferUnavailable")


def test_progress_reports_phases_and_never_breaks_the_run():
    src = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1", rating=8.0)])
    dst = FakeServer(tracks=[FakeTrack("Song", "Artist", guid="g1")])

    phases = []
    PlexToPlexTransfer(src, dst, on_progress=phases.append).transfer_ratings()
    assert any("source library" in p for p in phases)

    def _boom(_message):
        raise RuntimeError("UI went away")

    report = PlexToPlexTransfer(src, dst, on_progress=_boom).transfer_ratings()
    assert report.considered == 1
