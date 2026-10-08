"""Bounded typed JSON for Redis values; never imports types named by cached data."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from functools import lru_cache
from typing import Any

from pydantic import BaseModel


@lru_cache
def _models():
    from app.api import models as api_models
    from app.bi import models as bi_models
    from app.core import kpi_configuration, user_accounts
    modules = (api_models, bi_models, kpi_configuration, user_accounts)
    return {f"{value.__module__}.{value.__qualname__}": value for module in modules for value in vars(module).values() if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel}


def _encode(value: Any, depth=0):
    if depth > 64:
        raise ValueError("Cache value nesting exceeds limits")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite cache values are unsupported")
        return value
    if isinstance(value, BaseModel):
        name = f"{type(value).__module__}.{type(value).__qualname__}"
        if name not in _models():
            raise ValueError("Cache model is not in the safe registry")
        return {"t": "model", "name": name, "v": _encode(value.model_dump(mode="python"), depth + 1)}
    if isinstance(value, (datetime, date, time)):
        return {"t": type(value).__name__, "v": value.isoformat()}
    if isinstance(value, dict):
        return {"t": "dict", "v": [[_encode(key, depth + 1), _encode(item, depth + 1)] for key, item in value.items()]}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = [_encode(item, depth + 1) for item in value]
        if isinstance(value, (set, frozenset)):
            values.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"t": type(value).__name__, "v": values}
    raise ValueError("Cache value type is unsupported")


def _decode(value, depth=0):
    if depth > 64:
        raise ValueError("Cache value nesting exceeds limits")
    if not isinstance(value, dict):
        if value is None or isinstance(value, (str, bool, int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Nonfinite cache values are unsupported")
            return value
        raise ValueError("Invalid cache value")
    kind = value.get("t")
    if kind == "model":
        model = _models().get(value.get("name"))
        if model is None:
            raise ValueError("Unknown cache model")
        return model.model_validate(_decode(value["v"], depth + 1))
    if kind in {"datetime", "date", "time"}:
        return {"datetime": datetime, "date": date, "time": time}[kind].fromisoformat(value["v"])
    if kind == "dict":
        return {_decode(pair[0], depth + 1): _decode(pair[1], depth + 1) for pair in value["v"]}
    if kind in {"list", "tuple", "set", "frozenset"}:
        items = [_decode(item, depth + 1) for item in value["v"]]
        return {"list": list, "tuple": tuple, "set": set, "frozenset": frozenset}[kind](items)
    raise ValueError("Unknown cache value tag")


def dumps(value: Any) -> bytes:
    return json.dumps({"version": 1, "value": _encode(value)}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def loads(payload: bytes | str, max_bytes: int):
    if len(payload.encode("utf-8") if isinstance(payload, str) else payload) > max_bytes:
        raise ValueError("Cache value exceeds the configured size")
    wrapper = json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Invalid number")))
    if not isinstance(wrapper, dict) or wrapper.get("version") != 1:
        raise ValueError("Unsupported cache format version")
    return _decode(wrapper["value"])
