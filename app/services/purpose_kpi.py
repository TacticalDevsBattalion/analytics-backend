"""Per-flight KPI arithmetic, with no expression evaluation or event counting."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.api.models import KpiPreviewResponse, PurposeKpiPreview
from app.core.kpi_configuration import KpiConfiguration, PurposeKpiRule, normalize_kpi_text


DEFAULT_RULE = PurposeKpiRule(purpose="Default")
# The result name of a lost drone, in flight and event records.
LOSS_RESULT = normalize_kpi_text("Втрата борту")


def _flight_id(row: dict[str, Any]) -> str:
    return str(row.get("flight_id") or "").strip()


class PurposeKpiCalculator:
    """Whether a flight was successful under its purpose's rule.

    "events" rules and loss rules need the flight's records: pass the period's
    events and lost drones here, or a flight's own records to each call.
    """
    def __init__(self, config: KpiConfiguration, *, events: list[dict[str, Any]] | None = None, losses: list[dict[str, Any]] | None = None):
        self.rules = {normalize_kpi_text(rule.purpose): rule for rule in config.purpose_rules}
        self.result_sets = {
            key: {normalize_kpi_text(value) for value in rule.successful_results}
            for key, rule in self.rules.items()
        }
        self.event_results: dict[str, set[str]] = defaultdict(set)
        for event in events or []:
            if (identifier := _flight_id(event)):
                self.event_results[identifier].add(normalize_kpi_text(event.get("result")))
        self.lost_flights = {identifier for loss in losses or [] if (identifier := _flight_id(loss))}

    def rule(self, row: dict[str, Any]) -> PurposeKpiRule:
        return self.rules.get(normalize_kpi_text(row.get("main_purpose")), DEFAULT_RULE)

    def _results(self, row, events):
        if events is not None:
            return {normalize_kpi_text(event.get("result")) for event in events}
        return self.event_results.get(_flight_id(row), set())

    def lost(self, row: dict[str, Any], events=None, losses=None) -> bool:
        if losses is not None and losses:
            return True
        return (_flight_id(row) in self.lost_flights or normalize_kpi_text(row.get("main_result")) == LOSS_RESULT
                or LOSS_RESULT in self._results(row, events))

    def _achieved(self, row, events) -> bool:
        key = normalize_kpi_text(row.get("main_purpose"))
        rule = self.rules.get(key, DEFAULT_RULE)
        if rule.success_mode == "source":
            return row.get("is_effective") is True
        if rule.success_mode == "events":
            return bool(self._results(row, events) & self.result_sets[key])
        return normalize_kpi_text(row.get("main_result")) in self.result_sets[key]

    def successful(self, row: dict[str, Any], events=None, losses=None) -> bool:
        if not self._achieved(row, events):
            return False
        return not (self.rule(row).loss_rule == "unsuccessful" and self.lost(row, events, losses))

    def penalized(self, row: dict[str, Any], events=None, losses=None) -> bool:
        """An unsuccessful flight that also lost its drone, under a "penalty" rule."""
        return self.rule(row).loss_rule == "penalty" and not self._achieved(row, events) and self.lost(row, events, losses)

    def weights(self, row: dict[str, Any], events=None, losses=None) -> tuple[float, float]:
        rule = self.rule(row)
        points = rule.usefulness_percent / 100.0 * rule.coefficient
        if self.successful(row, events, losses):
            return points, rule.coefficient
        return (-points if self.penalized(row, events, losses) else 0.0), rule.coefficient

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
            penalized = sum(self.penalized(row) for row in rows)
            numerator = (successful - penalized) * rule.usefulness_percent / 100.0 * rule.coefficient
            denominator = len(rows) * rule.coefficient
            purposes.append(PurposeKpiPreview(
                purpose=labels[key], flights=len(rows), successful_flights=successful, penalized_flights=penalized,
                success_rate=round((successful - penalized) / len(rows) * 100.0, 1) if rows else 0.0,
                usefulness_percent=rule.usefulness_percent, coefficient=rule.coefficient,
                usefulness_points=numerator, weighted_flights=denominator,
                weighted_efficiency=weighted_percentage(numerator, denominator),
                success_mode=rule.success_mode, successful_results=rule.successful_results, loss_rule=rule.loss_rule,
            ))
        purposes.sort(key=lambda row: (-row.flights, row.purpose.casefold()))
        numerator = sum(row.usefulness_points for row in purposes)
        denominator = sum(row.weighted_flights for row in purposes)
        return KpiPreviewResponse(
            source=source, total_flights=len(unique_flights),
            successful_flights=sum(row.successful_flights for row in purposes),
            penalized_flights=sum(row.penalized_flights for row in purposes),
            usefulness_points=numerator, weighted_flights=denominator,
            weighted_efficiency=weighted_percentage(numerator, denominator), purposes=purposes,
        )


def weighted_percentage(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator * 100.0, 1) if denominator > 0 else None
