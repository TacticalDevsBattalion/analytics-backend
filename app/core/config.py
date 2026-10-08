from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiConfig(BaseModel):
    title: str
    version: str
    docs_url: str
    openapi_url: str
    router_prefix: str


class CorsConfig(BaseModel):
    allow_credentials: bool
    allow_methods: list[str]
    allow_headers: list[str]


class AppConfig(BaseModel):
    api: ApiConfig
    cors: CorsConfig




class HttpConfig(BaseModel):
    timeout_seconds: float
    error_body_max_chars: int












class ClickHouseHttpConfig(BaseModel):
    timeout_seconds: float
    connect_timeout_seconds: float
    error_body_max_chars: int


class ClickHouseQueryConfig(BaseModel):
    max_execution_time_seconds: int = 90
    use_query_cache: bool = True
    event_table_limit: int = 100
    lost_devices_table_limit: int | None = None


class ClickHouseConfig(BaseModel):
    http: ClickHouseHttpConfig
    tables: dict[str, str]
    query: ClickHouseQueryConfig
    required_fields: dict[str, list[str]]
    selects: dict[str, list[str]]


class ClickHouseFieldMappingsConfig(BaseModel):
    entities: dict[str, dict[str, str | list[str]]]
    # Runtime-only mappings for administrator-selected columns (never serialised).
    _extra: dict[str, dict[str, str]] = PrivateAttr(default_factory=dict)

    def set_extra(self, entity: str, mapping: dict[str, str]) -> None:
        self._extra[entity] = dict(mapping)

    def candidates(self, entity: str, key: str) -> list[str]:
        extra = self._extra.get(entity, {}).get(key)
        if extra:
            return [extra]
        try:
            value = self.entities[entity][key]
        except KeyError as exc:
            raise RuntimeError(
                f"ClickHouse field mapping not found: entity={entity!r}, key={key!r}"
            ) from exc
        return [value] if isinstance(value, str) else list(value)


class DayPeriodConfig(BaseModel):
    start: str
    end: str


class FlightPurposeGroupConfig(BaseModel):
    key: str
    label: str
    purposes: list[str]


class FallbackPurposeMatchingConfig(BaseModel):
    recon_contains: str
    recon_exact: list[str]
    recon_group_key: str
    logistics_contains: list[str]
    logistics_group_key: str
    mining_contains: str
    mining_group_key: str


class NamedPurposeConfig(BaseModel):
    key: str
    label: str


class FollowupReconConfig(NamedPurposeConfig):
    purpose: str


class AnalyticsConfig(BaseModel):
    timezone: str
    day_period: DayPeriodConfig
    personnel_target_classes: list[str]
    personnel_target_prefixes: list[str]
    flight_purpose_groups: list[FlightPurposeGroupConfig]
    excluded_analytics_purposes: list[str]
    strike_device_types: list[str]
    lost_device_type_aliases: dict[str, str]
    lost_devices_sentinel: str
    detected_ammunition_name: str
    unknown_device_type_label: str
    followup_recon: FollowupReconConfig
    other_group: NamedPurposeConfig
    fallback_purpose_matching: FallbackPurposeMatchingConfig
    result_names: dict[str, str]


class CacheTtlConfig(BaseModel):
    filter_options: int
    overview: int
    timeline: int
    events: int
    map: int
    source_status: int
    clickhouse_dataset: int
    clickhouse_schema: int


class CacheWarmupConfig(BaseModel):
    filter_options: bool = True


class CacheAdminConfig(BaseModel):
    enabled: bool = True
    header: str = "X-Cache-Admin-Token"


class CacheConfig(BaseModel):
    enabled: bool = True
    max_entries: int = 256
    stale_if_error_seconds: int = 3600
    stale_while_revalidate: bool = True
    copy_on_read: bool = False
    copy_on_write: bool = False
    ttl_seconds: CacheTtlConfig
    warmup: CacheWarmupConfig = CacheWarmupConfig()
    admin: CacheAdminConfig = CacheAdminConfig()


class MockOverviewConfig(BaseModel):
    events_per_day_min: int
    events_per_day_max: int
    positive_ratio_min: float
    positive_ratio_max: float
    detected_ratio_min: float
    detected_ratio_max: float
    resolved_ratio_min: float
    resolved_ratio_max: float
    volume_min: int
    volume_max: int
    delta_min: float
    delta_max: float


class MockTimelineConfig(BaseModel):
    max_days: int
    events_min: int
    events_max: int
    effective_ratio_min: float
    effective_ratio_max: float


