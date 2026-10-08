"""Declarative, immutable mission KPI policy contracts."""
from __future__ import annotations

import uuid
import math
from datetime import date
from typing import Any, Literal

from pydantic import Field, model_validator

from app.bi.formula import validate_formula
from app.bi.models import BiModel


class PolicyContext(BiModel):
    # category is the semantic Кафедра. Organizational ББАК stays separate.
    category: str | None = Field(default=None, min_length=1, max_length=256)
    purpose: str | None = Field(default=None, min_length=1, max_length=256)
    zone: str | None = Field(default=None, min_length=1, max_length=256)
    device_type: str | None = Field(default=None, min_length=1, max_length=256)
    bbak_id: str | None = Field(default=None, min_length=1, max_length=128)
    device: str | None = Field(default=None, min_length=1, max_length=256)


class ComponentNormalization(BiModel):
    minimum: float = Field(alias='min', allow_inf_nan=False)
    maximum: float = Field(alias='max', allow_inf_nan=False)
    direction: Literal['HIGHER_IS_BETTER', 'LOWER_IS_BETTER'] = 'HIGHER_IS_BETTER'

    @model_validator(mode='after')
    def increasing(self):
        if self.maximum <= self.minimum:
            raise ValueError('Normalization maximum must exceed minimum')
        return self


class ComponentThreshold(BiModel):
    operator: Literal['gt', 'gte', 'lt', 'lte', 'eq'] = 'gte'
    value: float = Field(allow_inf_nan=False)
    else_value: float = Field(default=0, allow_inf_nan=False)


class PolicyComponent(BiModel):
    key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z_][A-Za-z0-9_]*$')
    label: str = Field(min_length=1, max_length=256)
    metric_key: str | None = Field(default=None, min_length=1, max_length=128)
    formula: Any | None = None
    direction: Literal['positive', 'negative'] = 'positive'
    weight: float = Field(default=1, ge=0, le=1000000, allow_inf_nan=False)
    scale: float = Field(default=100, ge=0, le=1000000, allow_inf_nan=False)
    normalization: ComponentNormalization | None = None
    threshold: ComponentThreshold | None = None
    zone: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode='after')
    def expression(self):
        if (self.metric_key is None) == (self.formula is None):
            raise ValueError('A component requires exactly one metric or formula')
        if self.formula is not None:
            validate_formula(self.formula)
        return self


class KpiPolicyRule(BiModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    version: int = Field(default=0, ge=0)
    active: bool = True
    valid_from: date
    valid_to: date | None = None
    context: PolicyContext = Field(default_factory=PolicyContext)
    components: list[PolicyComponent] = Field(min_length=1, max_length=50)
    score_formula: Any | None = None

    @model_validator(mode='after')
    def valid_rule(self):
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError('Policy validity end precedes start')
        keys = {component.key for component in self.components}
        if len(keys) != len(self.components):
            raise ValueError('Policy component keys must be unique')
        if self.score_formula is not None and not validate_formula(self.score_formula).issubset(keys):
            raise ValueError('Score formula may reference only declared component contributions')
        return self


class SimulationContext(BiModel):
    category: str | None = Field(default=None, max_length=256)
    purpose: str | None = Field(default=None, max_length=256)
    zones: list[str] = Field(default_factory=list, max_length=100)
    device_type: str | None = Field(default=None, max_length=256)
    bbak_id: str | None = Field(default=None, max_length=128)
    device: str | None = Field(default=None, max_length=256)
    date: date


class PolicySimulation(BiModel):
    rule: KpiPolicyRule | None = None
    context: SimulationContext
    metrics: dict[str, float | None] = Field(default_factory=dict, max_length=100)
    zone_metrics: dict[str, dict[str, float | None]] = Field(default_factory=dict, max_length=100)
    mode: Literal['CURRENT_RULE', 'HISTORICAL_RULE'] = 'CURRENT_RULE'

    @model_validator(mode='after')
    def bounded_inputs(self):
        from app.core.kpi_configuration import normalize_kpi_text
        if len({normalize_kpi_text(zone) for zone in self.zone_metrics}) != len(self.zone_metrics):
            raise ValueError('Simulator zone labels must be unique after normalization')
        for metrics in [self.metrics, *self.zone_metrics.values()]:
            if len(metrics) > 100 or any(not key or len(key) > 128 for key in metrics):
                raise ValueError('Simulator metric references exceed supported limits')
            if any(value is not None and (not math.isfinite(value) or abs(value) > 1e15) for value in metrics.values()):
                raise ValueError('Simulator values must be bounded finite numbers')
        return self
