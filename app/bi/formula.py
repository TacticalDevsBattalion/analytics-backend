"""Bounded, declarative arithmetic for persisted metrics and KPIs.

Formulas are JSON trees, never Python or SQL. A reference is ``{"metric":
"flights"}``; an operation is ``{"op": "div", "args": [a, b]}``.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable


class FormulaError(ValueError):
    pass


MAX_DEPTH = 16
MAX_NODES = 128
MAX_ABSOLUTE = 1e15
_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$")
_ARITY = {
    "add": (2, 32), "sub": (2, 2), "mul": (2, 32), "div": (2, 2),
    "min": (2, 32), "max": (2, 32), "abs": (1, 1), "neg": (1, 1),
    "coalesce": (2, 32), "clamp": (3, 3), "normalize": (3, 3),
}


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormulaError("Formula operands must be finite numbers")
    result = float(value)
    if not math.isfinite(result) or abs(result) > MAX_ABSOLUTE:
        raise FormulaError("Formula number is outside the supported range")
    return result


def validate_formula(formula: Any) -> set[str]:
    """Validate the whole tree before evaluation and return its metric refs."""
    references: set[str] = set()
    count = 0

    def walk(node: Any, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            raise FormulaError("Formula exceeds the depth or node limit")
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            _number(node)
            return
        if not isinstance(node, dict):
            raise FormulaError("Formula nodes must be numbers or JSON objects")
        if set(node) == {"metric"}:
            key = node["metric"]
            if not isinstance(key, str) or not _REFERENCE.fullmatch(key):
                raise FormulaError("Invalid metric reference")
            references.add(key)
            return
        if set(node) == {"value"}:
            _number(node["value"])
            return
        op = node.get("op")
        if not isinstance(op, str) or op not in _ARITY:
            raise FormulaError("Unsupported formula operation")
        allowed = {"op", "args", "direction"} if op == "normalize" else {"op", "args"}
        if not set(node).issubset(allowed):
            raise FormulaError("Unexpected formula property")
        args = node.get("args")
        low, high = _ARITY[op]
        if not isinstance(args, list) or not low <= len(args) <= high:
            raise FormulaError(f"Invalid argument count for {op}")
        if op == "normalize" and node.get("direction", "HIGHER_IS_BETTER") not in {
            "HIGHER_IS_BETTER", "LOWER_IS_BETTER",
        }:
            raise FormulaError("Invalid normalization direction")
        for child in args:
            walk(child, depth + 1)

    walk(formula, 0)
    return references


def evaluate_formula(formula: Any, resolve: Callable[[str], float | None]) -> float | None:
    validate_formula(formula)

    def calculate(node: Any) -> float | None:
        if isinstance(node, (int, float)):
            return _number(node)
        if "metric" in node:
            return _number(resolve(node["metric"]))
        if "value" in node:
            return _number(node["value"])
        op = node["op"]
        values = [calculate(arg) for arg in node["args"]]
        if op == "coalesce":
            return next((value for value in values if value is not None), None)
        if any(value is None for value in values):
            return None
        args = [float(value) for value in values if value is not None]
        if op == "add":
            result = sum(args)
        elif op == "sub":
            result = args[0] - args[1]
        elif op == "mul":
            result = math.prod(args)
        elif op == "div":
            if args[1] == 0:
                return None
            result = args[0] / args[1]
        elif op == "min":
            result = min(args)
        elif op == "max":
            result = max(args)
        elif op == "abs":
            result = abs(args[0])
        elif op == "neg":
            result = -args[0]
        elif op == "clamp":
            if args[1] > args[2]:
                raise FormulaError("Clamp minimum exceeds maximum")
            result = min(args[2], max(args[1], args[0]))
        else:  # normalize
            if args[2] <= args[1]:
                raise FormulaError("Normalization maximum must exceed minimum")
            result = min(1.0, max(0.0, (args[0] - args[1]) / (args[2] - args[1])))
            if node.get("direction") == "LOWER_IS_BETTER":
                result = 1 - result
        return _number(result)

    return calculate(formula)
