"""Every legacy calculation receives the server scope, including derived calls."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.api.models import ComparisonRequest, FilterRequest, Metric
from app.bi.models import DataScope
from app.bi.security import Principal
from app.core.cache import cache
from app.core.kpi_configuration import KpiConfiguration
from app.services import analytics, comparison
from app.services.bi_legacy import current_principal, hierarchy_options, legacy_operation, pin_legacy_principal, scoped_filter, scoped_options


def filters(**values):
    return FilterRequest(date_from="2026-09-01", date_to="2026-09-07", **values)


class LegacyScopeTests(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.viewer = Principal("viewer", "viewer", frozenset({"dashboard.view", "metric.view", "comparison.view"}), DataScope(scope_type="DEPARTMENT", scope_ids=["1"]))
        self.admin = Principal("admin", "administrator", frozenset({"*"}), DataScope(scope_type="ALL"), "EXACT")
        snapshot = SimpleNamespace(fingerprint="legacy-test-config", revision=1, config=KpiConfiguration())
        for replacement in [patch("app.services.bi_legacy.get_settings", return_value=SimpleNamespace(active_source="clickhouse")), patch("app.services.analytics.get_settings", return_value=SimpleNamespace(active_source="clickhouse")), patch("app.services.comparison.get_settings", return_value=SimpleNamespace(active_source="clickhouse")), patch("app.services.analytics.current_kpi_snapshot", return_value=snapshot), patch("app.services.comparison.current_kpi_snapshot", return_value=snapshot)]:
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_none_and_disjoint_scopes_return_403_before_source_queries(self):
        none = replace(self.viewer, scope=DataScope(scope_type="NONE"))
        for principal, request in [(none, filters()), (self.viewer, filters(bbak=["2"]))]:
            with self.subTest(scope=principal.scope.scope_type), pin_legacy_principal(principal), patch("app.services.analytics._overview_uncached") as source:
                with self.assertRaises(HTTPException) as caught:
                    analytics.overview(request)
                self.assertEqual(caught.exception.status_code, 403)
                source.assert_not_called()

    def test_all_legacy_endpoints_receive_a_narrowed_copy(self):
        request = filters()
        for name, target in [("overview", "_overview_uncached"), ("timeline", "_timeline_uncached"), ("events", "_events_uncached"), ("geojson", "_geojson_uncached")]:
            with self.subTest(endpoint=name), patch.object(analytics, target, return_value=[]) as source, pin_legacy_principal(self.viewer):
                getattr(analytics, name)(request)
                narrowed = source.call_args.args[1]
                self.assertEqual(narrowed.bbak, ["1"])
                self.assertIsNot(narrowed, request)
        with patch("app.services.analytics.clickhouse_analytics.kpi_preview", return_value={}) as source, pin_legacy_principal(self.viewer):
            analytics.kpi_preview(request, KpiConfiguration())
            self.assertEqual(source.call_args.args[0].bbak, ["1"])
        self.assertEqual(request.bbak, [])

    def test_team_scope_requires_and_keeps_both_parent_contexts(self):
        with self.assertRaises(ValidationError):
            DataScope(scope_type="TEAM", scope_ids=["same-crew"])
        team = replace(self.viewer, scope=DataScope(scope_type="TEAM", scope_ids=["same-crew"], filters=[{"field": "department", "operator": "eq", "value": 1}, {"field": "group", "operator": "eq", "value": 11}]))
        with pin_legacy_principal(team):
            narrowed = scoped_filter(filters())
            self.assertEqual((narrowed.bbak, narrowed.rota, narrowed.group), (["1"], ["11"], ["same-crew"]))
            with self.assertRaises(HTTPException):
                scoped_filter(filters(rota=["21"]))

    def test_all_filters_are_anded_and_restricted_sources_fail_closed(self):
        constrained = replace(self.admin, scope=DataScope(scope_type="ALL", filters=[{"field": "department", "operator": "eq", "value": 1}, {"field": "category", "operator": "in", "value": ["A"]}]))
        with pin_legacy_principal(constrained):
            narrowed = scoped_filter(filters(category=["A", "B"]))
            self.assertEqual((narrowed.bbak, narrowed.category), (["1"], ["A"]))
            with self.assertRaises(HTTPException):
                scoped_filter(filters(category=["B"]))
        with patch("app.services.bi_legacy.get_settings", return_value=SimpleNamespace(active_source="mock")), pin_legacy_principal(constrained):
            with self.assertRaises(HTTPException) as caught:
                scoped_filter(filters())
            self.assertEqual(caught.exception.status_code, 403)

    def test_context_is_reset_after_errors_and_nested_operations(self):
        self.assertIsNone(current_principal())
        with pin_legacy_principal(self.admin):
            with self.assertRaises(RuntimeError):
                with pin_legacy_principal(self.viewer):
                    self.assertEqual(current_principal(), self.viewer)
                    raise RuntimeError("interrupted request")
            self.assertEqual(current_principal(), self.admin)
        self.assertIsNone(current_principal())
        with patch("app.services.bi_legacy.resolve_principal", return_value=self.viewer):
            with self.assertRaises(RuntimeError):
                legacy_operation(object(), lambda: (_ for _ in ()).throw(RuntimeError("interrupted operation")))
        self.assertIsNone(current_principal())

    def test_overview_cache_contains_narrowed_server_filters(self):
        observed = []

        def source(source_name, request, kpi):
            observed.append(list(request.bbak))
            return [Metric(key="flights", label="Flights", value=int(request.bbak[0]))]

        with patch("app.services.analytics._overview_uncached", side_effect=source):
            for principal, expected in [(self.viewer, 1), (replace(self.viewer, scope=DataScope(scope_type="DEPARTMENT", scope_ids=["2"])), 2), (self.viewer, 1)]:
                with pin_legacy_principal(principal):
                    self.assertEqual(analytics.overview(filters())[0].value, expected)
        self.assertEqual(observed, [["1"], ["2"]])

    def test_derived_comparison_unit_cannot_overwrite_scope(self):
        body = ComparisonRequest(mode="units", dimension="bbak", units=["1", "2"], filters=filters())
        with patch("app.services.comparison._group_labels", return_value={}), patch("app.services.analytics._overview_uncached", return_value=[Metric(key="flights", label="Flights", value=1)]) as source, pin_legacy_principal(self.viewer):
            with self.assertRaises(HTTPException) as caught:
                comparison.compare(body)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(source.call_count, 1)
            self.assertEqual(source.call_args.args[1].bbak, ["1"])

    def test_comparison_cache_cannot_cross_admin_viewer_or_unassigned_scopes(self):
        body = ComparisonRequest(mode="periods", filters=filters())

        def source(source_name, request, kpi):
            return [Metric(key="flights", label="Flights", value=int(request.bbak[0]) if request.bbak else 999)]

        with patch("app.services.analytics._overview_uncached", side_effect=source):
            with pin_legacy_principal(self.admin):
                self.assertEqual(comparison.compare(body).selections[0].metrics[0].value, 999)
            with pin_legacy_principal(self.viewer):
                self.assertEqual(comparison.compare(body).selections[0].metrics[0].value, 1)
            with pin_legacy_principal(replace(self.viewer, scope=DataScope(scope_type="NONE"))):
                with self.assertRaises(HTTPException) as caught:
                    comparison.compare(body)
                self.assertEqual(caught.exception.status_code, 403)

    def test_options_expose_only_scoped_fields_and_same_scope_cache(self):
        flights = [
            {"flight_id": "own", "bbak_id": 1, "bbak_title": "Own department", "rota_id": 11, "rota_title": "Own group", "crew": "own-crew", "device_type": "A", "device": "own-device", "main_purpose": "own-purpose", "direction": "own-direction"},
            {"flight_id": "peer", "bbak_id": 2, "bbak_title": "Peer department", "rota_id": 21, "rota_title": "Peer group", "crew": "peer-crew", "device_type": "B", "device": "peer-device", "main_purpose": "peer-purpose", "direction": "peer-direction"},
        ]
        events = [{"flight_id": "own", "target_class": "own-target", "result": "own-result", "units": "own-unit"}, {"flight_id": "peer", "target_class": "peer-target", "result": "peer-result", "units": "peer-unit"}]
        with patch("app.services.clickhouse_analytics.clickhouse_analytics._flight_where", return_value=object()), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", return_value=flights) as source, patch("app.services.clickhouse_analytics.clickhouse_analytics._events", return_value=events):
            result = scoped_options(self.viewer)
            self.assertEqual(result["bbak"], [{"id": 1, "title": "Own department"}])
            self.assertEqual(result["group"], ["own-crew"])
            self.assertEqual(result["rota"], ["11"])
            self.assertEqual(result["class_name"], ["own-target"])
            self.assertEqual(result["result"], ["own-result"])
            self.assertEqual(scoped_options(self.viewer), result)
            self.assertEqual(source.call_count, 1)
            peer = scoped_options(replace(self.viewer, scope=DataScope(scope_type="DEPARTMENT", scope_ids=["2"])))
            self.assertEqual(peer["bbak"], [{"id": 2, "title": "Peer department"}])
            self.assertEqual(source.call_count, 2)

    def test_scoped_rota_options_roundtrip_into_team_parent_intersection(self):
        team = replace(self.viewer, scope=DataScope(scope_type="TEAM", scope_ids=["same-crew"], filters=[{"field": "department", "operator": "eq", "value": 1}, {"field": "group", "operator": "eq", "value": 11}]))
        rows = [{"flight_id": "own", "bbak_id": 1, "rota_id": 11, "rota_title": "Group title", "crew": "same-crew"}]
        with patch("app.services.clickhouse_analytics.clickhouse_analytics._flight_where", return_value=object()), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", return_value=rows), patch("app.services.clickhouse_analytics.clickhouse_analytics._events", return_value=[]):
            options = scoped_options(team)
        with pin_legacy_principal(team):
            narrowed = scoped_filter(filters(rota=options["rota"]))
        self.assertEqual(narrowed.rota, ["11"])

    def test_event_custom_scope_restricts_both_legacy_option_dictionaries(self):
        viewer = replace(self.viewer, scope=DataScope(scope_type="CUSTOM", filters=[{"field": "result", "operator": "eq", "value": "positive"}]))
        rows = [
            {"flight_id": "own", "bbak_id": 1, "rota_id": 11, "crew": "own", "main_purpose": "own-purpose", "main_result": "own-main-result"},
            {"flight_id": "other", "bbak_id": 2, "rota_id": 21, "crew": "other", "main_purpose": "unrelated-purpose", "main_result": "unrelated-main-result"},
        ]
        events = [{"flight_id": "own", "result": "positive"}, {"flight_id": "other", "result": "negative"}]
        with patch("app.services.clickhouse_analytics.clickhouse_analytics._flight_where", return_value=object()), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", return_value=rows) as source, patch("app.services.clickhouse_analytics.clickhouse_analytics._events", return_value=events), pin_legacy_principal(viewer):
            options = analytics.options()
            self.assertEqual(options["purpose"], ["own-purpose"])
            self.assertEqual(options["result"], ["positive"])
            kpi = analytics.kpi_options()
            self.assertEqual(kpi, {"purposes": ["own-purpose"], "results": ["own-main-result"]})
            self.assertEqual(source.call_count, 1)

    def test_unsupported_owner_and_custom_scopes_do_not_query_option_rows(self):
        for scope in [DataScope(scope_type="SELF"), DataScope(scope_type="CUSTOM", filters=[{"field": "target_class", "operator": "contains", "value": "x"}]), DataScope(scope_type="NONE")]:
            viewer = replace(self.viewer, scope=scope)
            with self.subTest(scope=scope.scope_type), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows") as source, pin_legacy_principal(viewer):
                with self.assertRaises(HTTPException) as caught:
                    analytics.kpi_options()
                self.assertEqual(caught.exception.status_code, 403)
                source.assert_not_called()

    def test_hierarchy_nonorganization_filters_use_authorized_source_rows(self):
        full_rows = [
            {"flight_id": "own", "bbak_id": 1, "bbak_title": "Own department", "rota_id": 11, "rota_title": "Own group", "crew": "own-team", "device_type": "A"},
            {"flight_id": "other", "bbak_id": 1, "bbak_title": "Own department", "rota_id": 12, "rota_title": "Other group", "crew": "other-team", "device_type": "B"},
            {"flight_id": "outside", "bbak_id": 2, "bbak_title": "Outside department", "rota_id": 21, "rota_title": "Outside group", "crew": "outside-team", "device_type": "A"},
        ]
        hierarchy_rows = [{key: row[key] for key in ('bbak_id', 'bbak_title', 'rota_id', 'rota_title', 'crew')} for row in full_rows]
        events = [{"flight_id": "own", "result": "positive"}, {"flight_id": "other", "result": "negative"}, {"flight_id": "outside", "result": "positive"}]
        for condition in [{"field": "category", "operator": "eq", "value": "A"}, {"field": "result", "operator": "eq", "value": "positive"}]:
            viewer = replace(self.viewer, scope=DataScope(scope_type="DEPARTMENT", scope_ids=["1"], filters=[condition]))
            with self.subTest(field=condition["field"]), patch("app.services.bi_legacy._hierarchy_rows", return_value=hierarchy_rows), patch("app.services.clickhouse_analytics.clickhouse_analytics._flight_where", return_value=object()), patch("app.services.clickhouse_analytics.clickhouse_analytics._query_rows", return_value=full_rows), patch("app.services.clickhouse_analytics.clickhouse_analytics._events", return_value=events):
                result = hierarchy_options(viewer)
            self.assertEqual({row['id'] for row in result['departments']}, {'1'})
            self.assertEqual({row['id'] for row in result['groups']}, {'11'})
            self.assertEqual({row['id'] for row in result['teams']}, {'own-team'})

    def test_hierarchy_peer_names_require_feature_and_own_parent_context(self):
        rows = [
            {'bbak_id': 1, 'bbak_title': 'Own', 'rota_id': 11, 'rota_title': 'Own group', 'crew': 'own-team'},
            {'bbak_id': 1, 'bbak_title': 'Own', 'rota_id': 11, 'rota_title': 'Own group', 'crew': 'peer-team'},
            {'bbak_id': 1, 'bbak_title': 'Own', 'rota_id': 12, 'rota_title': 'Peer group', 'crew': 'other-group-team'},
            {'bbak_id': 2, 'bbak_title': 'Outside', 'rota_id': 21, 'rota_title': 'Outside group', 'crew': 'outside-team'},
        ]
        viewer = replace(self.viewer, scope=DataScope(scope_type='DEPARTMENT', scope_ids=['1'], filters=[{'field': 'category', 'operator': 'eq', 'value': 'A'}]))
        with patch('app.services.bi_legacy._hierarchy_rows', return_value=rows), patch('app.services.bi_legacy.scoped_option_rows', return_value=([rows[0]], [])):
            own = hierarchy_options(viewer)
            self.assertEqual({row['id'] for row in own['groups']}, {'11'})
            self.assertEqual({row['id'] for row in own['teams']}, {'own-team'})
            granted = replace(viewer, permissions=viewer.permissions | {'comparison.department', 'comparison.group', 'comparison.team'})
            permitted = hierarchy_options(granted)
            self.assertEqual({row['id'] for row in permitted['departments']}, {'1', '2'})
            self.assertEqual({row['id'] for row in permitted['groups']}, {'11', '12'})
            self.assertEqual({row['id'] for row in permitted['teams']}, {'own-team', 'peer-team'})
            missing_view = replace(granted, permissions=granted.permissions - {'comparison.view'})
            self.assertEqual(hierarchy_options(missing_view), own)
        for values in permitted.values():
            for item in values:
                self.assertTrue(set(item) <= {'id', 'title', 'department_id', 'group_id'})

    def test_administrator_hierarchy_stays_complete_for_configuration_editing(self):
        rows = [{'bbak_id': 1, 'rota_id': 11, 'crew': 'one'}, {'bbak_id': 2, 'rota_id': 21, 'crew': 'two'}]
        admin = replace(self.admin, scope=DataScope(scope_type='ALL', filters=[{'field': 'category', 'operator': 'eq', 'value': 'A'}]))
        with patch('app.services.bi_legacy._hierarchy_rows', return_value=rows), patch('app.services.bi_legacy.scoped_option_rows') as source:
            result = hierarchy_options(admin)
            source.assert_not_called()
        self.assertEqual({row['id'] for row in result['departments']}, {'1', '2'})
        self.assertEqual({row['id'] for row in result['groups']}, {'11', '21'})
        self.assertEqual({row['id'] for row in result['teams']}, {'one', 'two'})


if __name__ == "__main__":
    unittest.main()
