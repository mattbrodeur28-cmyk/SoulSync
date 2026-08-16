"""Unit tests for ``core/reaparr_client.py``.

Reaparr does not serve music yet — its Torznab validator rejects ``t=music``,
its capabilities advertise no Audio categories, and ``TorrentMetadataDTOValidator``
restricts ``Type`` to Episode/Movie. So these tests exercise the client against
a mock transport shaped to ``docs/reaparr-api-contract.md``, not a live server.

What that buys and what it does not:

- **Covered:** contract parsing, the opaque-token round trip, album grouping,
  identity preservation for dedup-before-transfer, qBittorrent progress
  mapping, the add/hash-diff race guard, and every terminal-state transition.
- **NOT covered:** that Reaparr actually answers any of these calls, that the
  response field names match, or that a real transfer lands a file. Those need
  a Reaparr build with music support.

If the contract changes, these tests should fail — that is their point.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from core.reaparr_client import ReaparrDownloadClient


def _run(coro):
    """Drive a coroutine to completion. Mirrors the helper in
    ``tests/test_torrent_usenet_plugins.py`` — pytest-asyncio is not a
    dependency of this repo."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


_CONFIG = {
    'reaparr.url': 'http://reaparr.test:5000',
    'reaparr.api_key': 'test-key',
    'reaparr.username': 'user',
    'reaparr.password': 'pass',
    'reaparr.category': 'soulsync',
    'reaparr.cleanup_after_import': True,
    'reaparr.timeout_seconds': 5,
    'reaparr.poll_timeout_seconds': 10,
}


def _make_client(tmp_path, overrides: Optional[Dict[str, Any]] = None) -> ReaparrDownloadClient:
    cfg = dict(_CONFIG)
    if overrides:
        cfg.update(overrides)

    def fake_get(key, default=None):
        return cfg.get(key, default)

    with patch('core.reaparr_client.config_manager.get', side_effect=fake_get):
        return ReaparrDownloadClient(download_path=str(tmp_path))


class _FakeResponse:
    def __init__(self, *, ok=True, status_code=200, json_data=None, content=b''):
        self.ok = ok
        self.status_code = status_code
        self._json = json_data
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError('no json')
        return self._json


def _contract_item(**overrides) -> Dict[str, Any]:
    """One result shaped exactly like docs/reaparr-api-contract.md."""
    item = {
        'download_token': 'Type=Music&MediaId=12&PartId=56',
        'artist': 'Radiohead',
        'album': 'In Rainbows',
        'title': 'Nude',
        'track_number': 3,
        'disc_number': 1,
        'year': '2007',
        'duration_ms': 255000,
        'size_bytes': 41234567,
        'format': 'FLAC',
        'bitrate_kbps': 1008,
        'sample_rate_hz': 44100,
        'bit_depth': 16,
        'identity': {
            'server_id': 1,
            'server_name': "Friend's Plex",
            'library_id': 2,
            'plex_rating_key': 91234,
        },
    }
    item.update(overrides)
    return item


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_is_configured_requires_url_and_api_key(tmp_path) -> None:
    assert _make_client(tmp_path).is_configured() is True
    assert _make_client(tmp_path, {'reaparr.api_key': ''}).is_configured() is False
    assert _make_client(tmp_path, {'reaparr.url': ''}).is_configured() is False


def test_reload_settings_drops_cached_session(tmp_path) -> None:
    """Credentials may have changed; a cached SID must not survive."""
    client = _make_client(tmp_path)
    client._session = object()
    with patch('core.reaparr_client.config_manager.get', side_effect=_CONFIG.get):
        client.reload_settings()
    assert client._session is None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_search_returns_empty_when_unconfigured(tmp_path) -> None:
    client = _make_client(tmp_path, {'reaparr.api_key': ''})
    tracks, albums = client._search_sync('anything')
    assert (tracks, albums) == ([], [])


