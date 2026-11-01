"""Parcours complet : compte admin → artiste → chasse → validation → téléchargement → import."""

import asyncio
import os

import httpx
import pytest
from fakes import FakeEngine, FakeMB, peer_with_album
from httpx import ASGITransport, AsyncClient

from soulsick.app import create_app
from soulsick.audiotags import read_meta
from soulsick.config import Env
from soulsick.core import Core


@pytest.fixture
async def ctx(tmp_path):
    env = Env(
        config_dir=tmp_path / "config", music_dir=tmp_path / "music", downloads_dir=tmp_path / "downloads",
    )
    engine = FakeEngine(str(env.downloads_dir))
    core = Core(env, mb=FakeMB(env.covers_dir), engine=engine)
    # No network in tests: ListenBrainz, Deezer and LRCLIB know nothing unless a test says otherwise
    offline = httpx.MockTransport(lambda request: httpx.Response(404))
    core.discover.client = httpx.AsyncClient(transport=offline)
    core.lyrics.client = httpx.AsyncClient(transport=offline)
    app = create_app(env, core)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, core, engine, env


async def wait_for(pred, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if await pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition jamais atteinte")


async def test_auth_required(ctx):
    client, *_ = ctx
    assert (await client.get("/api/status")).status_code == 401
    st = (await client.get("/api/auth/state")).json()
    assert st["setup_required"] is True
    r = await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    assert r.status_code == 200
    assert (await client.get("/api/status")).status_code == 200
    # Un second setup est refusé
    assert (await client.post("/api/auth/setup", json={"username": "x", "password": "motdepasse"})).status_code == 409


async def test_full_flow(ctx):
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})

    profiles = (await client.get("/api/profiles")).json()
    assert profiles[0]["name"] == "Lossless"

    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": "validation",
        "quality_profile_id": profiles[0]["id"], "primary_types": ["album"], "monitor": "all",
    })
    assert r.status_code == 200, r.text
    aid = r.json()["id"]

    artist = (await client.get(f"/api/artists/{aid}")).json()
    by_title = {rg["title"]: rg for rg in artist["release_groups"]}
    assert by_title["Nevermind"]["status"] == "wanted"
    assert by_title["MTV Unplugged"]["status"] == "ignored"  # live non suivi
    rid = by_title["Nevermind"]["id"]

    engine.results = [peer_with_album()]
    await core.hunter.hunt(rid)

    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "pending_approval"
    best = album["candidates"][0]
    assert best["status"] == "proposed" and best["format"] == "flac"
    assert best["score_detail"]["match"]["tracks_matched"] == 3

    r = await client.post(f"/api/candidates/{best['id']}/approve")
    assert r.status_code == 200

    async def imported():
        a = (await client.get(f"/api/albums/{rid}")).json()
        return a["status"] == "imported"

    await wait_for(imported)
    dest = env.music_dir / "Nirvana" / "1991 - Nevermind" / "01 - Smells Like Teen Spirit.flac"
    assert dest.exists()
    meta = read_meta(dest)
    assert meta.release_group_mbid == "1b022e01-4da6-387b-8658-8678046e4cef"
    assert meta.title == "Smells Like Teen Spirit" and meta.track == 1
    # Le dossier de téléchargement temporaire est nettoyé
    assert not any(os.scandir(env.downloads_dir / f"album-{rid}"))

    kinds = [e["kind"] for e in (await client.get("/api/events", params={"release_group_id": rid})).json()]
    for k in ("hunt.started", "hunt.scored", "hunt.awaiting_approval", "download.started",
              "download.complete", "import.done"):
        assert k in kinds, k


async def test_auto_mode_downloads_without_approval(ctx):
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={
        "mbid": "5b11f4ce-a62d-471e-81fc-a69a8278c7da", "mode": "auto", "primary_types": ["album"], "monitor": "all",
    })
    aid = r.json()["id"]
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = [peer_with_album()]
    await core.hunter.hunt(rid)
    assert len(engine.downloads) >= 3

    async def imported():
        return (await client.get(f"/api/albums/{rid}")).json()["status"] == "imported"

    await wait_for(imported)


async def test_no_candidate_keeps_wanted_with_reason(ctx):
    client, core, engine, env = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    r = await client.post("/api/artists", json={"mbid": "x", "mode": "auto", "primary_types": ["album"], "monitor": "all"})
    aid = r.json()["id"]
    rid = next(rg["id"] for rg in (await client.get(f"/api/artists/{aid}")).json()["release_groups"]
               if rg["title"] == "Nevermind")
    engine.results = []
    await core.hunter.hunt(rid)
    album = (await client.get(f"/api/albums/{rid}")).json()
    assert album["status"] == "wanted"
    assert album["status_reason"].startswith("No valid candidate")
    assert album["status_reason_t"] == {"key": "reason.no_valid", "params": {"n": 0}}


async def test_share_rules_preview(ctx, tmp_path):
    client, core, *_ = ctx
    await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
    share = tmp_path / "share"
    (share / "a").mkdir(parents=True)
    (share / "a" / "x.flac").write_bytes(b"0")
    (share / "a" / "y.exe").write_bytes(b"0")
    (share / "a" / "secret.flac").write_bytes(b"0")
    r = await client.post("/api/shares", json={"path": str(share), "exclude_patterns": ["secret*"]})
    assert r.status_code == 200, r.text
    prev = (await client.get(f"/api/shares/{r.json()['id']}/preview")).json()
    assert prev["included"] == 1 and prev["excluded"] == 2
    reasons = {s["path"]: s["reason"] for s in prev["excluded_samples"]}
    assert "not allowed" in reasons[os.path.join("a", "y.exe")]
    keys = {s["path"]: s["reason_t"]["key"] for s in prev["excluded_samples"]}
    assert keys[os.path.join("a", "y.exe")] == "share.ext_not_allowed"
    assert "secret*" in reasons[os.path.join("a", "secret.flac")]
