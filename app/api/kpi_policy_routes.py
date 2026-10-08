"""Administrative policy publishing and the same arithmetic used in missions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.bi_routes import no_store, operation, principal
from app.bi.engine import QueryEngine
from app.bi.kpi_policy import BUILTIN_METRICS, evaluate_policy, resolve_rule, validate_rule_metrics
from app.bi.kpi_policy_models import KpiPolicyRule, PolicySimulation
from app.bi.kpi_policy_store import PolicyStore
from app.bi.security import Principal, require_permission
from app.bi.store import get_bi_store
from app.core.user_accounts import check_request_origin

router = APIRouter(prefix='/v1/admin/kpi', dependencies=[Depends(no_store)])


@router.get('/rules')
def list_rules(include_versions: bool = False, user: Principal = Depends(principal)):
    require_permission(user, 'kpi.manage')
    return operation(lambda: {'rules': PolicyStore().list_rules(include_versions), 'builtin_metrics': [{'key': key, 'label': title} for key, title in BUILTIN_METRICS.items()]})


@router.post('/rules', response_model=KpiPolicyRule)
def publish_rule(body: KpiPolicyRule, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'kpi.manage')
    check_request_origin(request)
    def run():
        service = QueryEngine(get_bi_store())
        validate_rule_metrics(body, service, user, body.valid_from)
        return PolicyStore(service.store).publish(body, user.user_id)
    return operation(run)


@router.post('/simulate')
def simulate(body: PolicySimulation, request: Request, user: Principal = Depends(principal)):
    require_permission(user, 'kpi.manage')
    check_request_origin(request)
    def run():
        service = QueryEngine(get_bi_store())
        rule = resolve_rule([body.rule] if body.rule else PolicyStore(service.store).all_versions(), body.context, body.mode)
        if rule is None:
            return {'score': None, 'rule_set_id': None, 'rule_version': None, 'calculation_mode': 'NO_POLICY', 'components': []}
        validate_rule_metrics(rule, service, user, body.context.date)
        return evaluate_policy(rule, body.metrics, zone_metrics=body.zone_metrics)
    return operation(run)
