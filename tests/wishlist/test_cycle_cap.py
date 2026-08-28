"""Per-cycle track cap for scheduled wishlist processing.

A large wishlist used to be submitted as ONE batch.
``process_wishlist_automatically`` returns early while any wishlist batch is
still active, and slskd's shared search budget (35 creations / 220s, ~573/hour)
keeps a 7000-track batch active for roughly 12 hours — so no later cycle runs,
retry backoff never advances, and the albums/singles cycle never toggles.

The cap takes a slice per cycle instead. The slice MUST rotate: the underlying
query is ``ORDER BY date_added``, so a naive head-of-list cap would re-submit
the same oldest tracks forever and never reach the newest ones.
"""

from __future__ import annotations

from core.wishlist.processing import apply_cycle_cap, retry_priority_key


def _t(name, last_attempted=None):
    return {"name": name, "last_attempted": last_attempted}


# ---------------------------------------------------------------------------
# Capping
# ---------------------------------------------------------------------------


def test_cap_limits_the_cycle():
    tracks = [_t(f"s{i}") for i in range(1000)]
    assert len(apply_cycle_cap(tracks, 750)) == 750


def test_cap_is_a_noop_when_under_the_limit():
    tracks = [_t(f"s{i}") for i in range(10)]
    out = apply_cycle_cap(tracks, 750)
    assert out is tracks          # untouched, not re-sorted


def test_zero_or_none_disables_the_cap():
    """0 must preserve pre-cap behaviour for anyone who wants everything."""
    tracks = [_t(f"s{i}") for i in range(1000)]
    for disabled in (0, None, -1, ""):
        assert apply_cycle_cap(tracks, disabled) is tracks


def test_garbage_cap_value_disables_rather_than_crashes():
    """The value comes from user-editable config; a bad one must not take down
    the scheduled cycle."""
    tracks = [_t(f"s{i}") for i in range(50)]
    assert apply_cycle_cap(tracks, "not-a-number") is tracks


# ---------------------------------------------------------------------------
# Rotation — the part that makes a cap safe
# ---------------------------------------------------------------------------


def test_never_attempted_tracks_go_first():
    tracks = [
        _t("tried-recently", "2026-08-21 10:00:00"),
        _t("never-tried"),
        _t("tried-long-ago", "2026-01-01 00:00:00"),
    ]
    picked = [t["name"] for t in apply_cycle_cap(tracks, 2)]
    assert picked[0] == "never-tried"
    assert picked[1] == "tried-long-ago"


def test_least_recently_attempted_wins_among_tried_tracks():
    tracks = [
        _t("newest", "2026-08-21 12:00:00"),
        _t("oldest", "2026-08-01 12:00:00"),
        _t("middle", "2026-08-10 12:00:00"),
    ]
    assert [t["name"] for t in apply_cycle_cap(tracks, 1)] == ["oldest"]


def test_successive_cycles_cover_the_whole_wishlist():
    """The real requirement: with a cap, every track must eventually be tried.
    Simulates cycles, stamping last_attempted on whatever was submitted."""
    tracks = [_t(f"s{i}") for i in range(1000)]
    clock = 0
    seen = set()

    for _cycle in range(4):
        picked = apply_cycle_cap(tracks, 250)
        for t in picked:
            clock += 1
            t["last_attempted"] = f"2026-08-21 {clock // 3600:02d}:{(clock // 60) % 60:02d}:{clock % 60:02d}"
            seen.add(t["name"])

    assert len(seen) == 1000, f"only {len(seen)}/1000 tracks were ever submitted"


def test_capped_cycle_does_not_resubmit_the_same_head_forever():
    """Guards the naive-slice bug directly: cycle 2 must not repeat cycle 1."""
    tracks = [_t(f"s{i}") for i in range(100)]

    first = apply_cycle_cap(tracks, 25)
    for i, t in enumerate(first):
        t["last_attempted"] = f"2026-08-21 00:00:{i:02d}"
    second = apply_cycle_cap(tracks, 25)

    assert not ({t["name"] for t in first} & {t["name"] for t in second})


def test_cap_does_not_mutate_the_input_list():
    tracks = [_t("b", "2026-08-02 00:00:00"), _t("a", "2026-08-01 00:00:00")]
    original = list(tracks)
    apply_cycle_cap(tracks, 1)
    assert tracks == original


def test_retry_priority_key_orders_none_before_timestamps():
    assert retry_priority_key(_t("x")) < retry_priority_key(_t("y", "2020-01-01 00:00:00"))