class MockEventsConfig(BaseModel):
    count: int
    base_hour: int
    minute_step_min: int
    minute_step_max: int
    base_lat: float
    base_lon: float
    lat_spread: float
    lon_spread: float
    id_prefix: str
    id_start: int
    grid_prefix: str


class MockConfig(BaseModel):
    options: dict[str, list[str]]
    overview: MockOverviewConfig
    timeline: MockTimelineConfig
    events: MockEventsConfig


class BackendConfig(BaseModel):
    app: AppConfig
    clickhouse: ClickHouseConfig
    clickhouse_fields: ClickHouseFieldMappingsConfig
    analytics: AnalyticsConfig
    cache: CacheConfig
    mock: MockConfig



class Settings(BaseSettings):
    # Runtime/source selection.
    data_source: Literal["mock", "clickhouse"] | None = None
    mock_data: bool = True

    # Deployment-specific values and secrets belong in environment variables.
    api_cors_origins: str = ""

    # ClickHouse is the primary analytics store. Secrets stay in .env.
    clickhouse_url: str = ""
    clickhouse_host: str = ""
    clickhouse_port: int = 8123
    clickhouse_database: str = "default"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    clickhouse_secure: bool = False
    clickhouse_verify_ssl: bool = True
    clickhouse_ca_file: str = ""



    # Optional token protecting POST /api/cache/clear. If blank, the endpoint is disabled.
    cache_admin_token: str = ""
    api_prefix: str = ""

    # Shared analytics cache. URL credentials stay in environment variables.
    redis_url: str = ""
    cache_enabled: bool | None = None
    cache_key_prefix: str = Field(default="analytics", min_length=1, max_length=80)
    cache_l1_ttl_seconds: float = Field(default=15, ge=0, le=300)
    cache_l1_max_entries: int = Field(default=256, ge=1, le=100000)
    cache_l1_max_bytes: int = Field(default=33554432, ge=1024)
    cache_max_value_bytes: int = Field(default=1048576, ge=1024)
    cache_redis_timeout_seconds: float = Field(default=0.5, gt=0, le=10)
    cache_retry_seconds: float = Field(default=5, ge=0, le=300)
    cache_lease_seconds: float = Field(default=120, ge=1, le=900)
    cache_lock_wait_seconds: float = Field(default=120, ge=0, le=900)

    # Optional shared config root. The backend reads <root>/backend/*.json.
    config_root_dir: str = ""

    # Backward-compatible direct backend-config override. Prefer CONFIG_ROOT_DIR.
    backend_config_dir: str = ""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        extra="ignore",
    )

    @field_validator("api_prefix")
    @classmethod
    def valid_api_prefix(cls, value: str) -> str:
        if value and not re.fullmatch(r"/(?:[A-Za-z0-9_-]+)(?:/[A-Za-z0-9_-]+)*", value):
            raise ValueError("API_PREFIX must start with / and contain path segments without a trailing slash")
        return value

    @property
    def cors_origins(self) -> list[str]:
        return [x.strip() for x in self.api_cors_origins.split(",") if x.strip()]

    @property
    def active_source(self) -> Literal["mock", "clickhouse"]:
        if self.data_source:
            return self.data_source
        return "mock" if self.mock_data else "clickhouse"

    @property
    def config_root(self) -> Path:
        if self.config_root_dir.strip():
            return Path(self.config_root_dir).expanduser().resolve()

        current = Path(__file__).resolve()
        candidates = [
            current.parents[3] / "config",  # repository layout: backend/app/core/config.py
            current.parents[2] / "config",  # container layout: /app/app/core/config.py
        ]
        for candidate in candidates:
            if (candidate / "backend").is_dir():
                return candidate
        return candidates[0]

    @property
    def config_dir(self) -> Path:
        if self.backend_config_dir.strip():
            return Path(self.backend_config_dir).expanduser().resolve()
        return self.config_root / "backend"


def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Backend config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in backend config file {path}: {exc}") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_backend_config() -> BackendConfig:
    config_dir = get_settings().config_dir
    config = BackendConfig(
        app=AppConfig.model_validate(_read_json(config_dir / "app.json")),
        clickhouse=ClickHouseConfig.model_validate(_read_json(config_dir / "clickhouse.json")),
        clickhouse_fields=ClickHouseFieldMappingsConfig.model_validate(
            _read_json(config_dir / "clickhouse_fields.json")
        ),
        analytics=AnalyticsConfig.model_validate(_read_json(config_dir / "analytics.json")),
        cache=CacheConfig.model_validate(_read_json(config_dir / "cache.json")),
        mock=MockConfig.model_validate(_read_json(config_dir / "mock.json")),
    )


    if get_settings().api_prefix:
        config.app.api.router_prefix = get_settings().api_prefix
    return config
