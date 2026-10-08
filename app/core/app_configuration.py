"""Validated, versioned UI configuration kept separately from analytical data."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.core.config import get_settings
from app.core.kpi_configuration import KpiConfiguration

FilterKey = Literal["organization", "category", "purpose", "direction", "unit", "asset", "group", "class_name", "result", "time"]
MetricKey = Literal["flights", "effective", "detected", "affected", "destroyed", "efficiency", "avg_flights_per_position", "weighted_efficiency"]
BlockKey = Literal["timeline", "map", "events", "departments", "purposes", "lost_devices"]
ColumnKey = Literal["timestamp", "grid_ref", "direction", "unit", "category", "asset", "group", "purpose", "class_name", "result"]
PageKey = Literal["dashboard", "statistics", "comparison", "map", "table", "saved", "dictionaries", "settings"]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
OptionKey = Annotated[str, StringConstraints(min_length=1, max_length=256)]
FILTER_KEYS = {"organization", "category", "purpose", "direction", "unit", "asset", "group", "class_name", "result", "time"}
DICTIONARY_KEYS = {"direction", "unit", "category", "asset", "group", "bbak", "rota", "battalion", "purpose", "class_name", "result"}


class ConfigurationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterField(ConfigurationModel):
    key: FilterKey
    label: Label
    placement: Literal["main", "extra", "hidden"]


CustomFilterId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,39}$")]
CustomFilterField = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
MAX_CUSTOM_FILTERS = 20
MAX_CUSTOM_FILTER_OPTIONS = 200


class CustomFilterOption(ConfigurationModel):
    value: OptionKey
    label: Label


class CustomFilter(ConfigurationModel):
    """Administrator-defined filter applied to one logical BI field.

    The field must be one the BI query engine already exposes; the engine still
    validates every query, so a custom filter can never reach arbitrary columns.
    """

    id: CustomFilterId
    label: Label
    field: CustomFilterField
    type: Literal["select", "number_range", "text", "boolean"]
    placement: Literal["main", "extra", "hidden"] = "extra"
    options: list[CustomFilterOption] = Field(default_factory=list, max_length=MAX_CUSTOM_FILTER_OPTIONS)

    @model_validator(mode="after")
    def validate_options(self):
        if self.type == "select":
            if not self.options:
                raise ValueError("A selection filter needs at least one option")
            values = [option.value for option in self.options]
            if len(values) != len(set(values)):
                raise ValueError("Filter option values must be unique")
        elif self.options:
            raise ValueError("Only selection filters have options")
        return self


class FilterConfiguration(ConfigurationModel):
    layout: Literal["compact", "steps"]
    organization_dimension: Literal["bbak", "rota", "battalion", "unit"]
    fields: list[FilterField] = Field(min_length=10, max_length=10)
    # Older persisted versions have no custom filters.
    custom: list[CustomFilter] = Field(default_factory=list, max_length=MAX_CUSTOM_FILTERS)

    @field_validator("fields")
    @classmethod
    def complete_fields(cls, fields: list[FilterField]) -> list[FilterField]:
        if {field.key for field in fields} != FILTER_KEYS:
            raise ValueError("Each supported filter must appear exactly once; hide it using placement")
        return fields

    @model_validator(mode="after")
    def validate_custom(self):
        ids = [item.id for item in self.custom]
        if len(ids) != len(set(ids)):
            raise ValueError("Custom filter ids must be unique")
        if set(ids) & FILTER_KEYS:
            raise ValueError("Custom filter ids must not reuse built-in filter keys")
        if self.custom:
            # Imported lazily: the query engine pulls in the whole analytics stack.
            from app.bi.engine import FILTER_FIELDS

            # x_<column> fields are administrator-selected ClickHouse columns,
            # checked against the live table schema when the engine registers them.
            unknown = {item.field for item in self.custom if not item.field.startswith("x_")} - FILTER_FIELDS
            if unknown:
                raise ValueError("Custom filters can only use fields supported by the analytics engine")
        return self


class DashboardConfiguration(ConfigurationModel):
    metric_keys: list[MetricKey] = Field(min_length=1, max_length=8)
    blocks: list[BlockKey] = Field(max_length=6)

    @field_validator("metric_keys", "blocks")
    @classmethod
    def unique_keys(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Configuration selections must be unique")
        return values


class TableConfiguration(ConfigurationModel):
    page_size: int = Field(ge=5, le=200, strict=True)
    columns: list[ColumnKey] = Field(min_length=1, max_length=10)

    @field_validator("columns")
    @classmethod
    def unique_columns(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("Table columns must be unique")
        return values


class CardDetailsConfiguration(ConfigurationModel):
    interaction: Literal["auto", "click", "disabled"] = "auto"
    hover_delay_ms: int = Field(default=0, ge=0, le=1500, strict=True)
    width: Literal["standard", "wide"] = "standard"


class AppearanceConfiguration(ConfigurationModel):
    title: Label
    subtitle: str = Field(max_length=240)
    density: Literal["compact", "comfortable"]
    default_page: PageKey
    menu: list[PageKey] = Field(min_length=1, max_length=8)
    card_details: CardDetailsConfiguration = Field(default_factory=CardDetailsConfiguration)

    @model_validator(mode="after")
    def validate_menu(self):
        if len(self.menu) != len(set(self.menu)):
            raise ValueError("Menu pages must be unique")
        if self.default_page not in self.menu:
            raise ValueError("The default page must be visible in the menu")
        return self


class DefaultConfiguration(ConfigurationModel):
    date_range_days: int = Field(ge=1, le=365, strict=True)
    comparison_mode: Literal["periods", "units", "units_over_time"]
    comparison_granularity: Literal["day", "week", "month", "quarter"]


class DictionaryConfiguration(ConfigurationModel):
    labels: dict[str, dict[OptionKey, Label]] = Field(default_factory=dict, max_length=11)

    @field_validator("labels")
    @classmethod
    def supported_dictionaries(cls, values: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        if set(values) - DICTIONARY_KEYS:
            raise ValueError("Only existing filter dictionaries support label overrides")
        if any(len(labels) > 500 for labels in values.values()):
            raise ValueError("Each dictionary supports at most 500 label overrides")
        return values


class ApplicationConfiguration(ConfigurationModel):
    filters: FilterConfiguration
    dashboard: DashboardConfiguration
    tables: TableConfiguration
    appearance: AppearanceConfiguration
    defaults: DefaultConfiguration
    dictionaries: DictionaryConfiguration
    # Older persisted versions intentionally load their original calculations.
    kpi: KpiConfiguration = Field(default_factory=KpiConfiguration)


class RevisionSummary(ConfigurationModel):
    revision: int
    created_at: str
    action: Literal["initial", "publish", "rollback"]


class AdministrationSnapshot(ConfigurationModel):
    revision: int
    config: ApplicationConfiguration
    draft: ApplicationConfiguration | None
    draft_base_revision: int | None
    draft_version: int
    history: list[RevisionSummary]


class ConfigurationConflict(ValueError):
    pass


class ConfigurationRevisionNotFound(ValueError):
    pass


def default_application_configuration() -> ApplicationConfiguration:
    """Seed existing deployment defaults; future edits live in the persistent DB."""
    frontend_dir = get_settings().config_root / "frontend"

    def read(name: str) -> dict:
        try:
            return json.loads((frontend_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    app = read("app.json")
    ui = read("ui.json")
    field_labels = {
        "organization": "Підрозділ", "category": "Кафедра", "purpose": "Мета вильоту",
        "direction": "Напрямок", "unit": "Зона відповідальності", "asset": "Конкретний засіб", "group": "Екіпаж",
        "class_name": "Клас цілі", "result": "Результат", "time": "Час і звітна доба",
    }
    return ApplicationConfiguration.model_validate({
        "filters": {
            "layout": "compact", "organization_dimension": "bbak",
            "fields": [{"key": key, "label": label, "placement": "main" if key in {"organization", "category", "purpose"} else "extra"} for key, label in field_labels.items()],
        },
        "dashboard": {
            "metric_keys": ui.get("kpi_metric_keys", ["flights", "effective", "detected", "affected", "destroyed", "efficiency"]),
            "blocks": ["map", "timeline", "departments", "lost_devices"],
        },
        "tables": {
            "page_size": ui.get("tables", {}).get("main_page_size", 18),
            "columns": ["timestamp", "grid_ref", "direction", "unit", "category", "asset", "group", "purpose", "class_name", "result"],
        },
        "appearance": {
            "title": app.get("branding", {}).get("sidebar_title", "ANALYTICS"),
            "subtitle": "Огляд. Аналіз. Результат.", "density": "compact",
            "default_page": app.get("routing", {}).get("default_page", "dashboard"),
            "menu": ["dashboard", "statistics", "comparison", "map", "table", "saved", "dictionaries", "settings"],
        },
        "defaults": {
            "date_range_days": app.get("defaults", {}).get("date_range_days", 7),
            "comparison_mode": "units_over_time", "comparison_granularity": "week",
        },
        "dictionaries": {"labels": {}},
    })


class ApplicationConfigurationStore:
    def __init__(self, path: str | Path, defaults: Callable[[], ApplicationConfiguration] = default_application_configuration):
        self.path = Path(path)
        self.defaults = defaults

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            # This serializes competing writes and gives snapshots one revision.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS configuration_versions (revision INTEGER PRIMARY KEY, config_json TEXT NOT NULL, created_at TEXT NOT NULL, action TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS configuration_state (id INTEGER PRIMARY KEY CHECK(id = 1), active_revision INTEGER NOT NULL, draft_version INTEGER NOT NULL DEFAULT 0)")
            state_columns = {row["name"] for row in connection.execute("PRAGMA table_info(configuration_state)")}
            if "draft_version" not in state_columns:
                connection.execute("ALTER TABLE configuration_state ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE TABLE IF NOT EXISTS configuration_draft (id INTEGER PRIMARY KEY CHECK(id = 1), config_json TEXT NOT NULL, base_revision INTEGER NOT NULL)")
            if connection.execute("SELECT active_revision FROM configuration_state WHERE id = 1").fetchone() is None:
                connection.execute("INSERT INTO configuration_versions VALUES (?, ?, ?, ?)", (1, self.defaults().model_dump_json(), self._now(), "initial"))
                connection.execute("INSERT INTO configuration_state (id, active_revision, draft_version) VALUES (1, 1, 0)")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _active_revision(connection: sqlite3.Connection) -> int:
        return connection.execute("SELECT active_revision FROM configuration_state WHERE id = 1").fetchone()[0]

    def _require_revision(self, connection: sqlite3.Connection, expected: int) -> int:
        current = self._active_revision(connection)
        if current != expected:
            raise ConfigurationConflict("Configuration changed; reload the current revision before applying your changes")
        return current

    @staticmethod
    def _draft_version(connection: sqlite3.Connection) -> int:
        return connection.execute("SELECT draft_version FROM configuration_state WHERE id = 1").fetchone()[0]

    def _require_draft_version(self, connection: sqlite3.Connection, expected: int | None) -> None:
        if expected is not None and self._draft_version(connection) != expected:
            raise ConfigurationConflict("The draft changed; reload and review the latest draft before saving or publishing")

    def _snapshot(self, connection: sqlite3.Connection) -> AdministrationSnapshot:
        current = self._active_revision(connection)
        active = connection.execute("SELECT config_json FROM configuration_versions WHERE revision = ?", (current,)).fetchone()
        draft = connection.execute("SELECT config_json, base_revision FROM configuration_draft WHERE id = 1").fetchone()
        history = connection.execute("SELECT revision, created_at, action FROM configuration_versions ORDER BY revision DESC LIMIT 100").fetchall()
        return AdministrationSnapshot(
            revision=current, config=ApplicationConfiguration.model_validate_json(active[0]),
            draft=ApplicationConfiguration.model_validate_json(draft[0]) if draft else None,
            draft_base_revision=draft[1] if draft else None,
            draft_version=self._draft_version(connection),
            history=[RevisionSummary(**dict(row)) for row in history],
        )

    def snapshot(self) -> AdministrationSnapshot:
        with self._transaction() as connection:
            return self._snapshot(connection)

    def save_draft(self, config: ApplicationConfiguration, base_revision: int, expected_draft_version: int | None = None) -> AdministrationSnapshot:
        # Revalidate copies even when callers constructed a model without validation.
        validated = ApplicationConfiguration.model_validate(config.model_dump())
        with self._transaction() as connection:
            self._require_revision(connection, base_revision)
            self._require_draft_version(connection, expected_draft_version)
            connection.execute("INSERT INTO configuration_draft VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET config_json = excluded.config_json, base_revision = excluded.base_revision", (validated.model_dump_json(), base_revision))
            connection.execute("UPDATE configuration_state SET draft_version = draft_version + 1 WHERE id = 1")
            return self._snapshot(connection)

    def preview(self, config: ApplicationConfiguration, base_revision: int) -> dict:
        validated = ApplicationConfiguration.model_validate(config.model_dump())
        with self._transaction() as connection:
            current = self._require_revision(connection, base_revision)
            return {"valid": True, "revision": current, "config": validated.model_dump(mode="json")}

    def _activate(self, connection: sqlite3.Connection, config_json: str, action: str) -> AdministrationSnapshot:
        current = self._active_revision(connection)
        connection.execute("INSERT INTO configuration_versions VALUES (?, ?, ?, ?)", (current + 1, config_json, self._now(), action))
        connection.execute("UPDATE configuration_state SET active_revision = ?, draft_version = draft_version + 1 WHERE id = 1", (current + 1,))
        connection.execute("DELETE FROM configuration_draft WHERE id = 1")
        return self._snapshot(connection)

    def publish(self, base_revision: int, expected_draft_version: int | None = None) -> AdministrationSnapshot:
        with self._transaction() as connection:
            self._require_revision(connection, base_revision)
            self._require_draft_version(connection, expected_draft_version)
            draft = connection.execute("SELECT config_json, base_revision FROM configuration_draft WHERE id = 1").fetchone()
            if draft is None:
                raise ConfigurationConflict("No draft is available to publish")
            if draft[1] != base_revision:
                raise ConfigurationConflict("The draft belongs to an older revision; save it again before publishing")
            config = ApplicationConfiguration.model_validate_json(draft[0])
            return self._activate(connection, config.model_dump_json(), "publish")

    def published_revision(self, revision: int) -> ApplicationConfiguration:
        """Resolve an explicitly selected immutable published rule snapshot."""
        with self._transaction() as connection:
            row = connection.execute('SELECT config_json FROM configuration_versions WHERE revision=?', (revision,)).fetchone()
            if row is None:
                raise ConfigurationRevisionNotFound('The selected published rule revision does not exist')
            return ApplicationConfiguration.model_validate_json(row[0])

    def rollback(self, target_revision: int, base_revision: int) -> AdministrationSnapshot:
        with self._transaction() as connection:
            self._require_revision(connection, base_revision)
            target = connection.execute("SELECT config_json FROM configuration_versions WHERE revision = ?", (target_revision,)).fetchone()
            if target is None:
                raise ConfigurationRevisionNotFound("The selected configuration revision does not exist")
            config = ApplicationConfiguration.model_validate_json(target[0])
            return self._activate(connection, config.model_dump_json(), "rollback")


def get_configuration_store() -> ApplicationConfigurationStore:
    return ApplicationConfigurationStore(os.environ.get("APP_CONFIGURATION_DB", "/app/data/app_configuration.sqlite3"))
