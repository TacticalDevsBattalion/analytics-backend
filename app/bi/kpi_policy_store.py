"""Append-only policy publication in the existing BI database."""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict, defaultdict
from datetime import date, timedelta

from app.bi.kpi_policy_models import KpiPolicyRule
from app.bi.store import BiConflict, get_bi_store
from app.core.kpi_configuration import normalize_kpi_text


def specificity(rule: KpiPolicyRule) -> tuple[int, ...]:
    context = rule.context
    return tuple(int(getattr(context, field) is not None) for field in ('category', 'purpose', 'zone', 'device_type', 'bbak_id', 'device'))


def contexts_overlap(a, b):
    return all(getattr(a, field) is None or getattr(b, field) is None or normalize_kpi_text(getattr(a, field)) == normalize_kpi_text(getattr(b, field)) for field in type(a).model_fields)


def effective_intervals(rules):
    groups = defaultdict(list)
    for rule in rules:
        groups[rule.id].append(rule)
    for versions in groups.values():
        versions.sort(key=lambda rule: (rule.valid_from, rule.version))
        for index, rule in enumerate(versions):
            upper = rule.valid_to or date.max
            if index + 1 < len(versions):
                if versions[index + 1].valid_from <= rule.valid_from:
                    continue
                upper = min(upper, versions[index + 1].valid_from - timedelta(days=1))
            if upper >= rule.valid_from:
                yield rule, upper


class PolicyStore:
    def __init__(self, store=None):
        self.store = store or get_bi_store()

    def all_versions(self):
        with self.store._connection() as connection:
            return [KpiPolicyRule.model_validate_json(row[0]) for row in connection.execute('SELECT definition_json FROM kpi_policy_versions ORDER BY rule_set_id,version')]

    def list_rules(self, include_versions=False):
        rules = self.all_versions()
        if include_versions:
            return rules
        latest = {}
        for rule in rules:
            latest[rule.id] = rule
        return list(latest.values())

    def publish(self, definition: KpiPolicyRule, actor=None):
        rule = KpiPolicyRule.model_validate(definition.model_dump())
        with self.store._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            current = connection.execute('SELECT MAX(version) FROM kpi_policy_versions WHERE rule_set_id=?', (rule.id,)).fetchone()[0] or 0
            if rule.version != current:
                raise BiConflict('KPI policy changed elsewhere; reload before publishing')
            previous = connection.execute('SELECT valid_from FROM kpi_policy_versions WHERE rule_set_id=? ORDER BY version DESC LIMIT 1', (rule.id,)).fetchone()
            if previous and rule.valid_from.isoformat() < previous[0]:
                raise BiConflict('A new policy version cannot precede the prior version validity start')
            others = [KpiPolicyRule.model_validate_json(row[0]) for row in connection.execute('SELECT definition_json FROM kpi_policy_versions WHERE rule_set_id<>?', (rule.id,))]
            if rule.active:
                for other, upper in effective_intervals(others):
                    if not other.active or specificity(rule) != specificity(other) or not contexts_overlap(rule.context, other.context):
                        continue
                    if rule.valid_from <= upper and other.valid_from <= (rule.valid_to or date.max):
                        raise BiConflict('Equal-specificity KPI policies overlap in context and validity')
            if len(others) >= 10000:
                raise BiConflict('KPI policy history exceeds the supported limit')
            published = rule.model_copy(deep=True, update={'version': current + 1})
            connection.execute('INSERT INTO kpi_policy_versions VALUES (?,?,?,?,?,?,?)', (published.id, published.version, published.model_dump_json(by_alias=True), published.valid_from.isoformat(), published.valid_to.isoformat() if published.valid_to else None, self.store._now(), actor))
            connection.execute('UPDATE bi_state SET configuration_revision=configuration_revision+1 WHERE id=1')
            return published


_fingerprints = OrderedDict()
_fingerprint_lock = threading.Lock()


def policy_fingerprint(store=None) -> str:
    store = store or get_bi_store()
    revision = store.configuration_revision()
    identity = (str(store.path.resolve()), revision)
    with _fingerprint_lock:
        if identity in _fingerprints:
            _fingerprints.move_to_end(identity)
            return _fingerprints[identity]
    # Revision is a memoization key; unrelated dashboard/metric edits must not
    # alter the digest of unchanged published policy semantics.
    digest = hashlib.sha256(b'policy-v1|')
    with store._connection() as connection:
        for row in connection.execute('SELECT definition_json FROM kpi_policy_versions ORDER BY rule_set_id,version'):
            digest.update(row[0].encode())
            digest.update(b'\n')
    result = digest.hexdigest()
    with _fingerprint_lock:
        _fingerprints[identity] = result
        while len(_fingerprints) > 16:
            _fingerprints.popitem(last=False)
    return result
