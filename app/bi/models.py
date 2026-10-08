"""Validated public BI contracts. Clients can never submit SQL or executable code."""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class VersionedEntity(BiModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=128)
    revision: int = Field(default=0, ge=0)
    created_at: str | None = None
    updated_at: str | None = None


class QueryFilter(BiModel):
    field: str = Field(min_length=1, max_length=128)
    operator: Literal["eq", "neq", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "not_contains", "is_null", "is_not_null", "between"]
    value: Any = None

    @model_validator(mode="after")
    def valid_operand(self):
        if self.operator in {"in", "not_in", "between"} and not isinstance(self.value, list):
            raise ValueError("This filter requires a list of values")
        if self.operator == "between" and len(self.value) != 2:
            raise ValueError("Between requires exactly two values")
        if isinstance(self.value, (dict, set)) or (isinstance(self.value, list) and any(isinstance(item, (dict, list, set)) for item in self.value)):
            raise ValueError("Filter values must be scalars")
        return self


class DateRange(BiModel):
    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")

    @model_validator(mode="after")
    def ordered(self):
        if self.date_from > self.date_to:
            raise ValueError("Date range end precedes start")
        return self


class Dimension(BiModel):
    field: str = Field(min_length=1, max_length=128)
    granularity: Literal["day", "week", "month", "quarter", "year"] | None = None


class MetricReference(BiModel):
    key: str = Field(min_length=1, max_length=128)


class SortField(BiModel):
    field: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class QueryRequest(BiModel):
    metrics: list[MetricReference] = Field(default_factory=list, max_length=30)
    dimensions: list[Dimension] = Field(default_factory=list, max_length=5)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    date_range: DateRange | None = None
    sort: list[SortField] = Field(default_factory=list, max_length=10)
    top_n: int | None = Field(default=None, ge=1, le=10000)
    data_scope: Literal["USER_SCOPE", "GLOBAL", "FIXED_SCOPE"] = "USER_SCOPE"
    fixed_scope: DataScope | None = None

    @model_validator(mode="after")
    def unique_references(self):
        for values in ([item.key for item in self.metrics], [item.field for item in self.dimensions]):
            if len(values) != len(set(values)):
                raise ValueError("Metric and dimension references must be unique")
        return self


class MetricFormat(BiModel):
    type: Literal["number", "percent", "currency", "duration"] = "number"
    decimals: int = Field(default=0, ge=0, le=8)
    suffix: str = Field(default="", max_length=40)


Direction = Literal["HIGHER_IS_BETTER", "LOWER_IS_BETTER", "NEUTRAL"]
Precision = Literal["EXACT", "ROUND_1", "ROUND_5", "RANGE"]


class MetricDefinition(VersionedEntity):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    source: Literal["flights", "events", "ammunition", "lost_devices"] = "flights"
    aggregation: Literal["COUNT", "COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX", "RATIO", "FORMULA", "WEIGHTED_SUM"]
    field: str | None = Field(default=None, max_length=128)
    category_field: str = Field(default="category", min_length=1, max_length=128)
    weight_set_id: str = Field(default="target_equivalent", min_length=1, max_length=128)
    category_mode: Literal["ALL", "GENERAL", "SEPARATE"] = "ALL"
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    formula: dict[str, Any] | None = None
    numerator: str | None = Field(default=None, max_length=128)
    denominator: str | None = Field(default=None, max_length=128)
    format: MetricFormat = Field(default_factory=MetricFormat)
    direction: Direction = "NEUTRAL"
    percentage_difference: Literal["percentage_points", "relative_percent"] = "relative_percent"
    permissions: list[str] = Field(default_factory=list, max_length=100)
    is_active: bool = True

    @field_validator("aggregation", mode="before")
    @classmethod
    def normalize_aggregation(cls, value):
        return str(value).upper().replace(" ", "_")

    @model_validator(mode="after")
    def calculation_fields(self):
        if self.aggregation in {"COUNT_DISTINCT", "SUM", "AVG", "MIN", "MAX"} and not self.field:
            raise ValueError("This aggregation requires a field")
        if self.aggregation == "RATIO" and not (self.numerator and self.denominator):
            raise ValueError("Ratio requires numerator and denominator metric keys")
        if self.aggregation == "FORMULA" and self.formula is None:
            raise ValueError("Formula aggregation requires a declarative formula")
        return self


