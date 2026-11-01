"""Réglages applicatifs persistés en base (modifiables depuis l'interface)."""


from pydantic import BaseModel, Field

from .db import Database
from .models import Setting

DEFAULT_NAMING = "{albumartist}/{year} - {album}/{disc_prefix}{track:02} - {title}"


class SoulseekSettings(BaseModel):
    username: str = ""
    password: str = ""
    listen_port: int = 50300
    upnp: bool = True
    upload_slots: int = 3
    upload_speed_kbps: int = 0
    download_speed_kbps: int = 0
    share_library: bool = True
    description: str = "Soulsick — self-hosted Soulseek client"


class HunterSettings(BaseModel):
    enabled: bool = True
    interval_minutes: int = 15
    max_searches_per_run: int = 10
    search_wait_seconds: int = 25
    proposals: int = 5
    min_match: float = 0.6
    download_covers: bool = True
    # When an album download counts as stuck (shown to you, nothing automatic)
    stuck_no_progress_minutes: int = Field(10, ge=1)
    stuck_queue_minutes: int = Field(30, ge=1)
    # Flagged from this attempt on (0 = never)
    stuck_after_attempts: int = Field(2, ge=0)


class LibrarySettings(BaseModel):
    naming_template: str = DEFAULT_NAMING
    sync_interval_hours: int = 24
    write_tags: bool = True
    embed_cover: bool = True
    # Lyrics from LRCLIB for the tracks without, after each import
    fetch_lyrics: bool = True


class NavidromeSettings(BaseModel):
    enabled: bool = False
    url: str = ""
    username: str = ""
    password: str = ""


class AppSettings(BaseModel):
    soulseek: SoulseekSettings = Field(default_factory=SoulseekSettings)
    hunter: HunterSettings = Field(default_factory=HunterSettings)
    library: LibrarySettings = Field(default_factory=LibrarySettings)
    navidrome: NavidromeSettings = Field(default_factory=NavidromeSettings)


class SettingsStore:
    KEY = "app"

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: AppSettings | None = None

    async def get(self) -> AppSettings:
        if self._cache is None:
            async with self.db.session() as s:
                row = await s.get(Setting, self.KEY)
                self._cache = AppSettings.model_validate(row.value) if row else AppSettings()
        return self._cache.model_copy(deep=True)

    async def save(self, value: AppSettings) -> AppSettings:
        async with self.db.session() as s:
            row = await s.get(Setting, self.KEY)
            data = value.model_dump()
            if row:
                row.value = data
            else:
                s.add(Setting(key=self.KEY, value=data))
            await s.commit()
        self._cache = value.model_copy(deep=True)
        return value

    async def get_raw(self, key: str) -> dict | None:
        async with self.db.session() as s:
            row = await s.get(Setting, key)
            return row.value if row else None

    async def set_raw(self, key: str, value: dict) -> None:
        async with self.db.session() as s:
            row = await s.get(Setting, key)
            if row:
                row.value = value
            else:
                s.add(Setting(key=key, value=value))
            await s.commit()
