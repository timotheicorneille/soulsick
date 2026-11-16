"""A preferred size per album, not a limit: a folder heavier than what you prefer goes behind a
lighter one of the same album, and is still taken when it is the only one."""

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


async def _prefer(client, mb: int) -> None:
    settings = (await client.get("/api/settings")).json()
    settings["hunter"]["preferred_album_mb"] = mb
    assert (await client.put("/api/settings", json=settings)).status_code == 200


async def test_without_a_preference_the_heavier_format_wins(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "validation")
    engine.results = [peer_with_album(), _hires()]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    assert cands[0]["username"] == "hirespeer" and "size" not in cands[0]["score_detail"]


async def test_a_heavier_folder_goes_behind_and_says_why(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "validation")
    await _prefer(client, 250)
    engine.results = [peer_with_album(), _hires()]
    await core.hunter.hunt(rid)
    cands = (await client.get(f"/api/albums/{rid}")).json()["candidates"]
    order = [(c["username"], c["status"]) for c in cands]
    assert order == [("goodpeer", "proposed"), ("hirespeer", "proposed")]
    assert "size" not in cands[0]["score_detail"]
    assert cands[1]["score_detail"]["size"] == {"points": -40.0, "mb": 100 * len(TRACKS), "preferred": 250}
    assert cands[1]["score"] < cands[0]["score"]


async def test_alone_the_heavier_folder_is_still_taken(ctx):  # noqa: F811
    client, core, engine, env = ctx
    rid = await _nevermind(client, "auto")
    await _prefer(client, 250)
    engine.results = [_hires()]
    await core.hunter.hunt(rid)
    # A preference never leaves an album without a source
    assert {d[0] for d in engine.downloads} == {"hirespeer"}