class KpiDefinition(VersionedEntity):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    metrics: list[MetricReference] = Field(default_factory=list, max_length=30)
    weights: dict[str, float] = Field(default_factory=dict, max_length=30)
    formula: dict[str, Any]
    normalization: Literal["NONE", "PER_OPERATION", "PER_100_OPERATIONS", "PERCENT_OF_TOTAL", "RATIO", "MIN_MAX"] = "NONE"
    normalization_metric: str | None = None
    minimum: float | None = Field(default=None, allow_inf_nan=False)
    maximum: float | None = Field(default=None, allow_inf_nan=False)
    format: MetricFormat = Field(default_factory=MetricFormat)
    direction: Direction = "HIGHER_IS_BETTER"
    permissions: list[str] = Field(default_factory=list, max_length=100)
    is_active: bool = True

    @model_validator(mode="after")
    def valid_normalization(self):
        if self.normalization == "MIN_MAX" and (self.minimum is None or self.maximum is None or self.maximum <= self.minimum):
            raise ValueError("Min/max normalization requires an increasing numeric range")
        if self.normalization in {"PER_OPERATION", "PER_100_OPERATIONS", "PERCENT_OF_TOTAL", "RATIO"} and not self.normalization_metric:
            raise ValueError("This normalization requires a denominator metric")
        return self


class WeightSet(VersionedEntity):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=4000)
    is_active: bool = True


class CategoryWeight(VersionedEntity):
    weight_set_id: str = Field(default="target_equivalent", min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=256)
    weight: float = Field(default=1, ge=0, le=1000000, allow_inf_nan=False)
    display_separately: bool = False
    include_in_weighted_total: bool = True
    include_in_general_total: bool = True
    excluded_from_general_total: bool = False


class DataScope(BiModel):
    scope_type: Literal["ALL", "DEPARTMENT", "GROUP", "TEAM", "SELF", "CUSTOM", "NONE"] = "NONE"
    scope_ids: list[str] = Field(default_factory=list, max_length=5000)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=100)

    @field_validator("scope_ids", mode="before")
    @classmethod
    def string_ids(cls, value):
        return [str(item).strip() for item in value]

    @model_validator(mode="after")
    def scope_requirements(self):
        if self.scope_type in {"DEPARTMENT", "GROUP", "TEAM"} and (not self.scope_ids or not all(self.scope_ids)):
            raise ValueError("Organizational scope requires stable IDs")
        if self.scope_type == "CUSTOM" and not self.filters:
            raise ValueError("Custom scope requires restrictive filters")
        if self.scope_type == "TEAM":
            # Crew is a source title, so the parent IDs make it a stable context.
            for fields in ({"department", "department_id", "bbak_id"}, {"group", "group_id", "rota_id"}):
                if not any(item.field in fields and item.operator in {"eq", "in"} and item.value is not None and item.value != [] for item in self.filters):
                    raise ValueError("Team scope requires department and group ID filters")
        return self


class RoleDefinition(VersionedEntity):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    title: str = Field(min_length=1, max_length=240)
    permissions: list[str] = Field(default_factory=list, max_length=500)


class UserAccessDefinition(VersionedEntity):
    user_id: str = Field(min_length=1, max_length=128)
    role_ids: list[str] = Field(default_factory=list, max_length=100)
    permissions: list[str] = Field(default_factory=list, max_length=500)
    denied_permissions: list[str] = Field(default_factory=list, max_length=500)
    data_scope: DataScope = Field(default_factory=DataScope)
    comparison_precision: Precision = "ROUND_5"

    @field_validator("data_scope", mode="before")
    @classmethod
    def absent_scope(cls, value):
        return {"scope_type": "NONE"} if value is None else value


class LayoutOverrideInput(BiModel):
    widget_id: str = Field(min_length=1, max_length=128)
    layout: WidgetLayout


