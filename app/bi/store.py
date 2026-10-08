"""Normalized BI storage with append-only schema migrations and revision checks."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.bi.models import (
    ENTITY_MODELS, CategoryWeight, DashboardAssignment, DashboardDefinition,
    DataScope, KpiDefinition, MetricDefinition, QueryRequest, RoleDefinition,
    UserAccessDefinition, UserLayoutOverride, VersionedEntity, WidgetDefinition,
    LayoutOverrideInput,
)


class BiConflict(ValueError):
    pass


class BiNotFound(ValueError):
    pass


class BiStore:
    TABLES = {"dashboards": "dashboards", "widgets": "dashboard_widgets", "metrics": "metric_definitions", "kpis": "kpi_definitions", "category_weights": "category_weights", "roles": "roles", "user_access": "user_access", "overrides": "dashboard_user_overrides"}
    TABLES["weight_sets"] = "weight_sets"

    def __init__(self, path: str | Path, *, default_viewer_scope: str | None = None):
        self.path = Path(path)
        self.default_viewer_scope = default_viewer_scope or os.environ.get("APP_BI_VIEWER_DEFAULT_SCOPE", "NONE").upper()
        if self.default_viewer_scope not in {"NONE", "ALL"}:
            raise ValueError("APP_BI_VIEWER_DEFAULT_SCOPE must be NONE or ALL")
        self._lock = threading.Lock()
        self._initialized = False

    def _initialize(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("CREATE TABLE IF NOT EXISTS bi_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                migrations = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
                for migration in migrations:
                    version = int(migration.name.split("_", 1)[0])
                    # The checked-in SQL owns every schema change, including new installations.
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute("SELECT 1 FROM bi_schema_migrations WHERE version=?", (version,)).fetchone():
                        connection.commit()
                        continue
                    for statement in migration.read_text(encoding="utf-8").split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute("INSERT INTO bi_schema_migrations VALUES (?,?)", (version, self._now()))
                    connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                if not connection.execute("SELECT seeded FROM bi_state WHERE id=1").fetchone()["seeded"]:
                    self._save_record(connection, "roles", RoleDefinition(id="role_viewer", key="viewer", title="Перегляд аналітики", permissions=["dashboard.view", "statistics.view", "comparison.view", "metric.view", "kpi.view"]), 0)
                    connection.execute("UPDATE bi_state SET seeded=1 WHERE id=1")
                connection.commit()
                self._expire_cache_jobs(connection, time.time())
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            self._initialized = True

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _connection(self):
        self._initialize()
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            if connection.in_transaction:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def _table(cls, entity):
        if entity not in cls.TABLES:
            raise ValueError("Unsupported BI entity")
        return cls.TABLES[entity]

    def _decode(self, entity, row, connection):
        model = ENTITY_MODELS[entity].model_validate_json(row["definition_json"])
        if entity == "dashboards":
            model.widgets = [WidgetDefinition.model_validate_json(item["definition_json"]) for item in connection.execute("SELECT definition_json FROM dashboard_widgets WHERE dashboard_id=? ORDER BY created_at,id", (model.id,))]
            model.assignments = [DashboardAssignment(assignment_type=item["assignment_type"], assignment_id=item["assignment_id"]) for item in connection.execute("SELECT * FROM dashboard_assignments WHERE dashboard_id=? ORDER BY assignment_type,assignment_id", (model.id,))]
        return model

    def list(self, entity: str):
        table = self._table(entity)
        with self._connection() as connection:
            return [self._decode(entity, row, connection) for row in connection.execute(f"SELECT * FROM {table} ORDER BY created_at,id")]

    def get(self, entity: str, entity_id: str):
        table = self._table(entity)
        with self._connection() as connection:
            row = connection.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise BiNotFound("BI definition was not found")
            return self._decode(entity, row, connection)

    def find_by_key(self, entity: str, key: str, *, weight_set_id: str = "target_equivalent"):
        column = {"metrics": "metric_key", "kpis": "metric_key", "roles": "role_key", "user_access": "user_id", "dashboards": "slug", "category_weights": "category", "weight_sets": "weight_set_key"}.get(entity)
        if column is None:
            raise ValueError("This entity does not have a lookup key")
        table = self._table(entity)
        with self._connection() as connection:
            predicate = f"{column}=?" + (" AND weight_set_id=?" if entity == "category_weights" else "")
            values = (key, weight_set_id) if entity == "category_weights" else (key,)
            row = connection.execute(f"SELECT * FROM {table} WHERE {predicate}", values).fetchone()
            return self._decode(entity, row, connection) if row else None

    def _save_record(self, connection, entity, definition, expected_revision):
        table = self._table(entity)
        definition = ENTITY_MODELS[entity].model_validate(definition.model_dump() if isinstance(definition, VersionedEntity) else definition)
        if entity == "widgets" and connection.execute("SELECT 1 FROM dashboard_personal_widgets WHERE id=?", (definition.id,)).fetchone():
            raise BiConflict("System and personal widgets require distinct stable IDs")
        current = connection.execute(f"SELECT * FROM {table} WHERE id=?", (definition.id,)).fetchone()
        current_revision = current["revision"] if current else 0
        expected = definition.revision if expected_revision is None else expected_revision
        if expected != current_revision:
            raise BiConflict("BI definition changed elsewhere; reload before saving")
        now = self._now()
        definition = definition.model_copy(deep=True, update={"revision": current_revision + 1, "created_at": current["created_at"] if current else now, "updated_at": now})
        columns = ["id", "revision", "definition_json", "created_at", "updated_at"]
        values = [definition.id, definition.revision, "", definition.created_at, definition.updated_at]
        extras = {
            "dashboards": {"slug": getattr(definition, "slug", "")},
            "widgets": {"dashboard_id": getattr(definition, "dashboard_id", "")},
            "metrics": {"metric_key": getattr(definition, "key", "")},
            "kpis": {"metric_key": getattr(definition, "key", "")},
            "roles": {"role_key": getattr(definition, "key", "")},
            "user_access": {"user_id": getattr(definition, "user_id", "")},
            "category_weights": {"category": getattr(definition, "category", ""), "weight_set_id": getattr(definition, "weight_set_id", "target_equivalent")},
            "weight_sets": {"weight_set_key": getattr(definition, "key", "")},
            "overrides": {"dashboard_id": getattr(definition, "dashboard_id", ""), "widget_id": getattr(definition, "widget_id", ""), "user_id": getattr(definition, "user_id", "")},
        }[entity]
        columns.extend(extras)
        values.extend(extras.values())
        payload = definition.model_dump(mode="json", by_alias=True)
        if entity == "dashboards":
            payload["widgets"] = []
            payload["assignments"] = []
        values[2] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        update = ",".join(f"{column}=excluded.{column}" for column in columns if column != "id")
        connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in values)}) ON CONFLICT(id) DO UPDATE SET {update}", values)
        connection.execute("UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1")
        return definition

    def save(self, entity: str, definition, expected_revision: int | None = None, actor: str | None = None):
        self._table(entity)
        definition = ENTITY_MODELS[entity].model_validate(definition.model_dump() if isinstance(definition, VersionedEntity) else definition)
        try:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if entity in {"metrics", "kpis"}:
                    other = "kpi_definitions" if entity == "metrics" else "metric_definitions"
                    if connection.execute(f"SELECT 1 FROM {other} WHERE metric_key=?", (definition.key,)).fetchone():
                        raise BiConflict("Metric and KPI keys share one namespace")
                if entity == "dashboards":
                    if any(widget.owner_id is not None for widget in definition.widgets):
                        raise BiConflict("Personal widgets must be saved separately from the system dashboard")
                    if definition.created_by is None:
                        definition.created_by = actor
                    saved = self._save_record(connection, entity, definition, expected_revision)
                    desired = {widget.id for widget in definition.widgets}
                    for existing in connection.execute("SELECT id FROM dashboard_widgets WHERE dashboard_id=?", (saved.id,)).fetchall():
                        if existing["id"] not in desired:
                            connection.execute("DELETE FROM dashboard_user_overrides WHERE widget_id=?", (existing["id"],))
                            connection.execute("DELETE FROM dashboard_widgets WHERE id=?", (existing["id"],))
                    saved.widgets = []
                    for widget in definition.widgets:
                        owned = connection.execute("SELECT dashboard_id FROM dashboard_widgets WHERE id=?", (widget.id,)).fetchone()
                        if owned is not None and owned["dashboard_id"] != saved.id:
                            raise BiConflict("A widget cannot be moved between dashboards")
                        widget = widget.model_copy(update={"dashboard_id": saved.id})
                        saved.widgets.append(self._save_record(connection, "widgets", widget, None))
                    connection.execute("DELETE FROM dashboard_assignments WHERE dashboard_id=?", (saved.id,))
                    for assignment in saved.assignments:
                        connection.execute("INSERT OR IGNORE INTO dashboard_assignments VALUES (?,?,?)", (saved.id, assignment.assignment_type, assignment.assignment_id))
                    return saved
                if entity == "user_access":
                    from app.bi.security import FIELD_ALIASES
                    if any(condition.field not in FIELD_ALIASES for condition in definition.data_scope.filters):
                        raise ValueError("Unknown data-scope field")
                    for role_id in definition.role_ids:
                        if not connection.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
                            raise BiNotFound("Assigned BI role was not found")
                if entity == "widgets" and definition.owner_id is not None:
                    raise BiConflict("Personal widgets must be saved separately from the system dashboard")
                if entity == "overrides":
                    self._owned_layout_widget(connection, definition.dashboard_id, definition.user_id, definition.widget_id)
                return self._save_record(connection, entity, definition, expected_revision)
        except sqlite3.IntegrityError as exc:
            raise BiConflict("Duplicate key or invalid BI relationship") from exc

    def delete(self, entity: str, entity_id: str, expected_revision: int | None = None):
        table = self._table(entity)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT revision FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                raise BiNotFound("BI definition was not found")
            if expected_revision is None or row["revision"] != expected_revision:
                raise BiConflict("Provide the current revision before deleting a definition")
            if entity == "widgets":
                connection.execute("DELETE FROM dashboard_user_overrides WHERE widget_id=?", (entity_id,))
            if entity == "weight_sets":
                if connection.execute("SELECT 1 FROM category_weights WHERE weight_set_id=?", (entity_id,)).fetchone():
                    raise BiConflict("Remove category weights before deleting their weight set")
                for item in connection.execute("SELECT definition_json FROM metric_definitions"):
                    metric = json.loads(item[0])
                    if metric.get("weight_set_id", "target_equivalent") == entity_id and (metric.get("aggregation") == "WEIGHTED_SUM" or metric.get("category_mode", "ALL") != "ALL"):
                        raise BiConflict("The weight set is used by a metric")
            connection.execute(f"DELETE FROM {table} WHERE id=?", (entity_id,))
            connection.execute("UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1")

    def configuration_revision(self) -> int:
        with self._connection() as connection:
            return connection.execute("SELECT configuration_revision FROM bi_state WHERE id=1").fetchone()[0]

    @staticmethod
    def _expire_cache_jobs(connection, now: float) -> int:
        return connection.execute("UPDATE analytics_cache_jobs SET status='failed',error_code='interrupted',lease_owner=NULL,lease_expires_at=NULL,updated_at=? WHERE status IN ('queued','running') AND COALESCE(lease_expires_at,0)<=?", (BiStore._now(), now)).rowcount

    def cleanup_cache_jobs(self) -> int:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._expire_cache_jobs(connection, time.time())

    def create_cache_job(self, request: dict, user_id: str | None, total_queries: int, *, lease_owner: str | None = None, lease_seconds: float = 90, max_runtime_seconds: float = 300, max_active: int = 2) -> dict:
        if not 1 <= total_queries <= 4 or not 1 <= max_active <= 2 or not 1 <= lease_seconds <= 300 or not 1 <= max_runtime_seconds <= 900:
            raise ValueError('Invalid cache warming reservation bounds')
        job_id = uuid.uuid4().hex
        now = self._now()
        instant = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_cache_jobs(connection, instant)
            active = connection.execute("SELECT COUNT(*) FROM analytics_cache_jobs WHERE status IN ('queued','running')").fetchone()[0]
            if active >= max_active:
                raise BiConflict('Cache warming is already busy; retry after a running job finishes')
            connection.execute("DELETE FROM analytics_cache_jobs WHERE status IN ('complete','failed') AND id NOT IN (SELECT id FROM analytics_cache_jobs ORDER BY created_at DESC LIMIT 50)")
            connection.execute("INSERT INTO analytics_cache_jobs (id,requested_by,request_json,status,completed_queries,total_queries,error_code,created_at,updated_at,lease_owner,lease_expires_at,deadline_at) VALUES (?,?,?,'queued',0,?,NULL,?,?,?,?,?)", (job_id, user_id, json.dumps(request), total_queries, now, now, lease_owner or uuid.uuid4().hex, instant + lease_seconds, instant + max_runtime_seconds))
        return self.cache_job(job_id)

    def renew_cache_job(self, job_id: str, lease_owner: str, lease_seconds: float = 90) -> bool:
        if not 1 <= lease_seconds <= 300:
            raise ValueError('Invalid cache job lease duration')
        instant = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_cache_jobs(connection, instant)
            changed = connection.execute("UPDATE analytics_cache_jobs SET lease_expires_at=?,updated_at=? WHERE id=? AND lease_owner=? AND status IN ('queued','running') AND lease_expires_at>?", (instant + lease_seconds, self._now(), job_id, lease_owner, instant)).rowcount
            return bool(changed)

    def update_cache_job(self, job_id: str, status: str, completed_queries: int, error_code: str | None = None, *, lease_owner: str | None = None) -> dict:
        if status not in {'running', 'complete', 'failed'} or not isinstance(completed_queries, int) or isinstance(completed_queries, bool):
            raise ValueError('Invalid cache job progress')
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_cache_jobs(connection, time.time())
            row = connection.execute("SELECT * FROM analytics_cache_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise BiNotFound('Cache job was not found')
            if row['status'] not in {'queued', 'running'} or (lease_owner is not None and row['lease_owner'] != lease_owner):
                raise BiConflict('Cache job reservation is no longer owned by this worker')
            if not row['completed_queries'] <= completed_queries <= row['total_queries'] or (status == 'complete' and completed_queries != row['total_queries']):
                raise ValueError('Invalid cache job progress')
            connection.execute("UPDATE analytics_cache_jobs SET status=?,completed_queries=?,error_code=?,updated_at=?,lease_owner=?,lease_expires_at=? WHERE id=?", (status, completed_queries, error_code, self._now(), row['lease_owner'] if status == 'running' else None, row['lease_expires_at'] if status == 'running' else None, job_id))
        return self.cache_job(job_id)

    def cache_job(self, job_id: str) -> dict:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_cache_jobs(connection, time.time())
            row = connection.execute("SELECT * FROM analytics_cache_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise BiNotFound('Cache job was not found')
            result = dict(row)
            result['request'] = json.loads(result.pop('request_json'))
            # Lease ownership is internal; the existing UI status shape stays stable.
            for field in ('lease_owner', 'lease_expires_at', 'deadline_at'):
                result.pop(field, None)
            return result

    @property
    def revision(self) -> int:
        return self.configuration_revision()

    def ensure_seed(self):
        """Initialize migrations and the read-only default role without touching users."""
        self._initialize()

    def seed_definitions(self, metrics=(), kpis=(), dashboards=()):
        """Import deterministic built-in definitions once, retaining administrator edits."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for entity, definitions in (("metrics", metrics), ("kpis", kpis)):
                table = self._table(entity)
                for definition in definitions:
                    if not connection.execute(f"SELECT 1 FROM {table} WHERE metric_key=?", (definition.key,)).fetchone():
                        self._save_record(connection, entity, definition, 0)
        for dashboard in dashboards:
            if self.find_by_key("dashboards", dashboard.slug) is None:
                self.save("dashboards", dashboard)

    def user_access(self, user_id: str) -> UserAccessDefinition:
        assigned = self.find_by_key("user_access", user_id)
        return assigned or UserAccessDefinition(id="unassigned:" + user_id, user_id=user_id, data_scope=DataScope(scope_type=self.default_viewer_scope))

    def overrides(self, dashboard_id: str, user_id: str):
        return [override for override in self.list("overrides") if override.dashboard_id == dashboard_id and override.user_id == user_id]

    def save_override(self, dashboard_id: str, user_id: str, widget_id: str, layout, expected_revision=0):
        with self._connection() as connection:
            self._owned_layout_widget(connection, dashboard_id, user_id, widget_id)
        existing = next((item for item in self.overrides(dashboard_id, user_id) if item.widget_id == widget_id), None)
        value = UserLayoutOverride(id=existing.id if existing else uuid.uuid4().hex, dashboard_id=dashboard_id, widget_id=widget_id, user_id=user_id, layout=layout, revision=expected_revision)
        return self.save("overrides", value)

    def overrides_snapshot(self, dashboard_id: str, user_id: str):
        with self._connection() as connection:
            if not connection.execute("SELECT 1 FROM dashboards WHERE id=?", (dashboard_id,)).fetchone():
                raise BiNotFound("Dashboard was not found")
            revision = connection.execute("SELECT revision FROM dashboard_override_revisions WHERE dashboard_id=? AND user_id=?", (dashboard_id, user_id)).fetchone()
            values = [UserLayoutOverride.model_validate_json(row["definition_json"]) for row in connection.execute("SELECT definition_json FROM dashboard_user_overrides WHERE dashboard_id=? AND user_id=? ORDER BY widget_id", (dashboard_id, user_id))]
            return {"revision": revision[0] if revision else 0, "overrides": values}

    def save_overrides(self, dashboard_id: str, user_id: str, overrides, expected_revision: int):
        values = [LayoutOverrideInput.model_validate(item.model_dump() if hasattr(item, "model_dump") else item) for item in overrides]
        if len({item.widget_id for item in values}) != len(values):
            raise BiConflict("Layout override widget IDs must be unique")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dashboard = connection.execute("SELECT * FROM dashboards WHERE id=?", (dashboard_id,)).fetchone()
            if dashboard is None:
                raise BiNotFound("Dashboard was not found")
            record = connection.execute("SELECT revision FROM dashboard_override_revisions WHERE dashboard_id=? AND user_id=?", (dashboard_id, user_id)).fetchone()
            revision = record[0] if record else 0
            if expected_revision != revision:
                raise BiConflict("Personal dashboard layout changed elsewhere; reload before saving")
            for value in values:
                widget = self._owned_layout_widget(connection, dashboard_id, user_id, value.widget_id)
                if widget.is_locked:
                    raise BiConflict("Locked widgets cannot have personal layout overrides")
            old = {item["widget_id"]: UserLayoutOverride.model_validate_json(item["definition_json"]) for item in connection.execute("SELECT widget_id,definition_json FROM dashboard_user_overrides WHERE dashboard_id=? AND user_id=?", (dashboard_id, user_id))}
            connection.execute("DELETE FROM dashboard_user_overrides WHERE dashboard_id=? AND user_id=?", (dashboard_id, user_id))
            saved = []
            for value in values:
                existing = old.get(value.widget_id)
                definition = UserLayoutOverride(id=existing.id if existing else uuid.uuid4().hex, dashboard_id=dashboard_id, widget_id=value.widget_id, user_id=user_id, layout=value.layout)
                saved.append(self._save_record(connection, "overrides", definition, 0))
            connection.execute("INSERT INTO dashboard_override_revisions VALUES (?,?,?) ON CONFLICT(dashboard_id,user_id) DO UPDATE SET revision=excluded.revision", (dashboard_id, user_id, revision + 1))
            connection.execute("UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1")
            return {"revision": revision + 1, "overrides": saved}

    @staticmethod
    def _owned_layout_widget(connection, dashboard_id, user_id, widget_id):
        row = connection.execute("SELECT definition_json FROM dashboard_widgets WHERE id=? AND dashboard_id=?", (widget_id, dashboard_id)).fetchone()
        if row is None:
            row = connection.execute("SELECT definition_json FROM dashboard_personal_widgets WHERE id=? AND dashboard_id=? AND user_id=?", (widget_id, dashboard_id, user_id)).fetchone()
        if row is None:
            raise BiNotFound("Layout widget does not belong to this dashboard and user")
        return WidgetDefinition.model_validate_json(row["definition_json"])

    def personal_widgets(self, dashboard_id: str, user_id: str):
        with self._connection() as connection:
            return [WidgetDefinition.model_validate_json(row["definition_json"]) for row in connection.execute("SELECT definition_json FROM dashboard_personal_widgets WHERE dashboard_id=? AND user_id=? ORDER BY created_at,id", (dashboard_id, user_id))]

    def get_personal_widget(self, dashboard_id: str, user_id: str, widget_id: str):
        with self._connection() as connection:
            row = connection.execute("SELECT definition_json FROM dashboard_personal_widgets WHERE id=? AND dashboard_id=? AND user_id=?", (widget_id, dashboard_id, user_id)).fetchone()
            if row is None:
                raise BiNotFound("Personal widget was not found")
            return WidgetDefinition.model_validate_json(row["definition_json"])

    def save_personal_widget(self, dashboard_id: str, user_id: str, widget, expected_revision: int | None = None):
        definition = WidgetDefinition.model_validate(widget.model_dump() if hasattr(widget, "model_dump") else widget)
        if not user_id:
            raise BiNotFound("A signed-in owner is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute("SELECT definition_json FROM dashboards WHERE id=?", (dashboard_id,)).fetchone()
            if parent is None:
                raise BiNotFound("Dashboard was not found")
            dashboard = DashboardDefinition.model_validate_json(parent["definition_json"])
            if dashboard.type != "DASHBOARD" or dashboard.layout_mode not in {"CUSTOMIZABLE", "FREE"}:
                raise BiConflict("This dashboard does not support personal widgets")
            if definition.data_scope != "USER_SCOPE" or definition.query.data_scope != "USER_SCOPE":
                raise BiConflict("Personal widgets must inherit the owner's data scope")
            if connection.execute("SELECT 1 FROM dashboard_widgets WHERE id=?", (definition.id,)).fetchone():
                raise BiConflict("Personal widgets require their own stable ID")
            existing = connection.execute("SELECT * FROM dashboard_personal_widgets WHERE id=?", (definition.id,)).fetchone()
            if existing and (existing["user_id"] != user_id or existing["dashboard_id"] != dashboard_id):
                raise BiNotFound("Personal widget was not found")
            revision = existing["revision"] if existing else 0
            expected = definition.revision if expected_revision is None else expected_revision
            if expected != revision:
                raise BiConflict("Personal widget changed elsewhere; reload before saving")
            now = self._now()
            saved = definition.model_copy(deep=True, update={"owner_id": user_id, "dashboard_id": dashboard_id, "revision": revision + 1, "created_at": existing["created_at"] if existing else now, "updated_at": now})
            connection.execute("INSERT INTO dashboard_personal_widgets VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,definition_json=excluded.definition_json,updated_at=excluded.updated_at", (saved.id, user_id, dashboard_id, saved.revision, saved.model_dump_json(by_alias=True), saved.created_at, saved.updated_at))
            connection.execute("UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1")
            return saved

    def delete_personal_widget(self, dashboard_id: str, user_id: str, widget_id: str, expected_revision: int):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT revision FROM dashboard_personal_widgets WHERE id=? AND dashboard_id=? AND user_id=?", (widget_id, dashboard_id, user_id)).fetchone()
            if row is None:
                raise BiNotFound("Personal widget was not found")
            if row["revision"] != expected_revision:
                raise BiConflict("Personal widget changed elsewhere; reload before deleting")
            connection.execute("DELETE FROM dashboard_user_overrides WHERE widget_id=? AND user_id=?", (widget_id, user_id))
            connection.execute("DELETE FROM dashboard_personal_widgets WHERE id=? AND user_id=?", (widget_id, user_id))
            connection.execute("UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1")


_stores: dict[str, BiStore] = {}
_stores_lock = threading.Lock()


def get_bi_store() -> BiStore:
    path = str(Path(os.environ.get("APP_BI_DB", "/app/data/app_bi.sqlite3")).expanduser().resolve())
    with _stores_lock:
        if path not in _stores:
            _stores[path] = BiStore(path)
        return _stores[path]
