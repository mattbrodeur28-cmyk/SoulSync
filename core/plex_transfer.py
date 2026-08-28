"""Plex-to-Plex transfer: playlists and per-track user ratings.

Moves playlists and ratings from one Plex server to another. Prompted by
MiniMediaPlaylists, but scoped to the part SoulSync actually lacked — its
Spotify-to-server sync, matching engine and write modes already existed.

**This never touches ``active_media_server``.** That single value is referenced
across 80+ files; server-to-server transfer instead holds two explicit
``PlexClient`` instances (see ``PlexClient(config=...)``), leaving the rest of
the app's idea of "the" media server untouched.

Matching, strongest first:

1. **guid** — two Plex servers that both matched a recording with the Plex Music
   agent report the same ``plex://track/<hash>``. Authoritative when present.
2. **title/artist search + ``MusicMatchingEngine``** above a threshold, for
   items with no guid (unmatched local files, non-Plex agents).

The destination guid index is built from ONE library sweep per run rather than a
lookup per track — a per-track query against a large library is what makes this
class of pass take hours.

Safety, because this writes to a live server:

- Dry-run is the DEFAULT; writes require ``dry_run=False``.
- Nothing is ever deleted, and a rating is never cleared. A track rated on the
  destination but not on the source is left alone.
- Idempotent: a rating equal to the destination's is skipped, so re-runs issue
  no writes.
- Every run returns a :class:`TransferReport` naming what did not match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config.settings import config_manager
from core.matching_engine import MusicMatchingEngine
from core.media_server.types import TrackInfo
from utils.logging_config import get_logger

logger = get_logger("plex_transfer")

DEFAULT_MATCH_THRESHOLD = 0.85


@dataclass
class TransferReport:
    """Outcome of one transfer run."""

    operation: str
    dry_run: bool
    considered: int = 0
    matched: int = 0
    written: int = 0
    skipped_same: int = 0
    unmatched: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    playlists: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched)

    def summary(self) -> str:
        mode = "DRY RUN — nothing written" if self.dry_run else "applied"
        parts = [
            f"{self.operation} ({mode})",
            f"considered={self.considered}",
            f"matched={self.matched}",
            f"unmatched={self.unmatched_count}",
            f"written={self.written}",
        ]
        if self.skipped_same:
            parts.append(f"already-equal={self.skipped_same}")
        if self.failures:
            parts.append(f"failures={len(self.failures)}")
        return ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation": self.operation,
            "dry_run": self.dry_run,
            "considered": self.considered,
            "matched": self.matched,
            "written": self.written,
            "skipped_same": self.skipped_same,
            "unmatched": self.unmatched,
            "unmatched_count": self.unmatched_count,
            "failures": self.failures,
            "playlists": self.playlists,
            "summary": self.summary(),
        }


def _artist_of(track: Any) -> str:
    """Artist name from either a TrackInfo or a raw plexapi Track."""
    artist = getattr(track, 'artist', None)
    if callable(artist):                      # raw plexapi Track.artist()
        try:
            got = artist()
            return getattr(got, 'title', '') or ''
        except Exception:
            return ''
    if isinstance(artist, str):
        return artist
    return getattr(track, 'grandparentTitle', '') or ''


def _rating_of(track: Any) -> Optional[float]:
    """User rating from either a TrackInfo (``rating``) or a raw Plex track."""
    value = getattr(track, 'rating', None)
    if value is None:
        value = getattr(track, 'userRating', None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PlexToPlexTransfer:
    """Transfers playlists and ratings from ``source`` to ``dest``."""

    def __init__(self, source, dest, *, match_threshold: float = DEFAULT_MATCH_THRESHOLD,
                 matching_engine=None, logger=logger):
        self.source = source
        self.dest = dest
        self.match_threshold = match_threshold
        self.matcher = matching_engine or MusicMatchingEngine()
        self.logger = logger
        self._guid_index: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Destination index
    # ------------------------------------------------------------------

    def _ensure_dest_index(self) -> Dict[str, Any]:
        """guid -> raw destination Plex track, from a single library sweep."""
        if self._guid_index is not None:
            return self._guid_index

        index: Dict[str, Any] = {}
        for track in self.dest.get_all_tracks() or []:
            guid = getattr(track, 'guid', None)
            if guid:
                index.setdefault(str(guid), track)
        self._guid_index = index
        self.logger.info(
            f"Destination index: {len(index)} track(s) with a usable guid")
        return index

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, track) -> Tuple[Optional[Any], str]:
        """Find ``track`` on the destination. Returns (plex_track_or_None, how)."""
        guid = getattr(track, 'guid', None)
        if guid:
            hit = self._ensure_dest_index().get(str(guid))
            if hit is not None:
                return hit, 'guid'

        title = getattr(track, 'title', '') or ''
        artist = _artist_of(track)
        if not title:
            return None, 'no-title'

        try:
            candidates = self.dest.search_tracks(title, artist, limit=10) or []
        except Exception as e:
            self.logger.debug(f"Destination search failed for '{title}': {e}")
            return None, 'search-error'

        best, best_score = None, 0.0
        source_duration = getattr(track, 'duration', 0) or 0
        for cand in candidates:
            score, _kind = self.matcher.score_track_match(
                title, [artist] if artist else [], source_duration,
                getattr(cand, 'title', '') or '',
                [getattr(cand, 'artist', '') or ''],
                getattr(cand, 'duration', 0) or 0,
            )
            if score > best_score:
                best_score, best = score, cand

        if best is not None and best_score >= self.match_threshold:
            # search_tracks returns TrackInfo with the raw object attached.
            raw = getattr(best, '_original_plex_track', None)
            return (raw if raw is not None else best), f'fuzzy:{best_score:.2f}'
        return None, 'unmatched'

    # ------------------------------------------------------------------
    # Ratings
    # ------------------------------------------------------------------

    def transfer_ratings(self, *, dry_run: bool = True) -> TransferReport:
        """Copy user ratings from source to destination.

        Only tracks WITH a rating on the source are considered — an unrated
        source track never clears a rating on the destination.
        """
        report = TransferReport(operation='ratings', dry_run=dry_run)

        for src in self.source.get_all_tracks() or []:
            rating = _rating_of(src)
            if rating is None:
                continue                       # never propagate "no rating"
            report.considered += 1

            label = f"{getattr(src, 'title', '?')} — {_artist_of(src)}"
            target, how = self.resolve(src)
            if target is None:
                report.unmatched.append(label)
                continue
            report.matched += 1

            if _rating_of(target) == rating:
                report.skipped_same += 1       # idempotent re-run
                continue

            if dry_run:
                report.written += 1            # what WOULD be written
                continue

            if self.dest.set_track_rating(target, rating):
                report.written += 1
            else:
                report.failures.append(label)

        self.logger.info(report.summary())
        return report

    # ------------------------------------------------------------------
    # Playlists
    # ------------------------------------------------------------------

    def transfer_playlists(self, *, dry_run: bool = True,
                           names: Optional[List[str]] = None) -> TransferReport:
        """Recreate the source's playlists on the destination.

        ``PlaylistInfo`` already carries its tracks, so no extra read per
        playlist. Tracks that do not resolve are reported and excluded rather
        than silently dropped.
        """
        report = TransferReport(operation='playlists', dry_run=dry_run)
        wanted = set(names) if names else None

        for playlist in self.source.get_all_playlists() or []:
            if wanted is not None and playlist.title not in wanted:
                continue

            resolved, missing = [], []
            for track in playlist.tracks or []:
                report.considered += 1
                target, _how = self.resolve(track)
                if target is None:
                    label = f"{getattr(track, 'title', '?')} — {_artist_of(track)}"
                    missing.append(label)
                    report.unmatched.append(f"[{playlist.title}] {label}")
                    continue
                report.matched += 1
                resolved.append(target)

            entry = {
                'name': playlist.title,
                'source_tracks': len(playlist.tracks or []),
                'resolved': len(resolved),
                'missing': missing,
            }
            report.playlists.append(entry)

            if not resolved:
                self.logger.warning(
                    f"Playlist '{playlist.title}': no tracks resolved on the "
                    f"destination — skipping")
                entry['written'] = False
                continue

            if dry_run:
                entry['written'] = False
                report.written += 1            # what WOULD be created
                continue

            try:
                ok = self.dest.create_playlist(playlist.title, resolved)
            except Exception as e:
                ok = False
                report.failures.append(f"{playlist.title}: {e}")
            entry['written'] = bool(ok)
            if ok:
                report.written += 1
            elif f"{playlist.title}" not in report.failures:
                report.failures.append(f"{playlist.title}: create_playlist returned False")

        self.logger.info(report.summary())
        return report


def build_transfer(*, reverse: bool = False) -> Optional[PlexToPlexTransfer]:
    """Build a transfer from configured Plex + Plex-secondary settings.

    ``reverse=True`` swaps direction (secondary becomes the source). Returns
    None when either server is unconfigured, so callers can report that
    plainly instead of failing mid-run.

    Config defaults only apply to FRESH installs — an existing config row never
    gets new keys merged — so the threshold default is supplied here.
    """
    from core.plex_client import PlexClient

    primary_cfg = config_manager.get_plex_config() or {}
    secondary_cfg = config_manager.get_plex_secondary_config() or {}

    for label, cfg in (('Plex', primary_cfg), ('Plex (secondary)', secondary_cfg)):
        if not cfg.get('base_url') or not cfg.get('token'):
            logger.error(f"{label} is not configured (needs base_url and token)")
            return None

    threshold = config_manager.get('plex_secondary.match_threshold',
                                   DEFAULT_MATCH_THRESHOLD) or DEFAULT_MATCH_THRESHOLD

    # None uses the global config; an explicit dict targets the second server.
    primary = PlexClient()
    secondary = PlexClient(config=dict(secondary_cfg))

    source, dest = (secondary, primary) if reverse else (primary, secondary)
    return PlexToPlexTransfer(source, dest, match_threshold=float(threshold))


__all__ = ['PlexToPlexTransfer', 'TransferReport', 'build_transfer',
           'DEFAULT_MATCH_THRESHOLD']
