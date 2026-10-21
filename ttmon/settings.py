from datetime import datetime, time
from functools import cache
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"

SlotName = Literal["morning", "noon", "evening"]
SourceName = Literal["search", "following", "feed"]
FALLBACK_CATEGORY = "Другое"


def project_path(path: Path) -> Path:
    return path if path.is_absolute() else PROJECT_ROOT / path


def _parse_hhmm(value: object) -> time:
    # YAML 1.1 читает 13:55 без кавычек как int 835 (base-60); pydantic принял бы его как секунды
    if not isinstance(value, str):
        raise ValueError("время задаётся строкой 'HH:MM' в кавычках")
    return datetime.strptime(value, "%H:%M").time()


def _ordered_range[N: (int, float)](value: tuple[N, N]) -> tuple[N, N]:
    low, high = value
    if low < 0 or low > high:
        raise ValueError("диапазон [min, max]: 0 <= min <= max")
    return value


HHMM = Annotated[time, BeforeValidator(_parse_hhmm)]
IntRange = Annotated[tuple[int, int], AfterValidator(_ordered_range)]
FloatRange = Annotated[tuple[float, float], AfterValidator(_ordered_range)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotConfig(_Section):
    title: str
    start: HHMM
    sheet: str


class CollectConfig(_Section):
    target_selling: int = Field(gt=0)
    min_selling: int = Field(gt=0)
    max_minutes: int = Field(gt=0)
    max_rooms_opened: int = Field(gt=0)
    watch_seconds: IntRange
    pause_between_rooms: IntRange
    audio_seconds: int = Field(gt=0)
    min_viewers: int = Field(ge=0)
    allow_repeat_across_slots: bool
    sources_order: list[SourceName] = Field(min_length=1)
    search_queries: list[str] = Field(min_length=1)
    rotate_queries: bool
    search_scrolls: int = Field(ge=0)
    scroll_px: int = Field(gt=0)
    scroll_pause_seconds: FloatRange
    scroll_load_timeout_seconds: float = Field(gt=0)
    chat_messages: int = Field(ge=0)

    @field_validator("sources_order", "search_queries")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("значения не должны повторяться")
        return value


class Viewport(_Section):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class BrowserConfig(_Section):
    channel: str
    chrome_path: Path
    profile_dir: Path
    viewport: Viewport
    locale: str
    headless: bool


class GuardsConfig(_Section):
    page_timeout_seconds: float = Field(gt=0)
    probe_timeout_seconds: float = Field(gt=0)
    login_settle_seconds: float = Field(ge=0)
    network_retries: int = Field(ge=1)
    network_retry_pause_seconds: float = Field(ge=0)
    forbidden_pause_minutes: float = Field(gt=0)
    forbidden_max_in_row: int = Field(gt=0)
    timeouts_max_in_row: int = Field(gt=0)
    timeouts_pause_minutes: float = Field(gt=0)


class AIConfig(_Section):
    provider: Literal["gemini", "claude"]
    selling_threshold: float = Field(ge=0, le=1)
    send_audio: Literal["auto", "always", "never"]
    max_image_width: int = Field(gt=0)
    prompt_file: Path
    timeout_seconds: float = Field(gt=0)
    api_retries: int = Field(ge=1)
    retry_wait_initial_seconds: float = Field(ge=0)
    retry_wait_max_seconds: float = Field(ge=0)
    invalid_answer_retries: int = Field(ge=0)
    max_consecutive_failures: int = Field(ge=1)
    thinking_budget: int = Field(ge=0)


class EvaluationConfig(_Section):
    dir: Path
    selling_accuracy: float = Field(ge=0, le=1)
    category_accuracy: float = Field(ge=0, le=1)
    price_recall: float = Field(ge=0, le=1)


class GoogleConfig(_Section):
    drive_root_folder_name: str = Field(min_length=1)
    report_title: str
    share_with: list[str]

    @field_validator("report_title")
    @classmethod
    def _title_template(cls, value: str) -> str:
        try:
            value.format(date="01.01.2000")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(
                f"report_title: допустим только плейсхолдер {{date}} ({exc!r})"
            ) from exc
        return value


class ReportConfig(_Section):
    auto_after_evening: bool
    fallback_time: HHMM
    no_price_text: str


class WebConfig(_Section):
    host: str
    port: int = Field(ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        if value != "localhost" and not ip_address(value).is_loopback:
            raise ValueError("панель без авторизации: допустим только loopback-адрес")
        return value


class PathsConfig(_Section):
    data_dir: Path
    logs_dir: Path
    db_file: Path
    secrets_dir: Path
    test_data_dir: Path


class MediaConfig(_Section):
    font_path: Path
    jpeg_quality: int = Field(ge=1, le=100)
    stamp_city: str = Field(min_length=1)


class AppConfig(_Section):
    timezone: str
    slots: dict[SlotName, SlotConfig]
    collect: CollectConfig
    browser: BrowserConfig
    guards: GuardsConfig
    ai: AIConfig
    evaluation: EvaluationConfig
    categories: list[str] = Field(min_length=1)
    google: GoogleConfig
    report: ReportConfig
    web: WebConfig
    paths: PathsConfig
    media: MediaConfig

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестный часовой пояс: {value}") from exc
        return value

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("категории не должны повторяться")
        if FALLBACK_CATEGORY not in value:
            raise ValueError(f"в списке должна быть категория «{FALLBACK_CATEGORY}»")
        return value

    @model_validator(mode="after")
    def _all_slots(self) -> "AppConfig":
        missing = {"morning", "noon", "evening"} - self.slots.keys()
        if missing:
            raise ValueError(f"не заданы слоты: {sorted(missing)}")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,  # пустые KEY= из .env.example → None, а не SecretStr("")
        extra="ignore",
    )

    gemini_api_key: SecretStr | None = None
    # запасные ключи через запятую: переход при исчерпании квоты или отказе ключа
    gemini_api_keys_reserve: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash"
    anthropic_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    google_credentials_file: Path = Path("secrets/credentials.json")
    google_token_file: Path = Path("secrets/token.json")
    # ID шаблона — в .env, не в config.yaml: по ссылке на файл видна публичная папка со скриншотами
    google_template_sheet_id: str = ""

    def gemini_api_keys(self) -> list[str]:
        if self.gemini_api_key is None:
            return []
        reserve = self.gemini_api_keys_reserve
        raw = [self.gemini_api_key.get_secret_value()]
        raw += reserve.get_secret_value().split(",") if reserve else []
        return list(dict.fromkeys(k.strip() for k in raw if k.strip()))

    @field_validator("google_credentials_file", "google_token_file")
    @classmethod
    def _from_project_root(cls, value: Path) -> Path:
        # от корня проекта, а не от cwd: иначе token.json уедет из игнорируемого secrets/
        return project_path(value)


def load_config(path: Path = CONFIG_FILE) -> AppConfig:
    with path.open(encoding="utf-8") as fh:
        data: Any = yaml.safe_load(fh)
    return AppConfig.model_validate(data)


@cache
def get_config() -> AppConfig:
    return load_config()


@cache
def get_settings() -> Settings:
    return Settings()
