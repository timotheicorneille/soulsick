"""A size limit per album: a folder heavier than what you want is rejected, a lighter one of the
same album is taken instead, and the heavy one stays yours to take."""

from fakes import TRACKS, peer_with_album
from test_flow import ctx, wait_for  # noqa: F401

from soulsick.scoring import PeerResult, RemoteFile


def _hires(user: str = "hirespeer") -> PeerResult:
    """The same album in 24-bit: the profile prefers it, and it weighs 100 MB a track."""
    files = [
        RemoteFile(
            f"@@hires\\Nirvana\\Nevermind [24-96]\\{i + 1:02d} - {t}.flac",
            size=100_000_000, bit_depth=24, duration=ln // 1000,
        )
        for i, (t, ln) in enumerate(TRACKS)
    ]
    return PeerResult(user, True, 1_500_000, 0, files)


async def _nevermind(client, mode: str) -> int:
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": mode, "primary_types": ["album"],
        "monitor": "none",
    })
    albums = (await client.get(f"/api/artists/{r.json()['id']}")).json()["release_groups"]
    return next(rg["id"] for rg in albums if rg["title"] == "Nevermind")


async def _limit(client, mb: int) -> None:
    settings = (await client.get("/api/settings")).json()
    settings["hunter"]["max_album_mb"] = mb
    assert (await client.put("/api/settings", json=settings)).status_code == 200


async def test_without_a_limit_the_heavier_format_wins(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "validation")
    engine.results = [peer_with_album(), _hires()]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    assert cands[0]["username"] == "hirespeer" and cands[0]["status"] == "proposed"


async def test_a_folder_over_the_limit_is_rejected_and_says_why(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "validation")
    await _limit(client, 250)
    engine.results = [peer_with_album(), _hires()]
    await core.hunter.hunt(rid)
    cands = {c["username"]: c for c in (await client.get(f"/api/albums/{rid}")).json()["candidates"]}
    assert cands["goodpeer"]["status"] == "proposed"
    heavy = cands["hirespeer"]
    assert heavy["status"] == "rejected"
    assert heavy["reject_reason_t"] == {
        "key": "reject.too_big", "params": {"size": 100 * len(TRACKS), "max": 250},
    }


async def test_only_heavy_folders_nothing_is_downloaded_on_its_own(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "auto")
    await _limit(client, 250)
    engine.results = [_hires()]
    await core.hunter.hunt(rid)
    assert engine.downloads == []
    assert (await client.get(f"/api/albums/{rid}")).json()["status"] == "wanted"