class LayoutOverridesUpdate(BiModel):
    expected_revision: int = Field(ge=0)
    overrides: list[LayoutOverrideInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def distinct_widgets(self):
        keys = [item.widget_id for item in self.overrides]
        if len(keys) != len(set(keys)):
            raise ValueError("Layout override widget IDs must be unique")
        return self


class WidgetLayout(BiModel):
    x: int = Field(default=0, ge=0, le=11)
    y: int = Field(default=0, ge=0, le=10000)
    w: int = Field(default=6, ge=1, le=12)
    h: int = Field(default=4, ge=1, le=100)

    @model_validator(mode="after")
    def within_grid(self):
        if self.x + self.w > 12:
            raise ValueError("Widget extends beyond the 12-column grid")
        return self


class SeriesDefinition(BiModel):
    metric: str = Field(min_length=1, max_length=128)
    type: str = Field(default="bar", min_length=1, max_length=64)
    axis: Literal["left", "right"] = "left"
    name: str | None = Field(default=None, max_length=240)
    color: str | None = Field(default=None, max_length=40)
    stack: str | None = Field(default=None, max_length=64)


class VisualizationConfig(BiModel):
    type: str = Field(default="number", min_length=1, max_length=64)
    series: list[SeriesDefinition] = Field(default_factory=list, max_length=30)
    axes: dict[str, Any] = Field(default_factory=dict)
    legend: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    tooltip: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class WidgetPermissions(BiModel):
    required_permissions: list[str] = Field(default_factory=list, max_length=100)
    roles: list[str] = Field(default_factory=list, max_length=100)
    users: list[str] = Field(default_factory=list, max_length=100)


class WidgetInteraction(BiModel):
    click_action: Literal['NONE', 'CROSS_FILTER', 'DRILL_THROUGH'] = 'NONE'
    target_page: Literal['STATISTICS', 'DASHBOARD', 'MAP', 'TABLE'] = 'STATISTICS'
    target_section: Literal['overview', 'effectiveness', 'targets', 'losses', 'resources'] = 'overview'
    pass_date: bool = True
    pass_filters: bool = True


class WidgetVisibility(BiModel):
    conditions: list[QueryFilter] = Field(default_factory=list, max_length=20)
    show_when_all_departments: bool = False


class WidgetAppearance(BiModel):
    background: str | None = Field(default=None, max_length=40, pattern=r'^(#[0-9a-fA-F]{6}|transparent|default|navy|graphite|gradient)$')
    color: str | None = Field(default=None, pattern=r'^#[0-9a-fA-F]{6}$')
    opacity: float = Field(default=1, ge=0.2, le=1, allow_inf_nan=False)
    radius: int = Field(default=12, ge=0, le=40)


class WidgetDefinition(VersionedEntity):
    dashboard_id: str = Field(default="", max_length=128)
    owner_id: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=240)
    widget_type: str = Field(default="chart", min_length=1, max_length=64)
    layout: WidgetLayout = Field(default_factory=WidgetLayout)
    query: QueryRequest = Field(default_factory=QueryRequest)
    visualization: VisualizationConfig = Field(default_factory=VisualizationConfig)
    permissions: WidgetPermissions = Field(default_factory=WidgetPermissions)
    data_scope: Literal["GLOBAL", "USER_SCOPE", "FIXED_SCOPE"] = "USER_SCOPE"
    fixed_filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    inherit_global_filters: bool = True
    date_mode: Literal["INHERIT_GLOBAL_DATE", "USE_FIXED_DATE"] = "INHERIT_GLOBAL_DATE"
    is_locked: bool = False
    widget_kind: Literal['SYSTEM_WIDGET', 'USER_WIDGET'] = 'SYSTEM_WIDGET'
    builtin: Literal['summary', 'timeline', 'map', 'records', 'losses', 'average', 'category', 'purpose'] | None = None
    interaction: WidgetInteraction = Field(default_factory=WidgetInteraction)
    visibility: WidgetVisibility = Field(default_factory=WidgetVisibility)
    appearance: WidgetAppearance = Field(default_factory=WidgetAppearance)
    mandatory: bool = False
    movable: bool = True
    resizable: bool = True
    removable: bool = True
    section: Literal['overview', 'effectiveness', 'targets', 'losses', 'resources'] = 'overview'

    @model_validator(mode="after")
    def fixed_configuration(self):
        if self.data_scope == "FIXED_SCOPE" and not self.fixed_filters:
            raise ValueError("Fixed-scope widgets require fixed filters")
        if self.date_mode == "USE_FIXED_DATE" and self.query.date_range is None:
            raise ValueError("Fixed-date widgets require a date range")
        return self


