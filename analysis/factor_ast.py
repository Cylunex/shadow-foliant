"""Bounded factor DSL. No eval/exec/import/attributes, callbacks or Python code."""
from __future__ import annotations
import math
from application.results import payload_hash

OPS = {"field": 0, "constant": 0, "add": 2, "sub": 2, "mul": 2,
       "div": 2, "lag": 1, "mean": 1, "abs": 1}


def validate_ast(node, *, allowed_fields, depth=0, budget=None):
    budget = budget if budget is not None else [64]
    budget[0] -= 1
    if budget[0] < 0 or depth > 8 or not isinstance(node, dict):
        raise ValueError("factor_complexity_limit")
    op = node.get("op")
    if op not in OPS:
        raise ValueError("factor_operation_not_allowed")
    allowed_keys = {"op", "args"} | ({"name"} if op == "field" else {"value"} if op == "constant" else {"window"} if op in {"lag", "mean"} else set())
    if set(node) - allowed_keys:
        raise ValueError("factor_unknown_attribute")
    args = node.get("args", [])
    if not isinstance(args, list) or len(args) != OPS[op]:
        raise ValueError("factor_arity_invalid")
    if op == "field" and node.get("name") not in allowed_fields:
        raise ValueError("factor_field_not_allowed")
    if op == "constant":
        v = node.get("value")
        if type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e9:
            raise ValueError("factor_constant_invalid")
    if op in {"lag", "mean"} and (type(node.get("window")) is not int or not 1 <= node["window"] <= 250):
        raise ValueError("factor_window_invalid")
    for child in args:
        validate_ast(child, allowed_fields=allowed_fields, depth=depth + 1, budget=budget)
    return node


def fingerprint(node, fields):
    validate_ast(node, allowed_fields=fields)
    def canonical(item):
        result = dict(item)
        if "args" in result:
            result["args"] = [canonical(a) for a in result["args"]]
            if item["op"] in {"add", "mul"}:
                result["args"].sort(key=payload_hash)
        return result
    return payload_hash({"dsl": "factor-ast-v1", "ast": canonical(node)})


def evaluate(node, fields):
    validate_ast(node, allowed_fields=set(fields))
    lengths = {len(v) for v in fields.values()}
    if len(lengths) != 1 or not lengths or sum(len(v) for v in fields.values()) > 200000:
        raise ValueError("factor_input_budget")
    n = lengths.pop()
    for values in fields.values():
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)) for v in values):
            raise ValueError("factor_non_numeric_input")
    def run(item):
        op = item["op"]
        if op == "field":
            return list(fields[item["name"]])
        if op == "constant":
            return [item["value"]] * n
        args = [run(a) for a in item["args"]]
        if op == "lag":
            w = item["window"]
            return ([None] * min(w, n) + args[0][:max(0, n - w)])
        if op == "mean":
            w = item["window"]
            values, total, missing, out = args[0], 0., 0, []
            for i, value in enumerate(values):
                total += value or 0
                missing += value is None
                if i >= w:
                    total -= values[i - w] or 0
                    missing -= values[i - w] is None
                out.append(total / w if i >= w - 1 and not missing else None)
            return out
        out = []
        for i in range(n):
            a = args[0][i]
            b = args[1][i] if len(args) == 2 else None
            if a is None or len(args) == 2 and b is None:
                out.append(None)
                continue
            value = (abs(a) if op == "abs" else a + b if op == "add" else a - b if op == "sub"
                     else a * b if op == "mul" else a / b if b else None)
            out.append(value if value is None or math.isfinite(value) else None)
        return out
    return run(node)


PREREGISTERED_FACTORS = {
    "earnings_quality": {"op": "div", "args": [{"op": "field", "name": "operating_cash_flow"},
                                                   {"op": "abs", "args": [{"op": "field", "name": "net_profit"}]}]},
    "capital_persistence": {"op": "mean", "window": 5, "args": [{"op": "div", "args": [
        {"op": "field", "name": "main_net_inflow"}, {"op": "field", "name": "amount"}]}]},
    "trend_exhaustion": {"op": "sub", "args": [{"op": "div", "args": [
        {"op": "field", "name": "close"}, {"op": "lag", "window": 10, "args": [{"op": "field", "name": "close"}]}]},
        {"op": "constant", "value": 1}]},
}