def test_search_parses_contract_payload(tmp_path) -> None:
    client = _make_client(tmp_path)
    payload = {'results': [_contract_item()], 'total': 1}

    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data=payload)):
        tracks, albums = client._search_sync('radiohead nude')

    assert len(tracks) == 1
    tr = tracks[0]
    assert tr.username == 'reaparr'
    assert tr.artist == 'Radiohead'
    assert tr.album == 'In Rainbows'
    assert tr.title == 'Nude'
    assert tr.track_number == 3
    assert tr.size == 41234567
    assert tr.bitrate == 1008
    assert tr.sample_rate == 44100
    assert tr.bit_depth == 16
    # Format is lower-cased so quality_score's weight table matches.
    assert tr.quality == 'flac'


def test_search_encodes_opaque_token_into_filename(tmp_path) -> None:
    """download() recovers the token by splitting on '||'. The token is never
    parsed — Reaparr may change its internal shape freely."""
    client = _make_client(tmp_path)
    token = 'Type=Music&MediaId=99&Quality=Lossless&Weird=a||b-ish'
    payload = {'results': [_contract_item(download_token=token)]}

    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data=payload)):
        tracks, _ = client._search_sync('q')

    recovered, display = tracks[0].filename.split('||', 1)
    assert recovered == token.split('||', 1)[0]
    assert 'Radiohead' in display


def test_search_preserves_plex_identity_for_dedup(tmp_path) -> None:
    """Dedup is SoulSync's job and happens BEFORE transfer, so the matching
    engine needs Reaparr's Plex identity on every hit."""
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data={'results': [_contract_item()]})):
        tracks, _ = client._search_sync('q')

    meta = tracks[0]._source_metadata
    assert meta['source'] == 'reaparr'
    assert meta['identity']['plex_rating_key'] == 91234
    assert meta['identity']['server_name'] == "Friend's Plex"
    assert meta['download_token']


def test_search_skips_results_without_download_token(tmp_path) -> None:
    """A hit with no token can never be fetched; surfacing it would produce a
    search result that fails at download time."""
    client = _make_client(tmp_path)
    payload = {'results': [
        _contract_item(),
        _contract_item(download_token=None, title='Unfetchable'),
        _contract_item(download_token='', title='Also Unfetchable'),
    ]}
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data=payload)):
        tracks, _ = client._search_sync('q')

    assert [t.title for t in tracks] == ['Nude']


def test_search_groups_tracks_into_albums(tmp_path) -> None:
    client = _make_client(tmp_path)
    payload = {'results': [
        _contract_item(title='Nude', track_number=3),
        _contract_item(title='Reckoner', track_number=7),
        _contract_item(artist='Portishead', album='Dummy', title='Roads'),
    ]}
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data=payload)):
        tracks, albums = client._search_sync('q')

    assert len(tracks) == 3
    by_title = {a.album_title: a for a in albums}
    assert by_title['In Rainbows'].track_count == 2
    assert by_title['In Rainbows'].dominant_quality == 'flac'
    assert by_title['In Rainbows'].year == '2007'
    assert by_title['Dummy'].track_count == 1


def test_search_skips_album_grouping_for_tracks_without_album(tmp_path) -> None:
    """Tracks with no album must not collapse into one empty-named album."""
    client = _make_client(tmp_path)
    payload = {'results': [
        _contract_item(album=None, title='Loose A'),
        _contract_item(album=None, title='Loose B'),
    ]}
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data=payload)):
        tracks, albums = client._search_sync('q')

    assert len(tracks) == 2
    assert albums == []


def test_search_survives_missing_endpoint(tmp_path) -> None:
    """404 is the expected response until Reaparr ships /music/search."""
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(ok=False, status_code=404)):
        assert client._search_sync('q') == ([], [])


def test_search_survives_malformed_results(tmp_path) -> None:
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(json_data={'results': 'not-a-list'})):
        assert client._search_sync('q') == ([], [])


def test_search_survives_transport_error(tmp_path) -> None:
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               side_effect=OSError('connection refused')):
        assert client._search_sync('q') == ([], [])


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def test_check_connection_probes_indexer_caps(tmp_path) -> None:
    """caps uses the apikey scheme, so it validates URL + key together
    without needing download-client credentials."""
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse()) as mock_get:
        assert client._check_connection_sync() is True

    url = mock_get.call_args[0][0]
    params = mock_get.call_args[1]['params']
    assert url.endswith('/api/public/indexer/api')
    assert params == {'t': 'caps', 'apikey': 'test-key'}


