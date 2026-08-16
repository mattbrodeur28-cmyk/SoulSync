# AGENTS.md — SoulSync Music Lite fork

Orientation for AI assistants working in this repo. Read this before touching
anything.

This is a **fork** of `Nezreka/SoulSync`, branch `feature/music-lite`.
Upstream base: `c857a3f9` (v3.2.0, 2026-08-12).

---

## Ground rules

1. **Read before writing.** This codebase is large and was substantially
   AI-generated. Patterns are inconsistent between modules. Do not assume a
   convention holds — grep for it.
2. **Small, focused diffs.** One concern per change. If a change touches more
   than a few files, stop and write a plan first.
3. **Never auto-commit.** Propose the change; the human commits.
4. **Never delete files** without explicit approval.
5. **State uncertainty.** If you have not verified something by reading the
   code or running it, say so. Do not present inference as fact.
6. **The tests are the gate, not the build.** A clean `ruff` run and a booting
   container prove very little here. See "Testing" below.

---

## What this fork changed

Music Lite strips providers and the entire video subsystem from upstream.
**49 files, +622 / −19,225.**

**Removed:** Tidal, Qobuz, Deezer, Discogs, ListenBrainz, Video backend + UI,
Chat.

**Removal technique:** rather than editing 70+ call sites, the removed clients'
accessor functions were kept and made to return `None` as compatibility shims:

```python
def get_deezer_client(client_factory=None):
    return None  # Music Lite compatibility shim
```

**Implication:** callers must null-check. Most do (`deezer_cl and hasattr(...)`).
When adding code near a removed provider, assume the accessor returns `None`
and guard accordingly. Do not "restore" a provider by re-adding an import —
the client module is deleted.

**Download sources: 11 → 8.**
Remaining: `amazon, soulseek, youtube, hifi, lidarr, soundcloud, torrent, usenet`.
Plus `reaparr` (9), added by this fork — see "Current work".

---

## Environment — non-negotiable

**Python 3.11.** Not 3.12, not 3.14.

- `Dockerfile` builds on `python:3.11-slim`
- `pyproject.toml` sets `target-version = "py311"`
- Upstream CI pins `python-version: "3.11"`

macOS Homebrew may default `python3` to a much newer version. Using it will
either fail to install dependencies or produce test results that do not match
what the container actually runs.

```bash
/opt/homebrew/bin/python3.11 -m venv ~/.venvs/soulsync
source ~/.venvs/soulsync/bin/activate
python --version          # must print 3.11.x
pip install -r requirements.txt
pip install ruff pytest
```

The venv lives **outside** the repo by convention. (`.gitignore` line 42 is
`**/.*/`, which already ignores every dotted directory — `.venv/`,
`.pytest_cache/`, `.ruff_cache/`, and also `.vscode/`.)

Every new shell needs `source ~/.venvs/soulsync/bin/activate` first.

---

## Layout

| Path | What |
|---|---|
| `web_server.py` | **40,038 lines.** Flask monolith: routes, globals, worker wiring. Grep, never read whole. |
| `api/` | Blueprint modules split out of the monolith |
| `core/` | ~200 modules — clients, workers, engines |
| `core/download_plugins/` | **Download source plugin system. See below.** |
| `core/download_orchestrator.py` | 655 lines. Routes downloads across sources via the registry. |
| `webui/` | React SPA (`webui/`) + legacy vanilla JS (`webui/static/`) |
| `tests/` | 520 files. **Currently broken — see Testing.** |
| `docs/` | Design notes, refactor plans |

**Stack:** Python 3.11, Flask, SQLite (WAL), React + vanilla JS SPA.

---

## Download plugin system — the important part

This is the extension point. It is well-designed and **must not be worked
around**.

**`core/download_plugins/base.py`** — `DownloadSourcePlugin`, a
`@runtime_checkable` Protocol. Structural typing: no inheritance, no base
class. Implement the methods and you are a source.

| Method | Kind | Returns |
|---|---|---|
| `is_configured()` | sync | `bool` |
| `check_connection()` | async | `bool` |
| `search(query, timeout, progress_callback)` | async | `(List[TrackResult], List[AlbumResult])` |
| `download(username, filename, file_size)` | async | `download_id: str \| None` |
| `get_all_downloads()` | async | `List[DownloadStatus]` |
| `get_download_status(download_id)` | async | `DownloadStatus \| None` |
| `cancel_download(download_id, username, remove)` | async | `bool` |
| `clear_all_completed_downloads()` | async | `bool` |

`username` and `filename` are **source-specific and opaque to the
orchestrator**. Streaming sources pass a source-name string as `username`;
Soulseek passes a peer username. Encode whatever your source needs.

**`core/download_plugins/registry.py`** — adding a source is one line:

```python
registry.register(PluginSpec(name='foo', factory=FooClient, display_name='Foo'))
```

The registry docstring is explicit that this is the intended path: *"One
`register()` call adds a source to every dispatch path."*

