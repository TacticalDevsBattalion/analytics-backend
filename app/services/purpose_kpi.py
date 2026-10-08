"""Per-flight KPI arithmetic, with no expression evaluation or event counting."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.api.models import KpiPreviewResponse, PurposeKpiPreview
from app.core.kpi_configuration import KpiConfiguration, PurposeKpiRule, normalize_kpi_text


DEFAULT_RULE = PurposeKpiRule(purpose="Default")


class PurposeKpiCalculator:
    def __init__(self, config: KpiConfiguration):
        self.rules = {normalize_kpi_text(rule.purpose): rule for rule in config.purpose_rules}
        self.result_sets = {
            key: {normalize_kpi_text(value) for value in rule.successful_results}
            for key, rule in self.rules.items()
        }

    def rule(self, row: dict[str, Any]) -> PurposeKpiRule:
        return self.rules.get(normalize_kpi_text(row.get("main_purpose")), DEFAULT_RULE)

    def successful(self, row: dict[str, Any]) -> bool:
        key = normalize_kpi_text(row.get("main_purpose"))
        rule = self.rules.get(key, DEFAULT_RULE)
        if rule.success_mode == "source":
            return row.get("is_effective") is True
        return normalize_kpi_text(row.get("main_result")) in self.result_sets[key]

    def weights(self, row: dict[str, Any]) -> tuple[float, float]:
        rule = self.rule(row)
        numerator = rule.usefulness_percent / 100.0 * rule.coefficient if self.successful(row) else 0.0
        return numerator, rule.coefficient

    def preview(self, unique_flights: list[dict[str, Any]], source: str) -> KpiPreviewResponse:
        by_purpose: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, str] = {}
        for row in unique_flights:
            key = normalize_kpi_text(row.get("main_purpose"))
            labels.setdefault(key, str(row.get("main_purpose") or "").strip() or "Без мети")
            by_purpose[key].append(row)
        # Retain configured purposes with no flights in the selected window.
        for key, rule in self.rules.items():
            labels.setdefault(key, rule.purpose)
            by_purpose.setdefault(key, [])
        purposes = []
        for key, rows in by_purpose.items():
            rule = self.rules.get(key, DEFAULT_RULE)
            successful = sum(self.successful(row) for row in rows)
            numerator = successful * rule.usefulness_percent / 100.0 * rule.coefficient
            denominator = len(rows) * rule.coefficient
            purposes.append(PurposeKpiPreview(
                purpose=labels[key], flights=len(rows), successful_flights=successful,
                success_rate=round(successful / len(rows) * 100.0, 1) if rows else 0.0,
                usefulness_percent=rule.usefulness_percent, coefficient=rule.coefficient,
                usefulness_points=numerator, weighted_flights=denominator,
                weighted_efficiency=weighted_percentage(numerator, denominator),
                success_mode=rule.success_mode, successful_results=rule.successful_results,
            ))
        purposes.sort(key=lambda row: (-row.flights, row.purpose.casefold()))
        numerator = sum(row.usefulness_points for row in purposes)
        denominator = sum(row.weighted_flights for row in purposes)
        return KpiPreviewResponse(
            source=source, total_flights=len(unique_flights),
            successful_flights=sum(row.successful_flights for row in purposes),
            usefulness_points=numerator, weighted_flights=denominator,
            weighted_efficiency=weighted_percentage(numerator, denominator), purposes=purposes,
        )


def weighted_percentage(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator * 100.0, 1) if denominator > 0 else None
