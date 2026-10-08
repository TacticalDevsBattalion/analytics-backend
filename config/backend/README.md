# Backend configuration

The dashboard uses ClickHouse directly when `DATA_SOURCE=clickhouse`.

- `clickhouse.json` — table names, query settings, required logical fields.
- `clickhouse_fields.json` — logical field → candidate physical ClickHouse column names.
- `analytics.json` — business rules (day/night, excluded purposes, result labels, etc.).
- `cache.json` — API/cache TTL settings.
- `app.json` — FastAPI and CORS behavior.

Secrets/hostnames belong in `.env`, not in JSON.

`time_range`/`timeRange` is required for the ClickHouse flight list because it drives exact reporting windows and day/night classification.

For BBAK the backend resolves `bbak_id` + `bbak_title`. The UI displays the title but sends the stable ID back in filters.
