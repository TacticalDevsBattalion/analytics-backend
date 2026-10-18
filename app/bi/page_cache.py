"""Ready page responses: one entry per page, period, filters and user.

Reports already cache their results, but checking them means loading every event of
the period first, which is most of a repeated request (1.5–2 s for a month). A ready
page skips that, and is kept as compressed JSON: a month of the dashboard with its map
is 5 MB of JSON but 0.7 MB compressed, and is sent without being parsed again.
Entries follow the cache age bands of the period and are invalidated with the same
day, dictionary and metric tags as the reports they contain.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from datetime import timedelta

from fastapi import Response
from fastapi.encoders import jsonable_encoder

from app.bi.cache_policy import date_tags
from app.bi.engine import _canonical
from app.bi.widgets import query_page
from app.core.cache import cache

NAMESPACE = 'bi.page'
VERSION = 1


class _Uncacheable(Exception):
    def __init__(self, body: bytes):
        super().__init__('page has failed widgets')
        self.body = body


def page_signature(definition, body, user, service) -> tuple[dict, list[str]]:
    from app.bi.catalog import dimension_labels
    from app.bi.reports import _BUILTIN_VERSIONS
    definitions, weights = service._definitions()
    signature = {
        'version': VERSION,
        'builtin_versions': _BUILTIN_VERSIONS,
        'source': service.semantic_signature(),
        'principal': user.cache_key(),
        'page': hashlib.sha256(_canonical(definition.model_dump(mode='json')).encode()).hexdigest(),
        'query': body.model_dump(mode='json', by_alias=True),
        'definitions': hashlib.sha256(_canonical([definitions, weights, dimension_labels()]).encode()).hexdigest(),
    }
    start, end = body.date_range.date_from, body.date_range.date_to
    # Losses keep the flight they came from, which may be a day earlier.
    tags = [*date_tags(start - timedelta(days=1), end), 'dictionary', 'data:epoch']
    tags += [f'{item.get("kind", "metric")}:{key}' for key, item in definitions.items()]
    return signature, tags


def _json(result) -> bytes:
    return json.dumps(jsonable_encoder(result), ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()


def page_json(definition, body, user, service, *, serve_stale=True) -> bytes:
    """The page response as JSON, from the cache when it is there.

    With serve_stale a page just past its TTL is returned at once and recomputed in
    the background; without it (the prewarm pass) it is recomputed right away.
    """
    start, end = body.date_range.date_from, body.date_range.date_to
    ttl = service.cache_policy.ttl_for_range(start, end, service.today())
    if ttl <= 0:
        return _json(query_page(definition, body, user, service))
    signature, tags = page_signature(definition, body, user, service)

    def compute():
        content = _json(query_page(definition, body, user, service))
        # A widget that failed (source unavailable, timeout) must not stick for the TTL.
        if b'"status":"error"' in content and any(item['status'] == 'error' for item in json.loads(content)['results']):
            raise _Uncacheable(content)
        return base64.b64encode(zlib.compress(content, 6)).decode('ascii')

    try:
        # A recent period may be served for a moment past its TTL while it recomputes.
        packed = cache.get_or_load(NAMESPACE, _canonical(signature), compute, ttl_seconds=ttl, dependency_tags=tags, stale_while_revalidate=serve_stale)
    except _Uncacheable as exc:
        return exc.body
    return zlib.decompress(base64.b64decode(packed))


def cached_page_response(definition, body, user, service) -> Response:
    # A returned Response skips the router's no-store dependency, so it is set here.
    return Response(page_json(definition, body, user, service), media_type='application/json', headers={'Cache-Control': 'no-store'})
