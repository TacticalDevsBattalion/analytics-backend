"""Compare complete overview metrics using the existing cached analytics source."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.api.models import (
    ComparisonPeriod,
    ComparisonRequest,
    ComparisonResponse,
    ComparisonSelection,
    FilterOptions,
    FilterRequest,
)
from app.core.cache import cache
from app.core.config import get_backend_config, get_settings
from app.core.kpi_configuration import current_kpi_snapshot, pin_kpi_snapshot
from . import analytics

MAX_COMPARISON_PERIODS = 24
MAX_COMPARISON_CELLS = 72


class ComparisonValidationError(ValueError):
    """A comparison uses unsupported period boundaries or exceeds workload bounds."""


def previous_period_filters(filters: FilterRequest) -> FilterRequest:
    """Return the adjacent, equally long preceding inclusive period.

    Date-only requests keep whole calendar days and unset time filters. Explicit
    time boundaries use milliseconds, matching the ClickHouse DateTime64(3)
    query resolution. Subtracting one millisecond from the current start avoids
    querying the same boundary in both inclusive periods.
    """
    try:
        if filters.time_from is None and filters.time_to is None:
            duration = timedelta(days=(filters.date_to - filters.date_from).days + 1)
            updates = {
                "date_from": filters.date_from - duration,
                "date_to": filters.date_from - timedelta(days=1),
            }
        else:
            start = datetime.combine(filters.date_from, filters.time_from or time.min)
            end = datetime.combine(filters.date_to, filters.time_to or time.max)
            start = start.replace(microsecond=(start.microsecond // 1000) * 1000)
            end = end.replace(microsecond=(end.microsecond // 1000) * 1000)
            quantum = timedelta(milliseconds=1)
            duration = end - start + quantum
            previous_start = start - duration
            previous_end = start - quantum
            updates = {
                "date_from": previous_start.date(),
                "date_to": previous_end.date(),
                "time_from": previous_start.time(),
                "time_to": previous_end.time(),
            }
    except (OverflowError, TypeError) as exc:
        raise ComparisonValidationError(
            "The previous equal-duration period is outside the supported date range; "
            "choose a shorter current period or provide comparison_filters explicitly"
        ) from exc

    # A deep copy prevents derived selections from mutating the caller's filters.
    return filters.model_copy(deep=True, update=updates)


def _calendar_bounds(value: date, granularity: str) -> tuple[date, date]:
    if granularity == "day":
        return value, value
    if granularity == "week":
        start = value - timedelta(days=value.weekday())
        return start, start + timedelta(days=6)
    if granularity == "month":
        return (
            value.replace(day=1),
            value.replace(day=monthrange(value.year, value.month)[1]),
        )
    if granularity == "quarter":
        start_month = (value.month - 1) // 3 * 3 + 1
        end_month = start_month + 2
        return (
            value.replace(month=start_month, day=1),
            value.replace(month=end_month, day=monthrange(value.year, end_month)[1]),
        )
    raise ComparisonValidationError("Unsupported comparison period granularity")


def _period_label(start: date, end: date, granularity: str) -> str:
    if granularity == "day":
        return start.isoformat()
    if granularity == "month":
        return f"{start.year:04d}-{start.month:02d}"
    if granularity == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year:04d}"
    return f"{start.isoformat()} — {end.isoformat()}"


def _reporting_date() -> date:
    return datetime.now(ZoneInfo(get_backend_config().analytics.timezone)).date()


def comparison_periods(
    filters: FilterRequest, granularity: str, group_count: int, *, reporting_date: date | None = None,
) -> list[ComparisonPeriod]:
    """Split a continuous reporting window into bounded, clipped calendar buckets.

    Weeks start on Monday. Explicit times constrain only the first/last window
    endpoints; middle buckets cover full calendar days. A partial flag describes
    clipping and lets consumers distinguish incomplete periods from full ones.
    Validate every workload bound before querying even cached source options.
    """
    periods = []
    reporting_date = reporting_date or _reporting_date()
    cursor = filters.date_from
    while cursor <= filters.date_to:
        count = len(periods) + 1
        if count > MAX_COMPARISON_PERIODS or count * group_count > MAX_COMPARISON_CELLS:
            raise ComparisonValidationError(
                f"Comparison supports at most {MAX_COMPARISON_PERIODS} periods and "
                f"{MAX_COMPARISON_CELLS} group-period cells; narrow the date range, "
                "choose a larger calendar period, or select fewer groups"
            )
        try:
            natural_start, natural_end = _calendar_bounds(cursor, granularity)
        except OverflowError as exc:
            raise ComparisonValidationError(
                "The calendar period is outside the supported date range"
            ) from exc
        start = max(natural_start, filters.date_from)
        end = min(natural_end, filters.date_to)
        time_from = filters.time_from if start == filters.date_from else None
        time_to = filters.time_to if end == filters.date_to else None
        # Full-day endpoints use ClickHouse's millisecond query resolution.
        partial_time = (
            (time_from is not None and time_from > time.min)
            or (time_to is not None and time_to < time(23, 59, 59, 999000))
        )
        periods.append(ComparisonPeriod(
            id=f"{granularity}:{natural_start.isoformat()}",
            label=_period_label(natural_start, natural_end, granularity),
            date_from=start,
            date_to=end,
            time_from=time_from,
            time_to=time_to,
            is_partial=start != natural_start or end != natural_end or partial_time,
            is_current=natural_start <= reporting_date <= natural_end,
            is_future=natural_start > reporting_date,
        ))
        if end == filters.date_to:
            break
        cursor = end + timedelta(days=1)
    return periods


def _group_labels(request: ComparisonRequest) -> dict[str, str]:
    if request.dimension not in {"bbak", "battalion"}:
        return {}
    options = FilterOptions.model_validate(analytics.options())
    return {
        str(option.id): option.title.strip() or str(option.id)
        for option in getattr(options, request.dimension)
    }


def _compare_over_time(request: ComparisonRequest, reporting_date: date | None) -> ComparisonResponse:
    units = request.units or []
    periods = comparison_periods(
        request.filters, request.granularity, len(units), reporting_date=reporting_date,
    )
    labels = _group_labels(request)
    selections = []
    for period in periods:
        for unit in units:
            group_id = f"{request.dimension}:{unit}"
            filters = request.filters.model_copy(deep=True, update={
                request.dimension: [unit],
                "date_from": period.date_from,
                "date_to": period.date_to,
                "time_from": period.time_from,
                "time_to": period.time_to,
            })
            selections.append(ComparisonSelection(
                id=f"{period.id}/{group_id}",
                label=labels.get(unit, unit),
                group_id=group_id,
                period_id=period.id,
                filters=filters,
                metrics=analytics.overview(filters),
            ))
    return ComparisonResponse(
        mode=request.mode,
        dimension=request.dimension,
        granularity=request.granularity,
        baseline_id=selections[0].id,
        periods=periods,
        selections=selections,
    )


def _compare_uncached(request: ComparisonRequest, reporting_date: date | None) -> ComparisonResponse:
    if request.mode == "units_over_time":
        return _compare_over_time(request, reporting_date)
    if request.mode == "periods":
        previous = (
            request.comparison_filters.model_copy(deep=True)
            if request.comparison_filters is not None
            else previous_period_filters(request.filters)
        )
        selections = [
            ("current", "Поточний період", request.filters.model_copy(deep=True)),
            ("previous", "Період порівняння", previous),
        ]
        baseline_id = "previous"
    else:
        # ComparisonRequest already validates count, uniqueness and nonblank units.
        labels = _group_labels(request)
        selections = [
            (
                f"{request.dimension}:{unit}",
                labels.get(unit, unit),
                request.filters.model_copy(deep=True, update={request.dimension: [unit]}),
            )
            for unit in request.units or []
        ]
        baseline_id = selections[0][0]

    return ComparisonResponse(
        mode=request.mode,
        dimension=request.dimension,
        baseline_id=baseline_id,
        selections=[
            ComparisonSelection(
                id=selection_id,
                label=label,
                filters=filters,
                metrics=analytics.overview(filters),
            )
            for selection_id, label, filters in selections
        ],
    )


def compare(request: ComparisonRequest) -> ComparisonResponse:
    from .bi_legacy import current_principal
    principal = current_principal()
    reporting_date = _reporting_date() if request.mode == "units_over_time" else None
    # Enforce workload limits before creating potentially large day-tag lists.
    if request.mode == 'units_over_time':
        comparison_periods(request.filters, request.granularity, len(request.units or []), reporting_date=reporting_date)
    # Filter lists are normalized as sets by MemoryCache, but group order defines
    # the reference selection. A serialized request preserves that order. The
    # reporting date keeps current/future badges correct across local midnight.
    payload = {
        "source": get_settings().active_source,
        "request": request.model_dump_json(),
        "reporting_date": reporting_date.isoformat() if reporting_date else None,
        "data_access": principal.cache_key() if principal is not None else None,
    }
    snapshot = current_kpi_snapshot()
    payload['semantic'] = analytics.semantic_signature()
    payload["kpi"] = snapshot.fingerprint
    # Keep the metadata true even when an unrelated UI publication retains rules.
    payload["configuration_revision"] = snapshot.revision
    dependencies = analytics.cache_dependencies(request.filters, kpi=True)
    ttl = analytics.cache_ttl(request.filters, get_backend_config().cache.ttl_seconds.overview)
    if request.comparison_filters is not None:
        dependencies.extend(analytics.cache_dependencies(request.comparison_filters, kpi=True))
        ttl = analytics.cache_ttl(request.comparison_filters, ttl)
    elif request.mode == 'periods':
        dependencies.extend(analytics.cache_dependencies(previous_period_filters(request.filters), kpi=True))

    def load():
        with pin_kpi_snapshot(snapshot):
            response = _compare_uncached(request, reporting_date)
            return response.model_copy(update={
                "kpi_fingerprint": snapshot.fingerprint,
                "configuration_revision": snapshot.revision,
                "source": payload["source"],
                "kpi_configuration": snapshot.config.model_copy(deep=True),
            })
    return cache.get_or_load(
        "api.comparison",
        payload,
        load,
        ttl_seconds=ttl,
        stale_if_error_seconds=0,
        stale_while_revalidate=False,
        dependency_tags=dependencies,
    )