def test_check_connection_false_on_rejection(tmp_path) -> None:
    client = _make_client(tmp_path)
    with patch('core.reaparr_client.http_requests.get',
               return_value=_FakeResponse(ok=False, status_code=401)):
        assert client._check_connection_sync() is False


# ---------------------------------------------------------------------------
# Download dispatch
# ---------------------------------------------------------------------------


def test_download_rejects_filename_without_token(tmp_path) -> None:
    """No token means nothing to ask Reaparr for. Fail fast rather than
    spawning a thread that cannot succeed."""
    client = _make_client(tmp_path)
    assert _run(client.download('reaparr', 'no-token-here')) is None
    assert client.active_downloads == {}


def test_download_returns_none_when_unconfigured(tmp_path) -> None:
    client = _make_client(tmp_path, {'reaparr.url': ''})
    assert _run(client.download('reaparr', 'tok||Artist - Album - Track')) is None


def test_download_registers_active_entry(tmp_path) -> None:
    client = _make_client(tmp_path)
    with patch.object(ReaparrDownloadClient, '_download_thread_worker'):
        download_id = _run(client.download('reaparr', 'tok||A - B - C', file_size=123))

    assert download_id
    entry = client.active_downloads[download_id]
    assert entry['state'] == 'Initializing'
    assert entry['display_name'] == 'A - B - C'
    assert entry['size'] == 123


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


def test_get_download_status_maps_fields(tmp_path) -> None:
    client = _make_client(tmp_path)
    client.active_downloads['x'] = {
        'id': 'x', 'filename': 'tok||A - B - C', 'username': 'reaparr',
        'state': 'InProgress, Downloading', 'progress': 42.0, 'size': 1000,
        'transferred': 420, 'speed': 99, 'file_path': None, 'hash': 'abc',
    }
    status = _run(client.get_download_status('x'))
    assert status.id == 'x'
    assert status.username == 'reaparr'
    assert status.progress == 42.0
    assert status.transferred == 420
    assert status.speed == 99


def test_errored_status_surfaces_reason_in_filename(tmp_path) -> None:
    client = _make_client(tmp_path)
    client.active_downloads['x'] = {
        'id': 'x', 'filename': 'tok||A - B - C', 'username': 'reaparr',
        'state': 'Errored', 'progress': 0.0, 'size': 0, 'transferred': 0,
        'speed': 0, 'file_path': None, 'error': 'Reaparr login failed',
    }
    status = _run(client.get_download_status('x'))
    assert 'Reaparr login failed' in status.filename


def test_get_download_status_unknown_id_returns_none(tmp_path) -> None:
    assert _run(_make_client(tmp_path).get_download_status('nope')) is None


# ---------------------------------------------------------------------------
# Cancellation / cleanup
# ---------------------------------------------------------------------------


def test_cancel_marks_cancelled_and_tells_reaparr(tmp_path) -> None:
    """Cancelling locally without telling Reaparr would leave the transfer
    running server-side and untracked — the orphan case."""
    client = _make_client(tmp_path)
    client.active_downloads['x'] = {
        'id': 'x', 'filename': 'f', 'username': 'reaparr', 'state': 'InProgress',
        'progress': 5.0, 'size': 0, 'transferred': 0, 'speed': 0,
        'file_path': None, 'hash': 'deadbeef',
    }
    with patch.object(ReaparrDownloadClient, '_authenticated_session', return_value=object()), \
         patch.object(ReaparrDownloadClient, '_delete_torrent', return_value=True) as mock_del:
        assert _run(client.cancel_download('x')) is True

    assert client.active_downloads['x']['state'] == 'Cancelled'
    assert mock_del.call_args[0][1] == 'deadbeef'


def test_cancel_with_remove_drops_the_entry(tmp_path) -> None:
    client = _make_client(tmp_path)
    client.active_downloads['x'] = {
        'id': 'x', 'filename': 'f', 'username': 'reaparr', 'state': 'InProgress',
        'progress': 5.0, 'size': 0, 'transferred': 0, 'speed': 0,
        'file_path': None, 'hash': None,
    }
    assert _run(client.cancel_download('x', remove=True)) is True
    assert 'x' not in client.active_downloads


