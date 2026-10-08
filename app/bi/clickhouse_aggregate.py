"""Exact, parameterized pushdown for independently aggregatable flight counts.

Source joins, derived rules, averages, weighted metrics and unsupported scopes
return None and are evaluated by the shared reference engine instead.
"""
from __future__ import annotations

from datetime import date


class UnsupportedPushdown(Exception):
    pass


def aggregate_metric(request, principal, metric, *, daily=False):
    from app.bi.engine import ALIASES, SOURCE_FIELDS, QueryError, PRIMARY_FILTER_SOURCE
    from app.services.clickhouse_analytics import clickhouse_analytics as adapter, _SqlWhere
    from app.services.clickhouse_client import clickhouse_client, quote_identifier
    from app.core.config import get_backend_config

    if metric.get("source", "flights") != "flights" or metric.get("aggregation") not in {"COUNT", "COUNT_DISTINCT"} or metric.get("kind") == "kpi":
        return None
    if metric.get("category_mode", "ALL") != "ALL":
        return None  # Category policies are configured through versioned WeightSets.
    if any(condition["field"] in PRIMARY_FILTER_SOURCE for condition in request.get("filters", [])):
        return None
    where = _SqlWhere()
    params = where.params

    def logical(field):
        while field in ALIASES:
            field = ALIASES[field]
        if field not in SOURCE_FIELDS["flights"]:
            raise UnsupportedPushdown()
        return field

    def expression(field):
        key = logical(field)
        physical = adapter._physical("flight_list", key)
        if not physical:
            raise UnsupportedPushdown()
        quoted = quote_identifier(physical)
        if key == "is_effective":
            # Match the existing adapter's nullable bool normalization exactly.
            return f"if(isNull({quoted}), NULL, lowerUTF8(toString({quoted})) IN ('1', 'true'))"
        return quoted

    def parameter(value):
        if isinstance(value, float) or isinstance(value, (dict, list)):
            raise UnsupportedPushdown()
        name = where._name("v")
        params[name] = str(int(value)) if isinstance(value, bool) else str(value)
        return f"{{{name}:String}}"

    def condition(item, *, scope=False):
        field, operator, value = item["field"], item.get("operator", "eq"), item.get("value")
        # Scope aliases 'status'/'unit' have different legacy precedence. Only
        # direct flight fields and unambiguous organizational aliases push down.
        if scope and field not in SOURCE_FIELDS["flights"] | {"department", "department_id", "group", "group_id", "team", "team_id", "category", "type", "purpose", "asset"}:
            raise UnsupportedPushdown()
        expr = expression(field)
        if logical(field) == "is_effective" and operator in {"eq", "neq", "in", "not_in"}:
            values = value if operator in {"in", "not_in"} else [value]
            if any(v is not None and not isinstance(v, bool) for v in values):
                raise UnsupportedPushdown()
        if operator == "is_null":
            return f"isNull({expr})"
        if operator == "is_not_null":
            return f"isNotNull({expr})"
        if operator not in {"eq", "neq", "in", "not_in"}:
            raise UnsupportedPushdown()
        values = value if operator in {"in", "not_in"} else [value]
        clauses = [f"isNull({expr})" if v is None else f"toString({expr}) = {parameter(v)}" for v in values]
        joined = "(" + " OR ".join(clauses) + ")" if clauses else "0"
        if operator in {"neq", "not_in"}:
            return f"(isNotNull({expr}) AND NOT {joined})"
        return joined

    def scope_expression(scope):
        scope = scope.model_dump(mode="json") if hasattr(scope, "model_dump") else scope
        kind = scope.get("scope_type", "NONE")
        pieces = []
        if kind == "NONE":
            pieces.append("0")
        elif kind in {"DEPARTMENT", "GROUP", "TEAM"}:
            field = {"DEPARTMENT": "bbak_id", "GROUP": "rota_id", "TEAM": "crew"}[kind]
            pieces.append(condition({"field": field, "operator": "in", "value": scope.get("scope_ids", [])}, scope=True))
        elif kind not in {"ALL", "CUSTOM"}:
            raise UnsupportedPushdown()
        for item in scope.get("filters", []):
            pieces.append(condition(item, scope=True))
        return " AND ".join(pieces) if pieces else "1"

    try:
        date_field = adapter._physical("flight_list", "date", required=True)
        where.dates(date_field, date.fromisoformat(request["date_range"]["from"]), date.fromisoformat(request["date_range"]["to"]))
        if request.get("data_scope") == "GLOBAL":
            if not principal.has("analytics.global"):
                return None
        else:
            where.raw(scope_expression(principal.scope))
        if request.get("data_scope") == "FIXED_SCOPE":
            where.raw(scope_expression(request["fixed_scope"]))
        for item in request.get("filters", []):
            where.raw(condition(item))
        excluded = get_backend_config().analytics.excluded_analytics_purposes
        if excluded:
            purpose = expression("main_purpose")
            where.raw(f"(isNull({purpose}) OR NOT (" + " OR ".join(f"toString({purpose}) = {parameter(value)}" for value in excluded) + "))")
        metric_conditions = [condition(item) for item in metric.get("filters", [])]
        field = metric.get("field")
        field_expr = expression(field) if field else None
        if field_expr:
            metric_conditions.append(f"isNotNull({field_expr})")
        predicate = " AND ".join(metric_conditions) if metric_conditions else "1"
        aggregation = f"uniqExactIf({field_expr}, {predicate})" if metric["aggregation"] == "COUNT_DISTINCT" else f"countIf({predicate})"
        selections, group_by = [], []
        if daily:
            selections.append(f"toDate({quote_identifier(date_field)}) AS _day")
            group_by.append("_day")
        for index, dim in enumerate(request.get("dimensions", [])):
            expr = expression(dim["field"])
            granularity = dim.get("granularity")
            if logical(dim["field"]) == "date":
                if not granularity:
                    raise UnsupportedPushdown()
                functions = {"day": "toDate", "week": "toMonday", "month": "toStartOfMonth", "quarter": "toStartOfQuarter", "year": "toStartOfYear"}
                expr = f"{functions[granularity]}({expr})"
            alias = f"_dimension_{index}"
            selections.append(f"{expr} AS {quote_identifier(alias)}")
            group_by.append(quote_identifier(alias))
        selections.extend(["count() AS _population", f"{aggregation} AS _value"])
        sql = "SELECT " + ", ".join(selections) + f" FROM {quote_identifier(adapter._table('flight_list'))} WHERE {where.sql()}"
        if group_by:
            sql += " GROUP BY " + ", ".join(group_by) + " LIMIT 10001"
        rows = clickhouse_client.query(sql, parameters=params)
        if len(rows) > 10000:
            if daily:
                return None  # Extra daily states may exceed the final group limit.
            raise QueryError("Query creates too many groups; narrow filters or dates")
        dimensions = [dim["field"] for dim in request.get("dimensions", [])]
        output = []
        for row in rows:
            record = {field: row.get(f"_dimension_{index}") for index, field in enumerate(dimensions)}
            for field in dimensions:
                if logical(field) == "is_effective" and record[field] is not None:
                    record[field] = record[field] is True or record[field] == 1 or str(record[field]).lower() in {"1", "true"}
            if daily:
                record["_day"] = str(row.get("_day"))[:10]
            record[metric["key"]] = float(row.get("_value", 0)) if int(row.get("_population", 0)) else None
            output.append(record)
        if not rows and not dimensions and not daily:
            output = [{metric["key"]: None}]
        return {"rows": output}
    except UnsupportedPushdown:
        return None
