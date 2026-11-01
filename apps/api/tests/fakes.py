"""Doubles de test : MusicBrainz et moteur Soulseek sans réseau."""

import asyncio
import os
import struct
from pathlib import Path

from soulsick.models import TransferState
from soulsick.musicbrainz import MusicBrainz
from soulsick.scoring import PeerResult, RemoteFile
from soulsick.slsk.engine import SlskEngine, TransferUpdate

ARTIST_MBID = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
RG_MBID = "1b022e01-4da6-387b-8658-8678046e4cef"
REL_MBID = "b52a8f31-b5ab-34e9-92f4-f5b7110220f0"

TRACKS = [("Smells Like Teen Spirit", 301000), ("In Bloom", 254000), ("Come as You Are", 219000)]


def tiny_flac(path: Path, seconds: int, sample_rate: int = 44100) -> None:
    """FLAC minimal valide (STREAMINFO seul) : suffisant pour mutagen (durée + tags)."""
    total = seconds * sample_rate
    bits = (sample_rate << 44) | (1 << 41) | (15 << 36) | total  # 20b rate, 3b ch-1, 5b bps-1, 36b samples
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6 + bits.to_bytes(8, "big") + b"\x00" * 16
    header = bytes([0x80]) + len(info).to_bytes(3, "big")  # dernier bloc, type STREAMINFO
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fLaC" + header + info)


class FakeMB(MusicBrainz):
    async def search_artists(self, query, limit=15):
        return [{"mbid": ARTIST_MBID, "name": "Nirvana", "sort_name": "Nirvana", "disambiguation": "US grunge",
                 "country": "US", "type": "Group", "score": 100, "begin": "1987", "tags": []}]

    async def get_artist(self, mbid):
        return {"mbid": mbid, "name": "Nirvana", "sort_name": "Nirvana", "disambiguation": "US grunge",
                "country": "US", "type": "Group"}

    async def artist_release_groups(self, artist_mbid):
        return [
            {"mbid": RG_MBID, "title": "Nevermind", "primary_type": "album", "secondary_types": [],
             "first_release_date": "1991-09-24"},
            {"mbid": "live-1", "title": "MTV Unplugged", "primary_type": "album", "secondary_types": ["live"],
             "first_release_date": "1994-11-01"},
            {"mbid": "single-1", "title": "Lithium", "primary_type": "single", "secondary_types": [],
             "first_release_date": "1992-07-13"},
            {"mbid": "single-2", "title": "Future Single", "primary_type": "single", "secondary_types": [],
             "first_release_date": "2099-01-01"},
        ]

    async def release_group_releases(self, rg_mbid):
        return [{
            "mbid": REL_MBID, "title": "Nevermind", "date": "1991-09-24", "country": "US", "status": "Official",
            "disambiguation": "", "formats": "CD", "track_count": 3, "disc_count": 1, "label": "DGC",
            "barcode": None,
            "tracks": [
                {"mbid": f"t{i}", "recording_mbid": f"r{i}", "disc": 1, "position": i + 1, "number": str(i + 1),
                 "title": title, "artist_credit": "Nirvana", "length_ms": length}
                for i, (title, length) in enumerate(TRACKS)
            ],
        }]

    async def cover(self, kind, mbid, size=500):
        return None

    async def search_release_groups(self, artist, album, limit=5):
        if "nevermind" not in album.lower():
            return []
        return [{"mbid": RG_MBID, "title": "Nevermind", "primary_type": "album", "secondary_types": [],
                 "first_release_date": "1991-09-24", "artist_mbid": ARTIST_MBID, "artist_name": "Nirvana", "score": 100}]

    async def get_release_group(self, mbid):
        return {"mbid": mbid, "title": "Nevermind", "primary_type": "album", "secondary_types": [],
                "first_release_date": "1991-09-24", "artist_mbid": ARTIST_MBID, "artist_name": "Nirvana"}


