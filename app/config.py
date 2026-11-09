"""Настройки приложения из файла .env."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str = ""
    open_access: bool = True
    # Администраторы: Telegram ID или @ник через запятую (вдобавок к роли «администратор» в списке сотрудников)
    admins: str = ""
    timezone: str = "Asia/Almaty"

    database_url: str = f"sqlite+aiosqlite:///{(BASE_DIR / 'data' / 'app.db').as_posix()}"

    google_credentials_file: str = ""
    google_sheet_id: str = ""

    web_host: str = "127.0.0.1"
    web_port: int = 8000
    dashboard_password: str = ""

    workflow_file: str = str(BASE_DIR / "config" / "workflow.yaml")
    staff_file: str = str(BASE_DIR / "config" / "staff.yaml")

    @property
    def admin_ids(self) -> frozenset[str]:
        """Ники (без @, в нижнем регистре) и числовые ID администраторов."""
        return frozenset(a.strip().lstrip("@").lower() for a in self.admins.split(",") if a.strip())

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.google_credentials_file and self.google_sheet_id)

    @property
    def google_credentials_path(self) -> Path:
        path = Path(self.google_credentials_file)
        return path if path.is_absolute() else BASE_DIR / path


@lru_cache
def get_settings() -> Settings:
    return Settings()
