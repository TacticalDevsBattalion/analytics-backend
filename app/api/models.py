from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from app.core.kpi_configuration import KpiConfiguration


class FilterRequest(BaseModel):
    date_from: date
    date_to: date
    time_from: time | None = None
    time_to: time | None = None
    direction: list[str] = Field(default_factory=list)
    unit: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    asset: list[str] = Field(default_factory=list)
    group: list[str] = Field(default_factory=list)
    bbak: list[str] = Field(default_factory=list)
    rota: list[str] = Field(default_factory=list)
    battalion: list[str] = Field(default_factory=list)
    purpose: list[str] = Field(default_factory=list)
    class_name: list[str] = Field(default_factory=list)
    result: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_period(self):
        if any(value is not None and value.tzinfo is not None for value in (self.time_from, self.time_to)):
            raise ValueError("time filters must use local time without timezone offsets")
        start = datetime.combine(self.date_from, self.time_from or time.min)
        end = datetime.combine(self.date_to, self.time_to or time.max)

        if start > end:
            raise ValueError("date/time range end must not be earlier than start")

        return self


class MetricBreakdownItem(BaseModel):
    key: str
    label: str
    value: float | None
    unit: str | None = None
    children: list["MetricBreakdownItem"] = Field(default_factory=list)
    numerator: float | None = None
    denominator: float | None = None


class Metric(BaseModel):
    key: str
    label: str
    value: float | None
    unit: str | None = None
    delta: float | None = None
    primary_label: str | None = None
    secondary_label: str | None = None
    secondary_value: float | None = None
    breakdown_title: str | None = None
    breakdown: list[MetricBreakdownItem] = Field(default_factory=list)
    calculation_fingerprint: str | None = None
    numerator: float | None = None
    denominator: float | None = None


class KpiPreviewRequest(BaseModel):
    model_config = {"extra": "forbid"}
    filters: FilterRequest
    kpi: KpiConfiguration


class PurposeKpiPreview(BaseModel):
    purpose: str
    flights: int
    successful_flights: int
    success_rate: float
    usefulness_percent: float
    coefficient: float
    usefulness_points: float
    weighted_flights: float
    weighted_efficiency: float | None
    success_mode: Literal["source", "results"]
    successful_results: list[str]


class KpiPreviewResponse(BaseModel):
    source: str
    total_flights: int
    successful_flights: int
    usefulness_points: float
    weighted_flights: float
    weighted_efficiency: float | None
    purposes: list[PurposeKpiPreview]


class KpiOptions(BaseModel):
    purposes: list[str]
    results: list[str]


class ComparisonRequest(BaseModel):
    mode: Literal["periods", "units", "units_over_time"]
    dimension: Literal["unit", "bbak", "rota", "battalion"] = "unit"
    granularity: Literal["day", "week", "month", "quarter"] = "week"
    filters: FilterRequest
    comparison_filters: FilterRequest | None = None
    units: list[str] | None = Field(default=None, min_length=2, max_length=6)

    @field_validator("units")
    @classmethod
    def validate_units(cls, values: list[str] | None):
        if values is None:
            return None
        cleaned = [value.strip() for value in values]
        if not all(cleaned):
            raise ValueError("comparison units must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("comparison units must be unique")
        return cleaned

    @model_validator(mode="after")
    def validate_mode(self):
        if self.mode == "periods" and self.units is not None:
            raise ValueError("units are only supported in unit comparison mode")
        if self.mode in {"units", "units_over_time"}:
            if self.units is None:
                raise ValueError("unit comparison requires between 2 and 6 units")
            if self.comparison_filters is not None:
                raise ValueError("comparison_filters are only supported in period comparison mode")
            if self.dimension in {"bbak", "battalion"}:
                try:
                    ids = [int(value) for value in self.units]
                except ValueError as exc:
                    raise ValueError("organizational comparisons require stable numeric IDs") from exc
                if any(value < -(2**63) or value > 2**63 - 1 for value in ids):
                    raise ValueError("organizational comparison IDs must fit a signed 64-bit integer")
                self.units = [str(value) for value in ids]
                if len(set(self.units)) != len(self.units):
                    raise ValueError("comparison units must be unique")
        return self


class ComparisonSelection(BaseModel):
    id: str
    label: str
    filters: FilterRequest
    metrics: list[Metric]
    group_id: str | None = None
    period_id: str | None = None


class ComparisonPeriod(BaseModel):
    id: str
    label: str
    date_from: date
    date_to: date
    time_from: time | None = None
    time_to: time | None = None
    is_partial: bool
    is_current: bool = False
    is_future: bool = False


class ComparisonResponse(BaseModel):
    mode: Literal["periods", "units", "units_over_time"]
    dimension: Literal["unit", "bbak", "rota", "battalion"] = "unit"
    granularity: Literal["day", "week", "month", "quarter"] | None = None
    baseline_id: str
    selections: list[ComparisonSelection]
    periods: list[ComparisonPeriod] = Field(default_factory=list)
    kpi_fingerprint: str | None = None
    configuration_revision: int | None = None
    source: Literal["mock", "clickhouse"] | None = None
    kpi_configuration: KpiConfiguration | None = None


class TimelinePoint(BaseModel):
    date: date
    total: int
    effective: int
    day: int = 0
    night: int = 0
    day_effective: int = 0
    night_effective: int = 0
    day_efficiency: float = 0.0
    night_efficiency: float = 0.0


class EventRow(BaseModel):
    id: str
    timestamp: str
    direction: str
    unit: str
    category: str
    asset: str
    group: str
    purpose: str
    class_name: str
    result: str
    grid_ref: str
    lat: float
    lon: float


class FilterIdTitleOption(BaseModel):
    id: int
    title: str


class FilterOptions(BaseModel):
    direction: list[str] = Field(default_factory=list)
    unit: list[str] = Field(default_factory=list)
    category: list[str] = Field(default_factory=list)
    asset: list[str] = Field(default_factory=list)
    group: list[str] = Field(default_factory=list)
    # Organizational filters use stable IDs while the UI displays titles.
    bbak: list[FilterIdTitleOption] = Field(default_factory=list)
    rota: list[str] = Field(default_factory=list)
    battalion: list[FilterIdTitleOption] = Field(default_factory=list)
    purpose: list[str] = Field(default_factory=list)
    class_name: list[str] = Field(default_factory=list)
    result: list[str] = Field(default_factory=list)


class SourceStatus(BaseModel):
    source: Literal['mock', 'clickhouse']
    label: str
    connected: bool
    upstream: str | None = None
    message: str


class HealthResponse(BaseModel):
    status: Literal['ok']


class GeoPointGeometry(BaseModel):
    type: Literal['Point']
    coordinates: tuple[float, float]


class GeoFeatureProperties(BaseModel):
    id: str
    result: str
    unit: str
    category: str
    asset: str | None = None
    grid_ref: str
    timestamp: str | None = None
    purpose: str | None = None
    class_name: str | None = None
    # Keep compatibility with upstream APIs that may attach extra read-only metadata.
    model_config = {"extra": "allow"}


class GeoFeature(BaseModel):
    type: Literal['Feature']
    geometry: GeoPointGeometry
    properties: GeoFeatureProperties


class GeoFeatureCollection(BaseModel):
    type: Literal['FeatureCollection']
    features: list[GeoFeature]
