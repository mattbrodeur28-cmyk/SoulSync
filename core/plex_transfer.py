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
    # Which server/library each side actually bound to. A transfer that reads
    # the wrong library reports zeros exactly like one that read nothing, so
    # the run says out loud where it looked.
    source_server: str = ''
    dest_server: str = ''

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
            "source_server": self.source_server,
            "dest_server": self.dest_server,
            "summary": self.summary(),
        }


def _artist_of(track: Any) -> str:
    """Artist name from either a TrackInfo or a raw plexapi Track."""
    artist = getattr(track, 'artist', None)
    if callable(artist):                      # raw plexapi Track.artist()
        # grandparentTitle rides along on the library listing already in hand.
        # ``artist()`` is a fetchItem — one HTTP round trip PER TRACK, which on
        # a real library turns a sweep into thousands of requests.
        cheap = getattr(track, 'grandparentTitle', '') or ''
        if cheap:
            return cheap
        try:
            got = artist()
            return getattr(got, 'title', '') or ''
        except Exception:
            return ''
    if isinstance(artist, str):
        return artist
    return getattr(track, 'grandparentTitle', '') or ''


def _rating_of(track: Any) -> Optional[float]:
    """The USER's rating (stars), from a TrackInfo or a raw plexapi Track.

    A raw plexapi ``Track`` carries BOTH ``userRating`` (what the user set,
    0-10) and ``rating`` (the agent/critic rating). Only the first may travel.
    So: when the object exposes ``userRating`` at all it is the only field
    consulted — falling through to ``rating`` there would copy a critic score
    onto the destination as if the user had set it, and would make an unrated
    track look rated. ``TrackInfo`` has no ``userRating``; its ``rating`` field
    is already populated from ``track.userRating``.
    """
    if hasattr(track, 'userRating'):
        value = track.userRating
    else:
        value = getattr(track, 'rating', None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class PlexToPlexTransfer:
    """Transfers playlists and ratings from ``source`` to ``dest``."""

    def __init__(self, source, dest, *, match_threshold: float = DEFAULT_MATCH_THRESHOLD,
                 matching_engine=None, logger=logger, on_progress=None):
        self.source = source
        self.dest = dest
        self.match_threshold = match_threshold
        self.matcher = matching_engine or MusicMatchingEngine()
        self.logger = logger
        # Optional ``fn(str)`` phase callback. A full two-server sweep runs for
        # minutes, so the caller needs something to show that isn't a dead
        # spinner. Never allowed to break the run.
        self._on_progress = on_progress
        self._guid_index: Optional[Dict[str, Any]] = None

    def _progress(self, message: str) -> None:
        if not self._on_progress:
            return
        try:
            self._on_progress(message)
        except Exception as e:
            self.logger.debug(f"progress callback failed: {e}")

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @staticmethod
    def _describe(client) -> str:
        """"<friendlyName> / <library>" for a PlexClient, best effort.

        Both PlexClients read the SAME saved ``plex_music_library`` preference,
        so the second server binds to a same-named library if it has one and
        otherwise falls back to its first music section — which may not be the
        one the user meant. Naming it in the report makes that visible instead
        of leaving a wrong-library run looking like an empty one.
        """
        try:
            server = getattr(client, 'server', None)
            name = getattr(server, 'friendlyName', '') or '?'
            if getattr(client, '_all_libraries_mode', False):
                return f"{name} / All Libraries"
            library = getattr(client, 'music_library', None)
            return f"{name} / {getattr(library, 'title', '?')}"
        except Exception:
            return '?'

    def _stamp(self, report: 'TransferReport') -> None:
        report.source_server = self._describe(self.source)
        report.dest_server = self._describe(self.dest)

    # ------------------------------------------------------------------
    # Destination index
    # ------------------------------------------------------------------

    def _ensure_dest_index(self) -> Dict[str, Any]:
        """guid -> raw destination Plex track, from a single library sweep."""
        if self._guid_index is not None:
            return self._guid_index

        self._progress("Indexing the destination library…")
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

        self._progress("Reading the source library…")
        source_tracks = self.source.get_all_tracks() or []
        total = len(source_tracks)
        self._progress(f"Read {total} source track(s)")

        for seen, src in enumerate(source_tracks, 1):
            rating = _rating_of(src)
            if rating is None:
                continue                       # never propagate "no rating"
            report.considered += 1
            if report.considered % 25 == 0:
                self._progress(f"Ratings: {seen}/{total} scanned, "
                               f"{report.considered} rated, {report.matched} matched")

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

        self._stamp(report)
        self.logger.info(
            f"{report.summary()} [{report.source_server} -> {report.dest_server}]")
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

        self._progress("Reading the source playlists…")
        source_playlists = self.source.get_all_playlists() or []

        for position, playlist in enumerate(source_playlists, 1):
            if wanted is not None and playlist.title not in wanted:
                continue
            self._progress(f"Playlist {position}/{len(source_playlists)}: "
                           f"{playlist.title}")

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

        self._stamp(report)
        self.logger.info(
            f"{report.summary()} [{report.source_server} -> {report.dest_server}]")
        return report


class TransferUnavailable(RuntimeError):
    """A transfer can't start — unconfigured or unreachable server.

    Raised instead of returning None so the caller can show the user WHICH
    server is the problem. Silently handing back an empty transfer is the worst
    outcome here: it produces a report full of zeros that looks identical to a
    correct run over an empty library.
    """


def build_transfer(*, reverse: bool = False, on_progress=None) -> PlexToPlexTransfer:
    """Build a transfer from configured Plex + Plex-secondary settings.

    ``reverse=True`` swaps direction (secondary becomes the source). Raises
    :class:`TransferUnavailable` when a server is unconfigured or can't be
    reached, so the failure is reported before anything is swept.

    Config defaults only apply to FRESH installs — an existing config row never
    gets new keys merged — so the threshold default is supplied here.
    """
    from core.plex_client import PlexClient

    primary_cfg = config_manager.get_plex_config() or {}
    secondary_cfg = config_manager.get_plex_secondary_config() or {}

    for label, cfg in (('Plex', primary_cfg), ('Plex (secondary)', secondary_cfg)):
        if not cfg.get('base_url') or not cfg.get('token'):
            raise TransferUnavailable(
                f"{label} is not configured (needs a URL and a token).")

    threshold = config_manager.get('plex_secondary.match_threshold',
                                   DEFAULT_MATCH_THRESHOLD) or DEFAULT_MATCH_THRESHOLD

    # None uses the global config; an explicit dict targets the second server.
    primary = PlexClient()
    secondary = PlexClient(config=dict(secondary_cfg))

    # Connect BOTH before returning. These are freshly constructed clients, not
    # the app's long-lived one, so nothing has dialed them yet; an unconnected
    # client's reads return empty rather than raising, which is what makes a
    # broken transfer look like a successful no-op.
    for label, client in (('Plex', primary), ('Plex (secondary)', secondary)):
        if not client.is_connected():
            raise TransferUnavailable(
                f"Could not connect to {label}. Check its URL and token.")
        if not client.is_fully_configured():
            raise TransferUnavailable(
                f"{label} connected but has no music library selected.")

    source, dest = (secondary, primary) if reverse else (primary, secondary)
    transfer = PlexToPlexTransfer(source, dest, match_threshold=float(threshold),
                                  on_progress=on_progress)
    logger.info(f"Transfer ready: {transfer._describe(source)} -> "
                f"{transfer._describe(dest)}")
    return transfer


__all__ = ['PlexToPlexTransfer', 'TransferReport', 'TransferUnavailable',
           'build_transfer', 'DEFAULT_MATCH_THRESHOLD']
