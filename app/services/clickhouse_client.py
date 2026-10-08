from __future__ import annotations

import json
import logging
import re
import hashlib
import time
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.cache import cache
from app.core.config import get_backend_config, get_settings

logger = logging.getLogger(__name__)
CONFIG = get_backend_config()
CH_CONFIG = CONFIG.clickhouse

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_identifier(value: str) -> str:
    """Quote a config-controlled ClickHouse identifier safely."""
    parts = value.split(".")
    if not parts or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise RuntimeError(f"Unsafe ClickHouse identifier in config: {value!r}")
    return ".".join(f"`{part}`" for part in parts)


class ClickHouseClient:
    def __init__(self) -> None:
        self._client: httpx.Client | None = None

    @property
    def settings(self):
        return get_settings()

    def _base_url(self) -> str:
        s = self.settings
        if s.clickhouse_url.strip():
            parsed = urlparse(s.clickhouse_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise RuntimeError("CLICKHOUSE_URL must be a valid http(s) URL")
            return s.clickhouse_url.rstrip("/")
        if not s.clickhouse_host.strip():
            raise RuntimeError("CLICKHOUSE_HOST or CLICKHOUSE_URL is required")
        scheme = "https" if s.clickhouse_secure else "http"
        return f"{scheme}://{s.clickhouse_host.strip()}:{s.clickhouse_port}"

    def _verify(self) -> bool | str:
        s = self.settings
        if not s.clickhouse_verify_ssl:
            return False
        return s.clickhouse_ca_file.strip() or True

    def _http(self) -> httpx.Client:
        if self._client is None:
            timeout = httpx.Timeout(
                CH_CONFIG.http.timeout_seconds,
                connect=CH_CONFIG.http.connect_timeout_seconds,
            )
            self._client = httpx.Client(
                base_url=self._base_url(),
                auth=(self.settings.clickhouse_user, self.settings.clickhouse_password),
                verify=self._verify(),
                timeout=timeout,
                headers={"Accept-Encoding": "gzip"},
            )
        return self._client

    def query(
        self,
        sql: str,
        *,
        parameters: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        query = sql.rstrip().rstrip(";")
        if " FORMAT " not in query.upper():
            query += " FORMAT JSONEachRow"

        params: dict[str, Any] = {
            "database": self.settings.clickhouse_database,
            "max_execution_time": CH_CONFIG.query.max_execution_time_seconds,
        }
        if CH_CONFIG.query.use_query_cache:
            params["use_query_cache"] = 1
        if settings:
            params.update(settings)
        for key, value in (parameters or {}).items():
            params[f"param_{key}"] = value

        started = time.perf_counter()
        try:
            response = self._http().post("/", params=params, content=query.encode("utf-8"))
        except httpx.HTTPError as exc:
            logger.warning('clickhouse_request status=connection_error error_type=%s db_ms=%.2f', type(exc).__name__, (time.perf_counter() - started) * 1000)
            raise RuntimeError('ClickHouse connection is unavailable') from None

        from app.core.observability import request_id
        logger.info('clickhouse_request query_id=%s sql_hash=%s status=%s db_ms=%.2f', request_id.get(), hashlib.sha256(query.encode()).hexdigest()[:16], response.status_code, (time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            match = re.search(r'\bCode:\s*(\d+)\b', response.text[:100])
            code = match.group(1) if match else 'unknown'
            logger.warning('clickhouse_request_failed query_id=%s status=%s error_code=%s', request_id.get(), response.status_code, code)
            raise RuntimeError(f'ClickHouse returned {response.status_code} (error code {code})') from None

        rows: list[dict[str, Any]] = []
        for line in response.text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    def scalar(self, sql: str, *, parameters: dict[str, Any] | None = None) -> Any:
        rows = self.query(sql, parameters=parameters)
        if not rows:
            return None
        return next(iter(rows[0].values()), None)

    def table_columns(self, table: str) -> set[str]:
        namespace = "clickhouse.schema"
        cfg = CONFIG.cache
        from app.services.analytics import semantic_signature

        def load() -> set[str]:
            rows = self.query(f"DESCRIBE TABLE {quote_identifier(table)}")
            return {str(row.get("name") or "") for row in rows if row.get("name")}

        return cache.get_or_load(
            namespace,
            {"source": semantic_signature(include_kpi=False), "database": self.settings.clickhouse_database, "table": table},
            load,
            ttl_seconds=cfg.ttl_seconds.clickhouse_schema,
            stale_if_error_seconds=0,
            stale_while_revalidate=False,
            dependency_tags=['dictionary'],
        )

    def status(self) -> dict[str, Any]:
        try:
            upstream = self._base_url()
            self.scalar("SELECT 1 AS ok")
            table = CH_CONFIG.tables["flight_list"]
            columns = self.table_columns(table)
            return {
                "source": "clickhouse",
                "label": "ClickHouse",
                "connected": True,
                "upstream": f"{upstream}/{self.settings.clickhouse_database}",
                "message": f"Connected; {table}: {len(columns)} columns",
            }
        except Exception as exc:
            try:
                upstream = self._base_url()
            except Exception:
                upstream = None
            return {
                "source": "clickhouse",
                "label": "ClickHouse",
                "connected": False,
                "upstream": f"{upstream}/{self.settings.clickhouse_database}" if upstream else None,
                "message": str(exc),
            }


clickhouse_client = ClickHouseClient()