def test_cancel_unknown_id_returns_false(tmp_path) -> None:
    assert _run(_make_client(tmp_path).cancel_download('nope')) is False


def test_clear_completed_keeps_only_in_flight(tmp_path) -> None:
    client = _make_client(tmp_path)
    for did, state in [('a', 'Completed, Succeeded'), ('b', 'Errored'),
                       ('c', 'Cancelled'), ('d', 'InProgress, Downloading')]:
        client.active_downloads[did] = {
            'id': did, 'filename': 'f', 'username': 'reaparr', 'state': state,
            'progress': 0.0, 'size': 0, 'transferred': 0, 'speed': 0,
            'file_path': None,
        }
    assert _run(client.clear_all_completed_downloads()) is True
    assert set(client.active_downloads) == {'d'}


# ---------------------------------------------------------------------------
# Transfer internals
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal stand-in for requests.Session covering the transfer calls."""

    def __init__(self, listings: List[List[Dict[str, Any]]]):
        self._listings = listings
        self.posts: List[str] = []

    def get(self, url, params=None, timeout=None):
        if '/torrents/info' in url:
            data = self._listings.pop(0) if len(self._listings) > 1 else self._listings[0]
            return _FakeResponse(json_data=data)
        return _FakeResponse(content=b'torrent-bytes')

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append(url)
        return _FakeResponse()


def test_add_torrent_recovers_hash_by_diffing(tmp_path) -> None:
    client = _make_client(tmp_path)
    session = _FakeSession([
        [{'hash': 'old'}],                    # before
        [{'hash': 'old'}, {'hash': 'new'}],   # after
    ])
    assert client._add_torrent(session, b'bytes') == 'new'


def test_add_torrent_refuses_when_hash_is_ambiguous(tmp_path) -> None:
    """Two new torrents means concurrent activity outside our lock. Adopting
    one would report another job's progress as ours — refuse instead."""
    client = _make_client(tmp_path)
    session = _FakeSession([
        [{'hash': 'old'}],
        [{'hash': 'old'}, {'hash': 'n1'}, {'hash': 'n2'}],
    ])
    assert client._add_torrent(session, b'bytes') is None


def test_torrent_info_selects_matching_hash(tmp_path) -> None:
    client = _make_client(tmp_path)
    session = _FakeSession([[{'hash': 'a', 'progress': 0.5},
                            {'hash': 'b', 'progress': 0.9}]])
    assert client._torrent_info(session, 'b')['progress'] == 0.9
    assert client._torrent_info(session, 'missing') is None


def test_finalize_maps_progress_and_resolves_path(tmp_path) -> None:
    """qBittorrent reports progress 0.0-1.0; SoulSync uses 0-100. And the
    reported path is remapped through the shared resolver."""
    client = _make_client(tmp_path)
    client.active_downloads['x'] = {
        'id': 'x', 'filename': 'f', 'username': 'reaparr', 'state': 'InProgress',
        'progress': 50.0, 'size': 2000, 'transferred': 1000, 'speed': 10,
        'file_path': None,
    }
    record = {'name': 'Nude.flac', 'content_path': '/reaparr/downloads/Nude.flac',
              'progress': 1.0, 'size': 2000}

    with patch('core.reaparr_client.resolve_reported_save_path',
               return_value='/app/downloads/Nude.flac') as mock_resolve:
        client._finalize('x', record, 'A - B - C')

    entry = client.active_downloads['x']
    assert entry['state'] == 'Completed, Succeeded'
    assert entry['progress'] == 100.0
    assert entry['transferred'] == 2000
    assert entry['file_path'] == '/app/downloads/Nude.flac'
    assert mock_resolve.call_args[0][0] == '/reaparr/downloads/Nude.flac'


def test_authenticated_session_requires_sid_cookie(tmp_path) -> None:
    """A 200 with no SID cookie is a failed login, not a success."""
    client = _make_client(tmp_path)

    class _NoCookieSession:
        cookies: Dict[str, str] = {}

        def post(self, *a, **kw):
            return _FakeResponse()

    with patch('core.reaparr_client.http_requests.Session', return_value=_NoCookieSession()):
        assert client._authenticated_session() is None
