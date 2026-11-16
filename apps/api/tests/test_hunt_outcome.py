"""Fin d'une chasse sans candidat valable : l'album dit pourquoi et quand ça réessaie."""

from fakes import TRACKS
from test_flow import ctx  # noqa: F401

from soulsick.scoring import PeerResult, RemoteFile
from soulsick.services.hunter import RETRY_HOURS


async def _wanted_album(client):
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": "validation", "primary_types": ["album"],
        "monitor": "all",
    })
    aid = r.json()["id"]
    return next(rg["id"] for rg in (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
                if rg["title"] == "Nevermind")


async def test_no_valid_candidate_says_why_and_when(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _wanted_album(client)
    # A lossy copy (refused by the Lossless profile) and a folder with a single track
    mp3 = [
        RemoteFile(f"@@music\\Nevermind\\{i + 1:02d} - {t}.mp3", size=100, bitrate=128, duration=ln // 1000)
        for i, (t, ln) in enumerate(TRACKS)
    ]
    (title, length), *_ = TRACKS
    half = [RemoteFile(f"@@music\\Nevermind\\01 - {title}.flac", bit_depth=16, duration=length // 1000)]
    engine.results = [
        PeerResult("lossypeer", True, 1_500_000, 0, mp3),
        PeerResult("halfpeer", True, 1_500_000, 0, half),
    ]

    await core.hunter.hunt(rid)

    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "wanted"
    reason = album["status_reason"]
    assert "format not allowed ×1" in reason, reason
    assert "missing tracks ×1" in reason, reason
    assert f"{RETRY_HOURS} h" in reason, reason
    # Same sentence for the web UI, by key
    t = album["status_reason_t"]
    assert t["key"] == "reason.next_hunt" and t["params"]["hours"] == RETRY_HOURS
    why = t["params"]["reason"]
    assert why["key"] == "reason.no_valid_why" and why["params"]["n"] == 2
    kinds = [x["key"] for x in why["params"]["rejections"] if isinstance(x, dict)]
    assert sorted(kinds) == ["rejections.format_refused", "rejections.missing_tracks"]


async def test_nothing_found_still_says_when(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _wanted_album(client)
    engine.results = []

    await core.hunter.hunt(rid)

    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "wanted"
    assert album["status_reason"] == (
        "No valid candidate among 0 folders. "
        f"Next automatic search no sooner than {RETRY_HOURS} h after this one"
    )
