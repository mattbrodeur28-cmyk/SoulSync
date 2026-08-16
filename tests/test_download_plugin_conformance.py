"""Pin the structural conformance of every download source plugin
class to ``DownloadSourcePlugin``.

Each registered source class MUST:
- Implement every protocol method by name.
- Mark async methods as `async def` so the orchestrator can `await`
  them uniformly.

When someone adds a new source (e.g. Usenet) and forgets one of
these methods, this test fails at the contract — long before the
first real download attempt would have raised AttributeError in
production. When someone CHANGES the contract (adds a method to
the protocol), this test forces every existing source to be
updated.

Catches the smell that motivated the refactor in the first place:
8 sources independently grew the same shape because every
consumer site needed the same calls, but nothing enforced parity.

NOTE on test design: these tests check CLASSES, not instances.
Instantiating real client classes (TidalDownloadClient, etc.) at
fixture setup pollutes module-level state in tidalapi / spotipy
imports and breaks downstream tests that rely on a clean import
graph. Class-level checks are equally strict for structural
conformance — the protocol only constrains the method surface, not
runtime instance behavior.
"""

from __future__ import annotations

import inspect

import pytest


REQUIRED_SYNC_METHODS = {'is_configured'}

REQUIRED_ASYNC_METHODS = {
    'check_connection',
    'search',
    'download',
    'get_all_downloads',
    'get_download_status',
    'cancel_download',
    'clear_all_completed_downloads',
}


def _import_plugin_classes():
    """Import every download source class lazily inside the test
    rather than at module load — avoids dragging tidalapi /
    spotipy / yt-dlp imports into every other test module's
    collection phase."""
    # Music Lite: tidal/qobuz/deezer removed — their client modules were
    # deleted by the provider purge, so importing them here fails collection
    # for the whole module.
    from core.soulseek_client import SoulseekClient
    from core.youtube_client import YouTubeClient
    from core.hifi_client import HiFiClient
    from core.lidarr_download_client import LidarrDownloadClient
    from core.reaparr_client import ReaparrDownloadClient
    from core.soundcloud_client import SoundcloudClient
    from core.amazon_download_client import AmazonDownloadClient

    return {
        'soulseek': SoulseekClient,
        'youtube': YouTubeClient,
        'hifi': HiFiClient,
        'lidarr': LidarrDownloadClient,
        'reaparr': ReaparrDownloadClient,
        'soundcloud': SoundcloudClient,
        'amazon': AmazonDownloadClient,
    }


def test_default_registry_registers_all_sources():
    """Smoke check that the foundation registry knows about every
    source the orchestrator historically dispatched to. If someone
    drops a registration here, every other test in this module would
    silently miss the missing source."""
    from core.download_plugins.registry import build_default_registry

    registry = build_default_registry()
    expected = {
        'soulseek', 'youtube', 'hifi', 'lidarr', 'reaparr',
        'soundcloud', 'amazon', 'torrent', 'usenet',
    }
    assert set(registry.names()) == expected


def test_purged_providers_are_not_registered():
    """Music Lite removed Tidal, Qobuz and Deezer. Pin their absence so a
    later merge from upstream that re-adds a registration fails loudly here
    instead of booting a source whose client module was deleted.

    Replaces the upstream ``test_deezer_dl_alias_is_registered_against_deezer_spec``
    test — the ``deezer_dl`` alias resolved to a spec that no longer exists."""
    from core.download_plugins.registry import build_default_registry

    registry = build_default_registry()
    for purged in ('tidal', 'qobuz', 'deezer', 'deezer_dl'):
        assert registry.get_spec(purged) is None, (
            f"{purged} is registered but its client module was deleted by the "
            f"Music Lite purge"
        )


@pytest.mark.parametrize('plugin_name', [
    'soulseek', 'youtube', 'hifi',
    'lidarr', 'reaparr', 'soundcloud', 'amazon',
])
def test_plugin_class_has_all_required_methods(plugin_name):
    """Every registered plugin class exposes every protocol method
    by name. Diagnostic-friendly: tells you WHICH method is missing
    when a new source is added without all the required methods."""
    classes = _import_plugin_classes()
    cls = classes[plugin_name]

    missing = []
    for method_name in REQUIRED_SYNC_METHODS | REQUIRED_ASYNC_METHODS:
        if not hasattr(cls, method_name):
            missing.append(method_name)
    assert not missing, (
        f"{plugin_name} ({cls.__name__}) missing methods: {missing}"
    )


@pytest.mark.parametrize('plugin_name', [
    'soulseek', 'youtube', 'hifi',
    'lidarr', 'reaparr', 'soundcloud', 'amazon',
])
def test_plugin_class_async_methods_are_coroutines(plugin_name):
    """Methods declared async in the protocol must be async on every
    plugin class. A sync `download()` would silently skip the
    orchestrator's `await` and return a coroutine object instead of
    a download_id — the kind of bug that only surfaces at runtime
    against a live user."""
    classes = _import_plugin_classes()
    cls = classes[plugin_name]

    not_async = []
    for method_name in REQUIRED_ASYNC_METHODS:
        method = getattr(cls, method_name, None)
        if method is None:
            continue
        if not inspect.iscoroutinefunction(method):
            not_async.append(method_name)
    assert not not_async, (
        f"{plugin_name} ({cls.__name__}) declared these methods as "
        f"sync but the protocol requires async: {not_async}"
    )


def test_orchestrator_uses_registry_for_dispatch():
    """The orchestrator must hold a registry reference and the generic
    ``client(name)`` accessor must return the same instances the
    registry holds. Per-source attribute aliases (``orchestrator.soulseek``
    etc.) were removed in favor of ``orchestrator.client('soulseek')``;
    the legacy alias name (``deezer_dl``) still resolves to the canonical
    deezer plugin via the registry's alias map."""
    from core.download_orchestrator import DownloadOrchestrator

    orchestrator = DownloadOrchestrator()
    assert hasattr(orchestrator, 'registry')
    assert orchestrator.client('soulseek') is orchestrator.registry.get('soulseek')
    assert orchestrator.client('youtube') is orchestrator.registry.get('youtube')
    # Music Lite: the deezer_dl alias assertion was dropped with the provider.
    assert orchestrator.client('lidarr') is orchestrator.registry.get('lidarr')