class FakeEngine(SlskEngine):
    def __init__(self, downloads_dir: str) -> None:
        super().__init__(downloads_dir)
        self.logged_in = True
        self.downloads: list[tuple[str, str, str]] = []
        self.results: list[PeerResult] = []
        # Scénario par fichier : issue de chaque tentative successive ("complete" | "fail" | "hold")
        self.script: dict[str, list[str]] = {}
        self.paused: list[str] = []
        self.sent: list[tuple[str, str, str]] = []
        self.joined: set[str] = set()

    async def start(self, cfg, shares):
        self.config = cfg
        self.shares = shares

    async def stop(self):
        pass

    async def send_private(self, username, text):
        self.sent.append(("private", username, text))

    async def peer_profile(self, username, timeout=20):
        self.profiles_asked = getattr(self, "profiles_asked", 0) + 1
        return {"username": username, "status": "online", "privileged": False, "reachable": True,
                "avg_speed": 2_000_000, "shared_files": 1234, "shared_folders": 56, "uploads": 7,
                "description": "hello", "picture": None, "has_free_slots": True, "upload_slots": 3,
                "queue_size": 0, "interests": ["jazz"], "hated_interests": [], "country": None}

    async def send_room(self, room, text):
        from soulsick.slsk.engine import ChatEvent
        self.sent.append(("room", room, text))
        # The server echoes room messages back to the sender
        await self.on_chat(ChatEvent("room", room, self.config.username, text, 0, outgoing=True))

    async def join_room(self, room, timeout=15):
        self.joined.add(room)

    async def leave_room(self, room):
        self.joined.discard(room)

    async def room_list(self, timeout=20):
        return [{"name": "indie", "users": 120, "private": False}]

    def room_users(self, room):
        return ["alice", "bob"]

    async def search(self, query, wait_s):
        return list(self.results)

    async def download(self, username, remote_path, dest_dir):
        self.downloads.append((username, remote_path, dest_dir))
        name = remote_path.replace("\\", "/").rsplit("/", 1)[-1]
        local = os.path.join(dest_dir, name)
        title = name.split(" - ", 1)[-1].rsplit(".", 1)[0]
        if name.endswith(".lrc"):
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            Path(local).write_text(f"[00:01.00] {title}\n")
        else:
            seconds = next((ln // 1000 for t, ln in TRACKS if t == title), 200)
            tiny_flac(Path(local), seconds)

        outcomes = self.script.get(f"{username}:{name}", self.script.get(name, []))
        outcome = outcomes.pop(0) if outcomes else "complete"
        if outcome == "hold":
            return

        async def finish():
            await asyncio.sleep(0.01)
            await self.emit(username, remote_path, local, TransferState.QUEUED)
            if outcome == "fail":
                await asyncio.sleep(0.01)
                await self.emit(username, remote_path, local, TransferState.FAILED, "Enqueue failed due to internal error")
                return
            for state in (TransferState.TRANSFERRING, TransferState.COMPLETE):
                await asyncio.sleep(0.01)
                await self.emit(username, remote_path, local, state)

        asyncio.create_task(finish())

    async def emit(self, username, remote_path, local, state, error=None):
        await self.on_transfer(TransferUpdate(
            direction="download", username=username, remote_path=remote_path, local_path=local,
            size=100, bytes_transferred=100 if state == TransferState.COMPLETE else 0, speed=1000.0,
            state=state, place_in_queue=None, error=error, state_changed=True,
        ))

    async def pause(self, username, remote_path, direction="download"):
        self.paused.append(remote_path)

    async def apply_shares(self, shares):
        self.shares = shares


def peer_with_album(user: str = "goodpeer") -> PeerResult:
    files = [
        RemoteFile(f"@@music\\Nirvana\\Nevermind\\{i + 1:02d} - {t}.flac", size=100, bit_depth=16,
                   duration=ln // 1000)
        for i, (t, ln) in enumerate(TRACKS)
    ]
    return PeerResult(user, True, 1_500_000, 0, files)
