"""
Reaparr Download Client

Download source backed by Reaparr, which fetches media from Plex servers the
user already has shared access to — a capability no other SoulSync source has.

Division of responsibility (see ``docs/reaparr-api-contract.md``):

- **SoulSync** decides *what is missing* (matching engine, SoulID identity,
  Duplicates maintenance) and owns everything post-download (AcoustID
  verification, MusicBrainz tagging, organization, Plex sync).
- **Reaparr** answers *"can I get this, and fetch it"*.

Deduplication is therefore SoulSync's job and happens **before** transfer, which
is why ``search`` surfaces Reaparr's Plex identity fields on each result via
``TrackResult._source_metadata`` instead of discarding them.

Two auth schemes, because Reaparr uses two (both verified in its source):

- Search  — ``apikey`` query parameter (``IndexerAuthenticationPreProcessor``).
- Transfer — ``SID`` cookie from ``/auth/login``
  (``DownloadClientAuthenticationPreProcessor``).

STATUS: Reaparr does not serve music yet. Its Torznab validator rejects
``t=music``, its capabilities advertise no Audio categories, and
``TorrentMetadataDTOValidator`` restricts ``Type`` to Episode/Movie with a
``VideoQuality``. This client is written against the proposed contract and is
covered by ``tests/test_reaparr_client.py`` against a mock transport. It has
never been run against a live Reaparr — when the server side lands, the search
and transfer paths need real verification before this source is trusted.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests as http_requests

from config.settings import config_manager
from core.async_utils import run_blocking
from core.download_plugins.album_bundle import resolve_reported_save_path
from core.download_plugins.base import DownloadSourcePlugin
from core.download_plugins.types import AlbumResult, DownloadStatus, TrackResult
from utils.logging_config import get_logger

logger = get_logger("reaparr_client")

# Reaparr's PublicApiRoutes, mirrored. Kept as module constants so a route
# change on the server side is a one-line edit here rather than a grep.
_BASE = '/api/public'
_INDEXER = f'{_BASE}/indexer/api'
_DOWNLOAD_CLIENT = f'{_BASE}/download-client/api/v2'
_MUSIC_SEARCH = f'{_BASE}/music/search'

# Terminal states, mirroring the strings the rest of SoulSync's download
# pipeline already switches on (see LidarrDownloadClient / TorrentDownloadPlugin).
_STATE_COMPLETED = 'Completed, Succeeded'
_STATE_ERRORED = 'Errored'
_STATE_CANCELLED = 'Cancelled'
_TERMINAL_STATES = (_STATE_COMPLETED, _STATE_ERRORED, _STATE_CANCELLED)


class ReaparrDownloadClient(DownloadSourcePlugin):
    """Reaparr download source.

    Implements the ``DownloadSourcePlugin`` protocol so the orchestrator can
    dispatch to it generically. Source-specific extras stay private.
    """

    def __init__(self, download_path: str = None):
        if download_path is None:
            download_path = config_manager.get('soulseek.download_path', './downloads')
        self.download_path = Path(download_path)
        try:
            self.download_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not verify download path {self.download_path}: {e}")

        self.active_downloads: Dict[str, Dict[str, Any]] = {}
        self._download_lock = threading.Lock()
        # Reaparr's /torrents/add returns qBittorrent's bare 'Ok.' with no hash,
        # so the hash is recovered by diffing /torrents/info around the add.
        # That diff is only sound if one add is in flight at a time. See the
        # "Matching an add to its hash" section of the contract for the
        # server-side fix that would let this lock go away.
        self._add_lock = threading.Lock()
        self._session: Optional[http_requests.Session] = None
        self._session_lock = threading.Lock()
        self.shutdown_check = None
        self._load_config()

    def _load_config(self):
        self._url = (config_manager.get('reaparr.url', '') or '').rstrip('/')
        self._api_key = config_manager.get('reaparr.api_key', '') or ''
        self._username = config_manager.get('reaparr.username', '') or ''
        self._password = config_manager.get('reaparr.password', '') or ''
        self._category = config_manager.get('reaparr.category', 'soulsync') or 'soulsync'
        self._cleanup = config_manager.get('reaparr.cleanup_after_import', True)
        self._timeout = int(config_manager.get('reaparr.timeout_seconds', 30) or 30)
        self._poll_timeout = int(config_manager.get('reaparr.poll_timeout_seconds', 1800) or 1800)

    def set_shutdown_check(self, check_callable):
        self.shutdown_check = check_callable

    def reload_settings(self):
        self._load_config()
        # Credentials may have changed — drop the authenticated session so the
        # next transfer logs in again rather than reusing a stale SID.
        with self._session_lock:
            self._session = None
        logger.info("Reaparr settings reloaded")

    # ==================== Interface Methods ====================

    def is_configured(self) -> bool:
        return bool(self._url and self._api_key)

    def is_available(self) -> bool:
        return self.is_configured()

    async def check_connection(self) -> bool:
        if not self.is_configured():
            return False
        try:
            return await run_blocking(self._check_connection_sync)
        except Exception:
            return False

    def _check_connection_sync(self) -> bool:
        """Probe via the indexer capabilities endpoint.

        Chosen because it exists on Reaparr today and uses the apikey scheme,
        so a connection test validates the URL and the API key together
        without needing download-client credentials.
        """
        try:
            resp = http_requests.get(
                f"{self._url}{_INDEXER}",
                params={'t': 'caps', 'apikey': self._api_key},
                timeout=self._timeout,
            )
            return resp.ok
        except Exception as e:
            logger.error(f"Reaparr connection check failed: {e}")
            return False

    async def search(self, query: str, timeout: int = None,
                     progress_callback=None) -> Tuple[List[TrackResult], List[AlbumResult]]:
        """Search Reaparr's reachable Plex libraries for music."""
        if not self.is_configured():
            return ([], [])
        try:
            return await run_blocking(self._search_sync, query, timeout)
        except Exception as e:
            logger.error(f"Reaparr search failed: {e}")
            return ([], [])

    def _search_sync(self, query: str,
                     timeout: int = None) -> Tuple[List[TrackResult], List[AlbumResult]]:
        try:
            resp = http_requests.get(
                f"{self._url}{_MUSIC_SEARCH}",
                params={'apikey': self._api_key, 'q': query, 'limit': 100},
                timeout=timeout or self._timeout,
            )
            if not resp.ok:
                # 404 is the expected response until Reaparr ships /music/search.
                # Log it at debug so an unconfigured-but-enabled source doesn't
                # spam the log on every search.
                logger.debug(
                    f"Reaparr music search returned HTTP {resp.status_code} — "
                    f"endpoint may not be implemented yet"
                )
                return ([], [])
            payload = resp.json() or {}
        except Exception as e:
            logger.error(f"Reaparr search error: {e}")
            return ([], [])

        results = payload.get('results') or []
        if not isinstance(results, list):
            logger.warning("Reaparr search returned a non-list 'results' field")
            return ([], [])

        track_results: List[TrackResult] = []
        for item in results:
            tr = self._to_track_result(item)
            if tr is not None:
                track_results.append(tr)

        album_results = self._group_into_albums(track_results)
        logger.info(
            f"Reaparr search '{query}': {len(track_results)} tracks, "
            f"{len(album_results)} albums"
        )
        return (track_results, album_results)

    def _to_track_result(self, item: Dict[str, Any]) -> Optional[TrackResult]:
        """Project one contract result onto a TrackResult.

        Returns None for items without a ``download_token``, since those can
        never be fetched — surfacing them would produce search hits that fail
        at download time.
        """
        if not isinstance(item, dict):
            return None
        token = item.get('download_token')
        if not token:
            return None

        artist = item.get('artist') or ''
        album = item.get('album') or ''
        title = item.get('title') or ''

        # Same ``id||display`` filename convention the other id-based sources
        # use (Lidarr, HiFi). ``download()`` splits it back apart. The token is
        # opaque — never parsed here.
        display = ' - '.join(p for p in (artist, album, title) if p)
        filename = f"{token}||{display}"

        return TrackResult(
            username='reaparr',
            filename=filename,
            size=item.get('size_bytes') or 0,
            bitrate=item.get('bitrate_kbps'),
            duration=item.get('duration_ms'),
            quality=(item.get('format') or 'unknown').lower(),
            # Reaparr pulls from Plex servers over HTTP — there is no peer
            # queue or slot model. Neutral-to-favourable constants keep
            # quality_score from penalising this source for fields that do
            # not apply to it.
            free_upload_slots=1,
            upload_speed=0,
            queue_length=0,
            artist=artist or None,
            title=title or None,
            album=album or None,
            track_number=item.get('track_number'),
            sample_rate=item.get('sample_rate_hz'),
            bit_depth=item.get('bit_depth'),
            # Plex identity, preserved for dedup-before-transfer. The matching
            # engine reads this to decide whether SoulSync already owns the
            # track — Reaparr deliberately does not filter on its side.
            _source_metadata={
                'source': 'reaparr',
                'download_token': token,
                'identity': item.get('identity') or {},
                'year': item.get('year'),
                'disc_number': item.get('disc_number'),
            },
        )

    @staticmethod
    def _group_into_albums(tracks: List[TrackResult]) -> List[AlbumResult]:
        """Group track hits into AlbumResults by (artist, album).

        Reaparr returns a flat track list; the album-context download flow
        needs album-shaped results. Tracks with no album title are skipped
        rather than collapsed into a single empty-named album.
        """
        grouped: Dict[Tuple[str, str], List[TrackResult]] = {}
        for tr in tracks:
            if not tr.album:
                continue
            grouped.setdefault((tr.artist or '', tr.album), []).append(tr)

        albums: List[AlbumResult] = []
        for (artist, album_title), album_tracks in grouped.items():
            qualities = [t.quality for t in album_tracks if t.quality]
            dominant = max(set(qualities), key=qualities.count) if qualities else 'unknown'
            year = None
            for t in album_tracks:
                meta = t._source_metadata or {}
                if meta.get('year'):
                    year = str(meta['year'])
                    break
            albums.append(AlbumResult(
                username='reaparr',
                album_path=f"reaparr/{artist}/{album_title}",
                album_title=album_title,
                artist=artist or None,
                track_count=len(album_tracks),
                total_size=sum(t.size or 0 for t in album_tracks),
                tracks=album_tracks,
                dominant_quality=dominant,
                year=year,
                free_upload_slots=1,
                upload_speed=0,
                queue_length=0,
            ))
        return albums

    async def download(self, username: str, filename: str,
                       file_size: int = 0) -> Optional[str]:
        """Queue a transfer. Returns a download_id to poll, or None."""
        if not self.is_configured():
            return None

        download_id = str(uuid.uuid4())

        token = ''
        display_name = filename
        if '||' in filename:
            token, display_name = filename.split('||', 1)

        if not token:
            logger.error(
                f"Reaparr download rejected — no download_token in filename: {filename!r}"
            )
            return None

        with self._download_lock:
            self.active_downloads[download_id] = {
                'id': download_id,
                'filename': filename,
                'display_name': display_name,
                'username': 'reaparr',
                'state': 'Initializing',
                'progress': 0.0,
                'size': file_size,
                'transferred': 0,
                'speed': 0,
                'file_path': None,
                'hash': None,
            }

        thread = threading.Thread(
            target=self._download_thread_worker,
            args=(download_id, token, display_name),
            daemon=True,
            name=f'reaparr-dl-{download_id[:8]}',
        )
        thread.start()
        return download_id

    def _download_thread_worker(self, download_id: str, token: str, display_name: str):
        """Fetch the .torrent for a part, hand it to Reaparr, poll to completion."""
        try:
            session = self._authenticated_session()
            if session is None:
                self._set_error(download_id, 'Reaparr login failed')
                return

            torrent_bytes = self._fetch_torrent(session, token)
            if not torrent_bytes:
                self._set_error(download_id, 'Could not fetch torrent from Reaparr')
                return

            info_hash = self._add_torrent(session, torrent_bytes)
            if not info_hash:
                self._set_error(download_id, 'Reaparr rejected the torrent add')
                return

            with self._download_lock:
                if download_id not in self.active_downloads:
                    return
                self.active_downloads[download_id]['hash'] = info_hash
                self.active_downloads[download_id]['state'] = 'InProgress, Downloading'

            self._poll_until_done(download_id, session, info_hash, display_name)

        except Exception as e:
            logger.error(f"Reaparr download thread failed: {e}")
            self._set_error(download_id, str(e))

    def _poll_until_done(self, download_id: str, session: http_requests.Session,
                         info_hash: str, display_name: str):
        deadline = time.time() + self._poll_timeout
        while time.time() < deadline:
            if self.shutdown_check and self.shutdown_check():
                self._set_error(download_id, 'Server shutting down')
                return

            with self._download_lock:
                entry = self.active_downloads.get(download_id)
                if entry is None:
                    return
                if entry['state'] == _STATE_CANCELLED:
                    return

            record = self._torrent_info(session, info_hash)
            if record is not None:
                # qBittorrent reports progress as 0.0-1.0; SoulSync uses 0-100.
                progress = float(record.get('progress') or 0.0) * 100.0
                state = (record.get('state') or '').lower()

                with self._download_lock:
                    entry = self.active_downloads.get(download_id)
                    if entry is None:
                        return
                    entry['progress'] = min(progress, 99.0)
                    entry['size'] = int(record.get('size') or entry.get('size') or 0)
                    entry['speed'] = int(record.get('dlspeed') or 0)
                    entry['transferred'] = int(entry['size'] * (progress / 100.0))

                if state == 'error':
                    self._set_error(download_id, 'Reaparr reported a transfer error')
                    return

                if progress >= 100.0:
                    self._finalize(download_id, record, display_name)
                    if self._cleanup:
                        self._delete_torrent(session, info_hash)
                    return

            time.sleep(1)

        self._set_error(download_id, 'Download timed out')

    def _finalize(self, download_id: str, record: Dict[str, Any], display_name: str):
        """Resolve the completed file's path into this process's namespace."""
        reported = record.get('content_path') or record.get('save_path') or ''
        # Reuse the shared remote-path resolver the torrent/usenet plugins use
        # rather than a Reaparr-specific one — same arr-stack mount mismatch,
        # same escape hatch (download_source.path_mappings).
        resolved = resolve_reported_save_path(
            reported,
            expect_name=record.get('name') or None,
        )

        with self._download_lock:
            entry = self.active_downloads.get(download_id)
            if entry is None:
                return
            entry['state'] = _STATE_COMPLETED
            entry['progress'] = 100.0
            entry['transferred'] = entry.get('size') or 0
            entry['speed'] = 0
            entry['file_path'] = resolved or None

        if reported and resolved != reported:
            logger.info(
                f"Reaparr download complete: {display_name} — remapped "
                f"{reported!r} -> {resolved!r}"
            )
        else:
            logger.info(f"Reaparr download complete: {display_name} -> {resolved!r}")

    def _set_error(self, download_id: str, error: str):
        with self._download_lock:
            if download_id in self.active_downloads:
                self.active_downloads[download_id]['state'] = _STATE_ERRORED
                self.active_downloads[download_id]['error'] = error
        logger.error(f"Reaparr download error: {error}")

    async def get_all_downloads(self) -> List[DownloadStatus]:
        with self._download_lock:
            return [self._to_status(dl) for dl in self.active_downloads.values()]

    async def get_download_status(self, download_id: str) -> Optional[DownloadStatus]:
        with self._download_lock:
            dl = self.active_downloads.get(download_id)
            return self._to_status(dl) if dl else None

    @staticmethod
    def _to_status(dl: Dict[str, Any]) -> DownloadStatus:
        filename = dl['filename']
        if dl['state'] == _STATE_ERRORED and dl.get('error'):
            filename = f"{filename} — {dl['error']}"
        return DownloadStatus(
            id=dl['id'],
            filename=filename,
            username='reaparr',
            state=dl['state'],
            progress=dl['progress'],
            size=dl.get('size') or 0,
            transferred=dl.get('transferred') or 0,
            speed=dl.get('speed') or 0,
            file_path=dl.get('file_path'),
        )

    async def cancel_download(self, download_id: str, username: str = None,
                              remove: bool = False) -> bool:
        with self._download_lock:
            entry = self.active_downloads.get(download_id)
            if entry is None:
                return False
            info_hash = entry.get('hash')
            if remove:
                del self.active_downloads[download_id]
            else:
                entry['state'] = _STATE_CANCELLED

        # Tell Reaparr to drop it too, so cancelling here doesn't leave the
        # transfer running server-side and untracked — the orphan case the
        # torrent plugin documents.
        if info_hash:
            session = self._authenticated_session()
            if session is not None:
                self._delete_torrent(session, info_hash)
        return True

    async def clear_all_completed_downloads(self) -> bool:
        with self._download_lock:
            self.active_downloads = {
                k: v for k, v in self.active_downloads.items()
                if v['state'] not in _TERMINAL_STATES
            }
        return True

    # ==================== Reaparr API Helpers ====================

    def _authenticated_session(self) -> Optional[http_requests.Session]:
        """Return a session holding a valid SID cookie, logging in if needed.

        Transfer endpoints use cookie auth, not the API key. Cached because
        Reaparr issues a session per login and re-logging in on every call
        would churn its auth database.
        """
        with self._session_lock:
            if self._session is not None:
                return self._session
            try:
                session = http_requests.Session()
                resp = session.post(
                    f"{self._url}{_DOWNLOAD_CLIENT}/auth/login",
                    data={'username': self._username, 'password': self._password},
                    timeout=self._timeout,
                )
                if not resp.ok or 'SID' not in session.cookies:
                    logger.error(
                        f"Reaparr login failed: HTTP {resp.status_code} "
                        f"(SID cookie {'present' if 'SID' in session.cookies else 'missing'})"
                    )
                    return None
                self._session = session
                return session
            except Exception as e:
                logger.error(f"Reaparr login error: {e}")
                return None

    def _fetch_torrent(self, session: http_requests.Session, token: str) -> Optional[bytes]:
        """GET the .torrent for a media part. ``token`` is an opaque query string."""
        try:
            resp = session.get(
                f"{self._url}{_DOWNLOAD_CLIENT}/torrents/download?{token}",
                timeout=self._timeout,
            )
            if not resp.ok:
                logger.error(f"Reaparr torrent fetch returned HTTP {resp.status_code}")
                return None
            return resp.content or None
        except Exception as e:
            logger.error(f"Reaparr torrent fetch error: {e}")
            return None

    def _add_torrent(self, session: http_requests.Session,
                     torrent_bytes: bytes) -> Optional[str]:
        """Upload the .torrent and recover its info hash.

        Reaparr mirrors qBittorrent's ``/torrents/add``, which answers a bare
        ``Ok.`` with no hash. The hash is recovered by diffing the category
        listing around the add, which is only sound while a single add is in
        flight — hence ``_add_lock``.
        """
        with self._add_lock:
            before = {r.get('hash') for r in self._torrent_list(session)}
            try:
                resp = session.post(
                    f"{self._url}{_DOWNLOAD_CLIENT}/torrents/add",
                    files={'torrents': ('reaparr.torrent', torrent_bytes,
                                        'application/x-bittorrent')},
                    data={'category': self._category},
                    timeout=self._timeout,
                )
                if not resp.ok:
                    logger.error(f"Reaparr torrent add returned HTTP {resp.status_code}")
                    return None
            except Exception as e:
                logger.error(f"Reaparr torrent add error: {e}")
                return None

            # The add is asynchronous server-side; give it a few polls to appear.
            for _ in range(10):
                after = {r.get('hash') for r in self._torrent_list(session)}
                new = after - before
                if len(new) == 1:
                    return new.pop()
                if len(new) > 1:
                    # Concurrent activity outside our lock (a manual add in
                    # Reaparr's own UI, say). Refusing is safer than adopting
                    # the wrong hash and reporting another job's progress.
                    logger.error(
                        f"Reaparr add: {len(new)} new torrents appeared, cannot "
                        f"identify ours — see the contract's hash-return request"
                    )
                    return None
                time.sleep(0.5)

            logger.error("Reaparr add: torrent never appeared in the category listing")
            return None

    def _torrent_list(self, session: http_requests.Session) -> List[Dict[str, Any]]:
        try:
            resp = session.get(
                f"{self._url}{_DOWNLOAD_CLIENT}/torrents/info",
                params={'category': self._category},
                timeout=self._timeout,
            )
            if not resp.ok:
                return []
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"Reaparr torrents/info failed: {e}")
            return []

    def _torrent_info(self, session: http_requests.Session,
                      info_hash: str) -> Optional[Dict[str, Any]]:
        for record in self._torrent_list(session):
            if record.get('hash') == info_hash:
                return record
        return None

    def _delete_torrent(self, session: http_requests.Session, info_hash: str) -> bool:
        try:
            resp = session.post(
                f"{self._url}{_DOWNLOAD_CLIENT}/torrents/delete",
                data={'hashes': info_hash, 'deleteFiles': 'false'},
                timeout=self._timeout,
            )
            return resp.ok
        except Exception as e:
            logger.debug(f"Reaparr torrent delete failed: {e}")
            return False