**`core/download_plugins/types.py`** — canonical `TrackResult`, `AlbumResult`,
`DownloadStatus`. Use these; do not invent parallel shapes.

**Reference implementations**, closest first for an external self-hosted
service: `core/lidarr_download_client.py` (718 lines),
`core/soundcloud_client.py` (707), `core/hifi_client.py` (1279).

**All async methods must be `async def`.** A sync `download()` silently returns
a coroutine object instead of a `download_id` and only fails against a live
user. The conformance test exists to catch exactly this.

---

## Testing

```bash
ruff check .                                          # CI gate, must pass
python -m pytest tests/test_download_plugin_conformance.py -q   # 15 passed
python -m pytest tests/test_torrent_usenet_plugins.py -q        # 51 passed
```

### Known broken — read this before trusting a green run

**The full suite does not run.** `python -m pytest` aborts with **39 collection
errors**. Roughly **50 test files** still import provider modules deleted by the
Music Lite purge (`core.qobuz_client`, `core.tidal_download_client`,
`core.deezer_download_client`, and others).

Bundle ML-1.0 repaired `tests/test_download_plugin_conformance.py` only,
because that is the gate enforcing the plugin contract. The rest is
outstanding.

Consequence: **this fork currently has no working regression safety net.** Do
not claim a change is verified because the container booted.

### CI cannot catch this

`.github/workflows/music-lite-docker.yml` runs:

- `python3 -m py_compile web_server.py`
- `python3 -m ruff check web_server.py --select F821`
- `node --check webui/static/init.js`

`F821` finds undefined *names*. It cannot know an imported module no longer
exists on disk. Upstream's `build-and-test.yml` runs `python -m pytest` and
would have caught the purge breakage immediately.

**If you add a test, run it. Do not assume CI will.**

---

## Current work

Adding **Reaparr** as a download source. Reaparr pulls media from Plex servers
the user already has shared access to — a capability no existing source has.

Division of responsibility:

- **SoulSync** decides *what is missing* (matching engine, SoulID identity,
  Duplicates maintenance job) and handles everything post-download (AcoustID
  verification, MusicBrainz tagging, organization, Plex sync).
- **Reaparr** answers *"can I get this, and fetch it"*.

Deduplication is therefore SoulSync's job, not Reaparr's, and happens **before**
transfer.

**SoulSync side: done.** `core/reaparr_client.py` implements
`DownloadSourcePlugin`, one `registry.register()` line, settings panel, and 29
unit tests. Conformance gate is 17 (was 15).

**Reaparr side: blocked, and not the API originally assumed here.** Reaparr does
not expose a bespoke search/download/status/cancel API. Its `src/PublicAPI`
implements two *standard* protocols:

- a **Torznab indexer** (`/api/public/indexer/api`)
- a **qBittorrent WebUI emulation** (`/api/public/download-client/api/v2`)

Reaparr cannot serve music today. Verified in source:

- `TorznabEndpointRequestValidator` allows only `caps|search|tvsearch|movie` —
  Torznab's music type is `music`, so it is rejected.
- `TorznabEndpoint.cs` — `case "search": throw new NotImplementedException();`
- `GetCapabilitiesCommandHandler` advertises Movies + TV categories only; no
  Audio 3000-series, so Prowlarr would never route music to it.
- `TorrentMetadataDTOValidator` — `Type` must be Episode or Movie, `Quality` is
  a `VideoQuality`. The transfer path rejects music too.

`PlexMediaType.Music = 5` exists in the domain; Reaparr's own
`plans/plan-001` calls music "future".

The contract SoulSync is written against is `docs/reaparr-api-contract.md`. Its
key property: `download_token` is **opaque** to SoulSync, so Reaparr can change
`TorrentMetadataDTO` freely without breaking this side.

Until Reaparr ships music, this source returns no results at runtime. It has
never been exercised against a live server — the tests use a mock transport.
Do not describe it as working.

Reaparr is C# .NET 10 — a separate repo at `/Users/matt/Reaparr-v2`.

---

## Gotchas

- `web_server.py` has module-level globals set to `None` for removed providers
  (`deezer_worker`, `discogs_worker`, `tidal_enrichment_worker`). Guard before
  use.
- `core/metadata/registry.py` `METADATA_SOURCE_PRIORITY` no longer lists deezer
  or discogs. Keep it consistent with what actually exists.
- Live/network tests are opt-in via markers and filtered out by default in
  `pyproject.toml`. `pytest -m soundcloud_live` runs them.
- Merging from upstream will reintroduce purged providers. The
  `test_purged_providers_are_not_registered` test exists to fail loudly when
  that happens — do not delete it to make a merge green.
- `.gitignore` ends with `**/.*/`, a catch-all for hidden directories. It
  silently ignores `.vscode/` too, so editor config is untracked unless a
  negation is added. Keep virtualenvs outside the repo regardless.