class DashboardAssignment(BiModel):
    assignment_type: Literal["role", "user", "department", "group"]
    assignment_id: str = Field(min_length=1, max_length=128)


class DashboardDefinition(VersionedEntity):
    name: str = Field(min_length=1, max_length=240)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(default="", max_length=4000)
    type: Literal["DASHBOARD", "STATISTICS", "COMPARISON"] = "DASHBOARD"
    is_system: bool = True
    layout_mode: Literal["LOCKED", "LAYOUT_EDITABLE", "CUSTOMIZABLE", "FREE"] = "LOCKED"
    version: Literal[2] = 2
    widgets: list[WidgetDefinition] = Field(default_factory=list, max_length=100)
    assignments: list[DashboardAssignment] = Field(default_factory=list, max_length=1000)
    global_filters: list[QueryFilter] = Field(default_factory=list, max_length=100)
    created_by: str | None = None

    @model_validator(mode="after")
    def unique_widgets(self):
        ids = [widget.id for widget in self.widgets]
        if len(ids) != len(set(ids)):
            raise ValueError("Widget IDs must be unique")
        if self.type != "DASHBOARD" and self.layout_mode in {"CUSTOMIZABLE", "FREE"}:
            raise ValueError("Statistics and comparison keep a shared widget structure")
        return self


class UserLayoutOverride(VersionedEntity):
    dashboard_id: str
    widget_id: str
    user_id: str
    layout: WidgetLayout


class MetricResultMetadata(BiModel):
    key: str
    title: str
    format: MetricFormat = Field(default_factory=MetricFormat)
    direction: Direction = "NEUTRAL"


class QueryResult(BiModel):
    status: Literal["success", "empty", "error"] = "success"
    rows: list[dict[str, Any]] = Field(default_factory=list)
    metrics: list[MetricResultMetadata] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    message: str | None = None
    source: str = "clickhouse"
    total_rows: int = Field(default=0, ge=0)
    separate_rows: list[dict[str, Any]] = Field(default_factory=list)
    dimension_labels: dict[str, str] = Field(default_factory=dict)
    payload: Any = None


class BatchQueryRequest(BiModel):
    queries: list[QueryRequest] = Field(min_length=1, max_length=100)


class BiComparisonRequest(BiModel):
    level: Literal["DEPARTMENT", "GROUP", "TEAM", "CATEGORY", "BBAK", "CREW"]
    own_id: str = Field(min_length=1, max_length=128)
    peer_id: str | None = Field(default=None, min_length=1, max_length=128)
    context_department_id: str | None = None
    context_group_id: str | None = None
    context_category: str | None = Field(default=None, min_length=1, max_length=256)
    query: QueryRequest

    @model_validator(mode="after")
    def comparison_context(self):
        if self.own_id == self.peer_id:
            raise ValueError("Choose two different comparison areas")
        if self.level in {"GROUP", "TEAM"} and not self.context_department_id:
            raise ValueError("Group and team comparison requires department context")
        if self.level == "TEAM" and not self.context_group_id:
            raise ValueError("Team comparison requires group context")
        if self.level == "CREW" and not self.context_category:
            raise ValueError("Crew comparison requires department category context")
        return self


class BiComparisonResult(BiModel):
    level: str
    own_id: str
    peer_id: str | None = None
    precision: Precision
    rows: list[dict[str, Any]]
    raw_visible: bool
    status: Literal["success", "empty"] = "success"


ENTITY_MODELS = {
    "dashboards": DashboardDefinition,
    "widgets": WidgetDefinition,
    "metrics": MetricDefinition,
    "kpis": KpiDefinition,
    "category_weights": CategoryWeight,
    "weight_sets": WeightSet,
    "roles": RoleDefinition,
    "user_access": UserAccessDefinition,
    "overrides": UserLayoutOverride,
}

QueryRequest.model_rebuild()
LayoutOverrideInput.model_rebuild()
LayoutOverridesUpdate.model_rebuild()
