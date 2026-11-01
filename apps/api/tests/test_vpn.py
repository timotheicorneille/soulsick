"""Behind gluetun: Soulseek listens on the port the VPN forwards, and follows it when it changes."""

from fakes import FakeEngine, FakeMB
from httpx import ASGITransport, AsyncClient

from soulsick.app import create_app
from soulsick.config import Env
from soulsick.core import Core
from soulsick.settings_store import SoulseekSettings
from soulsick.slsk.engine import SlskEngine


def test_engine_uses_the_forwarded_port_single_and_without_upnp(tmp_path):
    e = SlskEngine(str(tmp_path))
    cfg = SoulseekSettings(username="u", password="p", listen_port=50300, upnp=True)
    s = e._build_settings(cfg)
    assert (s.network.listening.port, s.network.listening.obfuscated_port, s.network.upnp.enabled) == (50300, 50301, True)
    e.forwarded_port = 41234
    s = e._build_settings(cfg)
    assert (s.network.listening.port, s.network.listening.obfuscated_port, s.network.upnp.enabled) == (41234, 0, False)


async def test_core_follows_the_forwarded_port(tmp_path):
    port_file = tmp_path / "forwarded_port"
    port_file.write_text("41234\n")
    env = Env(config_dir=tmp_path / "config", music_dir=tmp_path / "music", downloads_dir=tmp_path / "downloads",
              forwarded_port_file=port_file)
    engine = FakeEngine(str(env.downloads_dir))
    core = Core(env, mb=FakeMB(env.covers_dir), engine=engine)
    app = create_app(env, core)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert engine.forwarded_port == 41234
            await client.post("/api/auth/setup", json={"username": "admin", "password": "motdepasse"})
            assert (await client.get("/api/status")).json()["engine"]["forwarded_port"] == 41234

            # gluetun reconnects and gets another port
            port_file.write_text("52011")
            await core.restart_engine()
            assert engine.forwarded_port == 52011

            # Garbage or an empty file: no port (the configured one is used meanwhile)
            port_file.write_text("")
            assert core._read_forwarded_port() is None


async def test_no_forwarded_port_keeps_soulseek_offline(tmp_path):
    """Without a port from the VPN, nobody could reach us: the engine waits instead of connecting."""
    port_file = tmp_path / "forwarded_port"
    env = Env(config_dir=tmp_path / "config", music_dir=tmp_path / "music", downloads_dir=tmp_path / "downloads",
              forwarded_port_file=port_file)
    engine = FakeEngine(str(env.downloads_dir))
    started = []
    engine.start = lambda cfg, shares: started.append(cfg) or _done()
    core = Core(env, mb=FakeMB(env.covers_dir), engine=engine)
    app = create_app(env, core)
    async with app.router.lifespan_context(app):
        assert not started and not engine.logged_in
        assert engine.error.key == "network.port_waiting"

        port_file.write_text("41234")
        await core.restart_engine()
        assert len(started) == 1 and engine.forwarded_port == 41234

        # gluetun loses the tunnel and clears the file: Soulseek goes offline again
        port_file.write_text("")
        await core.restart_engine()
        assert len(started) == 1 and engine.forwarded_port is None
        assert engine.error.key == "network.port_waiting"


async def _done():
    return None
