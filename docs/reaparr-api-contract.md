# Reaparr ↔ SoulSync music contract

Status: **proposed — not yet implemented on the Reaparr side.**

`core/reaparr_client.py` is written against this document. Until Reaparr ships
the endpoints marked NEW, the plugin is unit-tested against a mock and returns
empty results at runtime. Nothing here has been exercised against a live server.

Reaparr base URL is user-configured; all paths below are relative to it.

---

## What Reaparr already has (verified by reading the C# source)

`src/PublicAPI/_Shared/Config/FastEndpoints/PublicApiRoutes.cs`:

| Constant | Value |
|---|---|
| `Base` | `/api/public` |
| `Indexer` | `/api/public/indexer/api` |
| `DownloadClient` | `/api/public/download-client/api/v2` |

Existing endpoints, all real today:

| Method | Route | Purpose |
|---|---|---|
| GET | `{Indexer}?t=caps` | Torznab capabilities |
| GET | `{Indexer}?t=tvsearch\|movie` | Torznab search |
| POST | `{DownloadClient}/auth/login` | qBittorrent-style login → `SID` cookie |
| POST | `{DownloadClient}/auth/logout` | |
| GET | `{DownloadClient}/torrents/download` | Returns a `.torrent` for a media part |
| POST | `{DownloadClient}/torrents/add` | Accepts a `.torrent` upload |
| GET | `{DownloadClient}/torrents/info` | qBittorrent-shaped status list |
| GET | `{DownloadClient}/torrents/properties` | |
| GET | `{DownloadClient}/torrents/files` | |
| POST | `{DownloadClient}/torrents/delete` | |
| GET | `{DownloadClient}/torrents/categories` | |
| POST | `{DownloadClient}/torrents/createCategory` | |
| GET | `{DownloadClient}/app/version`, `/app/webapiVersion`, `/app/preferences` | |

### Authentication — two separate schemes

- **Indexer endpoints**: `apikey` query parameter, compared against
  `IIntegrationsSettings.ReaparrApiKey`
  (`IndexerAuthenticationPreProcessor.cs`).
- **Download-client endpoints**: `SID` cookie issued by `/auth/login`
  (`DownloadClientAuthenticationPreProcessor.cs`).

The SoulSync client holds both: an API key for search, and a session cookie for
transfer.

---

## Why music does not work today

Four independent blockers, each verified in source:

1. `TorznabEndpointRequestValidator` accepts only `caps`, `search`, `tvsearch`,
   `movie`. Torznab's music search type is `music` → rejected as invalid.
2. `TorznabEndpoint.cs` — `case "search": throw new NotImplementedException();`
   The generic search that would otherwise serve music throws.
3. `GetCapabilitiesCommandHandler` advertises Movies (2000s) and TV (5000s)
   categories only. No Audio 3000-series, despite `TorznabCategoryId` defining
   `Audio = 3000`, `Audio_MP3`, `Audio_Lossless`.
4. `TorrentMetadataDTOValidator` — `Type` must be `Episode` or `Movie`, and
   `Quality` is a **`VideoQuality`** enum. The transfer path rejects music even
   if search returned something.

`PlexMediaType.Music = 5` does exist in the domain model, and Reaparr's
`plans/plan-001-library-comparison-badges.md` refers to music as future work
throughout.

---

## NEW — music search

SoulSync does not use Torznab for music. Torznab's music semantics are weak
(no reliable track-level identity) and SoulSync needs Plex identity fields to
deduplicate *before* transfer. A dedicated JSON endpoint is cheaper for both
sides than bending Torznab.

```
GET {Base}/music/search
```

Query parameters:

| Param | Type | Notes |
|---|---|---|
| `apikey` | string | required; same key as the indexer scheme |
| `q` | string | free-text; used when artist/album/track are absent |
| `artist` | string | optional |
| `album` | string | optional |
| `track` | string | optional |
| `limit` | int | default 50, max 200 |
| `offset` | int | default 0 |

Response `200 application/json`:

```json
{
  "results": [
    {
      "download_token": "Type=Music&MediaId=12&DataId=34&PartId=56&PlexApiPartId=78&Quality=Lossless&LibraryId=2&ServerId=1",
      "artist": "Radiohead",
      "album": "In Rainbows",
      "title": "Nude",
      "track_number": 3,
      "disc_number": 1,
      "year": "2007",
      "duration_ms": 255000,
      "size_bytes": 41234567,
      "format": "flac",
      "bitrate_kbps": 1008,
      "sample_rate_hz": 44100,
      "bit_depth": 16,
      "identity": {
        "server_id": 1,
        "server_name": "Friend's Plex",
        "library_id": 2,
        "plex_rating_key": 91234,
        "plex_guid": "plex://track/5d07c...",
        "musicbrainz_track_id": null
      }
    }
  ],
  "total": 1
}
```

**`download_token` is opaque to SoulSync.** It is stored verbatim and handed
back as the query string of `/torrents/download`. Reaparr may change its
internal shape freely; SoulSync never parses it. This is the single most
important property of this contract — it keeps `TorrentMetadataDTO` a Reaparr
implementation detail.

`identity` is what SoulSync's matching engine uses to decide whether it already
has the track. Per the division of responsibility, **deduplication is SoulSync's
job and happens before transfer** — Reaparr should return everything it can see,
including things SoulSync may already own.

Unknown numeric fields must be `null`, not `0`. `null` means "unknown";
`0` means "genuinely zero" and will be treated as such by quality ranking.

---

## CHANGED — transfer path accepts music

No new endpoints. Two constraints relax:

1. `TorrentMetadataDTOValidator` must accept `PlexMediaType.Music`.
2. `Quality` must accept an audio quality. Either widen to a shared enum or add
   an `AudioQuality` alongside `VideoQuality`. SoulSync sends back only what it
   received in `download_token`, so the concrete choice is Reaparr's.

SoulSync's transfer sequence, using endpoints that already exist:

```
POST {DownloadClient}/auth/login        → SID cookie
GET  {DownloadClient}/torrents/download?<download_token>   → .torrent bytes
POST {DownloadClient}/torrents/add      → multipart: torrents=<bytes>, category=soulsync
GET  {DownloadClient}/torrents/info?category=soulsync      → poll until progress == 1.0
POST {DownloadClient}/torrents/delete   → hashes=<hash>, deleteFiles=false
```

`/torrents/info` is already qBittorrent-shaped: `hash`, `name`, `size`,
`progress` (0.0–1.0), `dlspeed`, `state`, `content_path`, `save_path`.
SoulSync maps these straight onto `DownloadStatus`.

### Matching an add to its hash

`/torrents/add` returns qBittorrent's bare `Ok.` body, with no hash. SoulSync
resolves the hash by diffing `/torrents/info?category=` before and after the
add. This works but is racy under concurrent adds.

**Requested improvement:** have `/torrents/add` return
`{"hash": "<sha1>"}` when the request carries an
`X-Reaparr-Client: soulsync` header. Strictly additive — *arr clients keep the
qBittorrent-compatible `Ok.` body. Until then SoulSync serializes adds behind a
lock, which caps throughput at one concurrent add.

---

## File handoff

`/torrents/info` reports `content_path` from Reaparr's filesystem view. When
SoulSync runs in a different container, that path is not directly readable.

SoulSync reuses the existing remote-path resolution rather than inventing a
Reaparr-specific one — `resolve_reported_save_path` in
`core/download_plugins/album_bundle.py`, the same helper the torrent and usenet
plugins use, honouring `download_source.path_mappings`.

The all-on-one-box and shared-volume cases work with no configuration. Split
deployments need a path mapping, exactly as they already do for qBittorrent.

---

## Open questions

1. **Audio quality selection.** If a Plex part is available in multiple
   qualities, does `download_token` pin one, or does SoulSync choose? Current
   assumption: the token pins it, and Reaparr returns one result per
   distinct quality.
2. **Rate limiting.** Searching across many shared Plex servers may be slow.
   Should `/music/search` stream or paginate server-by-server? Current
   assumption: single synchronous response inside SoulSync's search timeout.
3. **Availability vs. presence.** Does a result guarantee the part is currently
   fetchable, or only that it was seen in a library? Affects whether SoulSync
   should treat a failed transfer as permanent or retryable.
