"""Dedicated Last.fm Radio playlist persistence for Music Lite.

Last.fm Radio used to piggy-back on the ListenBrainz cache manager and tables.
Music Lite removes ListenBrainz, so this store owns Last.fm Radio persistence
directly. On first use it migrates legacy ``playlist_type='lastfm_radio'`` rows
into dedicated tables. Legacy rows are left untouched for rollback safety.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Dict, List

from utils.logging_config import get_logger

logger = get_logger("lastfm_playlist_store")


class LastFMPlaylistStore:
    def __init__(self, db_path: str, profile_id: int = 1):
        self.db_path = str(db_path)
        self.profile_id = int(profile_id or 1)
        self._ensure_tables()
        self._migrate_legacy_rows()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _ensure_tables(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lastfm_radio_playlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    creator TEXT DEFAULT 'Last.fm',
                    track_count INTEGER DEFAULT 0,
                    profile_id INTEGER DEFAULT 1,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(playlist_key, profile_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS lastfm_radio_tracks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    track_name TEXT NOT NULL,
                    artist_name TEXT NOT NULL,
                    album_name TEXT DEFAULT '',
                    duration_ms INTEGER DEFAULT 0,
                    recording_mbid TEXT,
                    album_cover_url TEXT,
                    additional_metadata TEXT,
                    UNIQUE(playlist_id, position)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _legacy_tables_exist(self, cur) -> bool:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('listenbrainz_playlists','listenbrainz_tracks')"
        )
        return len(cur.fetchall()) == 2

    def _migrate_legacy_rows(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            if not self._legacy_tables_exist(cur):
                return

            cur.execute("""
                SELECT id, playlist_mbid, title, creator, track_count, last_updated
                FROM listenbrainz_playlists
                WHERE playlist_type = 'lastfm_radio' AND profile_id = ?
                ORDER BY id
            """, (self.profile_id,))
            rows = cur.fetchall()

            for legacy_id, key, title, creator, count, updated in rows:
                cur.execute("""
                    INSERT OR IGNORE INTO lastfm_radio_playlists
                        (playlist_key, title, creator, track_count, profile_id, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    key, title, creator or "Last.fm", count or 0,
                    self.profile_id, updated,
                ))
                cur.execute("""
                    SELECT id FROM lastfm_radio_playlists
                    WHERE playlist_key = ? AND profile_id = ?
                """, (key, self.profile_id))
                new_row = cur.fetchone()
                if not new_row:
                    continue
                new_id = new_row[0]

                cur.execute(
                    "SELECT COUNT(*) FROM lastfm_radio_tracks WHERE playlist_id = ?",
                    (new_id,),
                )
                if cur.fetchone()[0]:
                    continue

                cur.execute("""
                    SELECT position, track_name, artist_name, album_name,
                           duration_ms, recording_mbid, album_cover_url,
                           additional_metadata
                    FROM listenbrainz_tracks
                    WHERE playlist_id = ?
                    ORDER BY position
                """, (legacy_id,))
                for track in cur.fetchall():
                    cur.execute("""
                        INSERT OR IGNORE INTO lastfm_radio_tracks
                            (playlist_id, position, track_name, artist_name,
                             album_name, duration_ms, recording_mbid,
                             album_cover_url, additional_metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (new_id, *track))

            conn.commit()
            if rows:
                logger.info(
                    "Migrated %s legacy Last.fm Radio playlist row(s) to dedicated storage",
                    len(rows),
                )
        except Exception as exc:
            conn.rollback()
            logger.warning("Last.fm Radio legacy migration skipped: %s", exc)
        finally:
            conn.close()

    def save_lastfm_radio_playlist(
        self,
        seed_track: str,
        seed_artist: str,
        similar_tracks: List[Dict],
    ) -> str:
        digest = hashlib.md5(
            f"{seed_artist.lower()}:{seed_track.lower()}".encode()
        ).hexdigest()[:12]
        key = f"lastfm_radio_{digest}"
        title = f"Last.fm Radio: {seed_track} by {seed_artist}"

        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id FROM lastfm_radio_playlists
                WHERE playlist_key = ? AND profile_id = ?
            """, (key, self.profile_id))
            row = cur.fetchone()

            if row:
                playlist_id = row[0]
                cur.execute(
                    "DELETE FROM lastfm_radio_tracks WHERE playlist_id = ?",
                    (playlist_id,),
                )
                cur.execute("""
                    UPDATE lastfm_radio_playlists
                    SET title = ?, track_count = ?, last_updated = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (title, len(similar_tracks), playlist_id))
            else:
                cur.execute("""
                    INSERT INTO lastfm_radio_playlists
                        (playlist_key, title, creator, track_count, profile_id)
                    VALUES (?, ?, 'Last.fm', ?, ?)
                """, (key, title, len(similar_tracks), self.profile_id))
                playlist_id = cur.lastrowid

            for idx, track in enumerate(similar_tracks):
                cur.execute("""
                    INSERT OR REPLACE INTO lastfm_radio_tracks
                        (playlist_id, position, track_name, artist_name,
                         album_name, duration_ms, recording_mbid,
                         album_cover_url, additional_metadata)
                    VALUES (?, ?, ?, ?, '', 0, ?, NULL, '{}')
                """, (
                    playlist_id,
                    idx,
                    track.get("name", ""),
                    track.get("artist", ""),
                    track.get("mbid", "") or "",
                ))

            cur.execute("""
                SELECT id, playlist_key FROM lastfm_radio_playlists
                WHERE profile_id = ?
                ORDER BY last_updated DESC
                LIMIT -1 OFFSET 10
            """, (self.profile_id,))
            stale = cur.fetchall()
            for stale_id, stale_key in stale:
                cur.execute(
                    "DELETE FROM lastfm_radio_tracks WHERE playlist_id = ?",
                    (stale_id,),
                )
                cur.execute(
                    "DELETE FROM lastfm_radio_playlists WHERE id = ?",
                    (stale_id,),
                )
                cur.execute("""
                    SELECT id FROM mirrored_playlists
                    WHERE source = 'lastfm' AND profile_id = ?
                      AND source_playlist_id = ?
                """, (self.profile_id, stale_key))
                mids = [r[0] for r in cur.fetchall()]
                for mid in mids:
                    cur.execute(
                        "DELETE FROM mirrored_playlist_tracks WHERE playlist_id = ?",
                        (mid,),
                    )
                    cur.execute(
                        "DELETE FROM mirrored_playlists WHERE id = ?",
                        (mid,),
                    )

            conn.commit()
            return key
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_cached_playlists(self, playlist_type: str = "lastfm_radio") -> List[Dict]:
        if playlist_type and playlist_type != "lastfm_radio":
            return []
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, playlist_key, title, creator, track_count, last_updated
                FROM lastfm_radio_playlists
                WHERE profile_id = ?
                ORDER BY last_updated DESC
                LIMIT 10
            """, (self.profile_id,))
            return [
                {
                    "id": row[0],
                    "playlist_mbid": row[1],
                    "title": row[2],
                    "creator": row[3],
                    "track_count": row[4],
                    "annotation": {},
                    "last_updated": row[5],
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()

    def get_cached_tracks(self, playlist_key: str) -> List[Dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id FROM lastfm_radio_playlists
                WHERE playlist_key = ? AND profile_id = ?
            """, (playlist_key, self.profile_id))
            row = cur.fetchone()
            if not row:
                return []
            playlist_id = row[0]

            cur.execute("""
                SELECT track_name, artist_name, album_name, duration_ms,
                       recording_mbid, album_cover_url, additional_metadata
                FROM lastfm_radio_tracks
                WHERE playlist_id = ?
                ORDER BY position
            """, (playlist_id,))
            tracks = []
            for item in cur.fetchall():
                tracks.append({
                    "track_name": item[0],
                    "artist_name": item[1],
                    "album_name": item[2],
                    "duration_ms": item[3],
                    "mbid": item[4],
                    "recording_mbid": item[4],
                    "release_mbid": None,
                    "album_cover_url": item[5],
                    "additional_metadata": (
                        json.loads(item[6]) if item[6] else {}
                    ),
                })
            return tracks
        finally:
            conn.close()
