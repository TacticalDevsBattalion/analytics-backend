"""Safe, declarative per-purpose KPI rules and their request-level snapshot."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


def normalize_kpi_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


KpiText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class KpiDataUnavailable(ValueError):
    """The selected calculation requires a field absent from the source schema."""


class PurposeKpiRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: KpiText
    success_mode: Literal["source", "results"] = "source"
    successful_results: list[KpiText] = Field(default_factory=list, max_length=500)
    usefulness_percent: float = Field(default=100, ge=0, le=100, allow_inf_nan=False, strict=True)
    coefficient: float = Field(default=1, ge=0, le=100, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def validate_results(self):
        normalized = [normalize_kpi_text(value) for value in self.successful_results]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Successful results must be unique after whitespace and case normalization")
        if self.success_mode == "results" and not self.successful_results:
            raise ValueError("Choose at least one successful flight result")
        if self.success_mode == "source" and self.successful_results:
            raise ValueError("Source success mode does not use selected results")
        return self


class KpiConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose_rules: list[PurposeKpiRule] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_purposes(self):
        purposes = [normalize_kpi_text(rule.purpose) for rule in self.purpose_rules]
        if len(purposes) != len(set(purposes)):
            raise ValueError("Each individual flight purpose may have only one KPI rule")
        return self


def kpi_fingerprint(config: KpiConfiguration) -> str:
    rules = [{
        "purpose": normalize_kpi_text(rule.purpose),
        "success_mode": rule.success_mode,
        "successful_results": sorted(normalize_kpi_text(value) for value in rule.successful_results),
        "usefulness_percent": float(rule.usefulness_percent),
        "coefficient": float(rule.coefficient),
    } for rule in config.purpose_rules]
    rules.sort(key=lambda row: row["purpose"])
    encoded = json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KpiSnapshot:
    config: KpiConfiguration
    fingerprint: str
    revision: int


_pinned_snapshot: ContextVar[KpiSnapshot | None] = ContextVar("kpi_snapshot", default=None)


def current_kpi_snapshot() -> KpiSnapshot:
    pinned = _pinned_snapshot.get()
    if pinned is not None:
        return pinned
    # A local import avoids a configuration model / analytics import cycle.
    from app.core.app_configuration import get_configuration_store
    snapshot = get_configuration_store().snapshot()
    config = snapshot.config.kpi.model_copy(deep=True)
    return KpiSnapshot(config, kpi_fingerprint(config), snapshot.revision)


@contextmanager
def pin_kpi_snapshot(snapshot: KpiSnapshot | None = None) -> Iterator[KpiSnapshot]:
    pinned = snapshot or current_kpi_snapshot()
    token = _pinned_snapshot.set(pinned)
    try:
        yield pinned
    finally:
        _pinned_snapshot.reset(token)
