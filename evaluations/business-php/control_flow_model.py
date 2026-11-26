from itertools import product
from math import prod
import operator

INT_MIN, INT_MAX = -(2**31), 2**31 - 1
MAX_PARAMS = 6
MAX_WORLDS = 24_000
_COMPARE = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
            ">": operator.gt, ">=": operator.ge}


def compare_models(left, right, contract, source_scope, target_scope):
    if any(model["params"] != contract for model in (left, right)) or left["return_type"] != right["return_type"]:
        return {"status": "unverified", "reason": "incompatible_type_contract"}
    bounds = set()
    for model, scope in ((left, source_scope), (right, target_scope)):
        _check_body(model["body"], model["return_type"], contract, scope, bounds, [512])
    choices = _choices(contract, bounds)
    if prod(map(len, choices)) > MAX_WORLDS:
        return {"status": "unverified", "reason": "input_partition_limit"}
    count, witness = 0, None
    for values in product(*choices):
        count += 1
        original = _run(left["body"], values, [])
        migrated = _run(right["body"], values, [])
        if original is None or migrated is None:
            return {"status": "unverified", "reason": "non_total_return", "checked_worlds": count}
        if witness is None and original["value"] != migrated["value"]:
            witness = {"inputs": list(values), "source": original, "target": migrated}
    if witness is not None:
        return {"status": "mismatch", "reason": "different_return", "checked_worlds": count, "witness": witness}
    return {"status": "verified", "reason": "same_modeled_returns", "checked_worlds": count}


def _choices(types, constants):
    integer_count = types.count("int")
    points = sorted(constants | {INT_MIN, INT_MAX})
    # Equalities and ordering distinguish at most one integer per input inside an interval.
    # Representatives preserve every relative order (including ties), not only each isolated boundary.
    representatives = [point for bound, next_bound in zip(points, points[1:])
                       for point in range(bound, min(next_bound, bound + integer_count + 1))]
    representatives.append(points[-1])
    return [(False, True) if kind == "bool" else representatives for kind in types]


def _check_body(body, return_type, args, scope, constants, budget, depth=0):
    if depth > 16 or not isinstance(body, list) or return_type not in {"bool", "int"}:
        raise ValueError("invalid bounded control-flow body")
    for statement in body:
        _spend(budget, depth)
        if not isinstance(statement, dict) or type(statement.get("line")) is not int:
            raise ValueError("invalid control-flow statement")
        line = statement["line"]
        if not scope["start_line"] <= line <= scope["end_line"]:
            raise ValueError("statement outside paired range")
        if statement.get("op") == "if" and set(statement) == {"op", "line", "test", "then", "else"}:
            if _check_expression(statement["test"], args, constants, budget, depth + 1) != "bool":
                raise ValueError("non-boolean condition")
            for branch in ("then", "else"):
                _check_body(statement[branch], return_type, args, scope, constants, budget, depth + 1)
        elif statement.get("op") == "return" and set(statement) == {"op", "line", "value"}:
            if _check_expression(statement["value"], args, constants, budget, depth + 1) != return_type:
                raise ValueError("incompatible return type")
        else:
            raise ValueError("unsupported control-flow statement")


def _spend(budget, depth):
    budget[0] -= 1
    if budget[0] < 0 or depth > 24:
        raise ValueError("control-flow expression limit")


def _check_expression(expression, args, constants, budget, depth):
    _spend(budget, depth)
    if not isinstance(expression, list) or len(expression) not in (2, 3) or not isinstance(expression[0], str):
        raise ValueError("invalid condition expression")
    op, first, *rest = expression
    if not rest:
        if op == "arg" and type(first) is int and 0 <= first < len(args):
            return args[first]
        if op == "bool" and type(first) is bool:
            return "bool"
        if op == "int" and type(first) is int and INT_MIN <= first <= INT_MAX:
            constants.add(first)
            return "int"
        if op == "not" and _check_expression(first, args, constants, budget, depth + 1) == "bool":
            return "bool"
        raise ValueError("unsupported operand or type")
    left = _check_expression(first, args, constants, budget, depth + 1)
    right = _check_expression(rest[0], args, constants, budget, depth + 1)
    if ((op in {"==", "!="} and left == right)
            or (op in {"&&", "||"} and left == right == "bool")
            or (op in {"<", "<=", ">", ">="} and left == right == "int")):
        return "bool"
    raise ValueError("operator outside the common typed subset")


def _run(body, values, trace):
    for statement in body:
        if statement["op"] == "return":
            return {"line": statement["line"], "value": _evaluate(statement["value"], values), "branches": trace}
        passed = _evaluate(statement["test"], values)
        trace.append({"line": statement["line"], "condition": passed})
        outcome = _run(statement["then" if passed else "else"], values, trace)
        if outcome is not None:
            return outcome
    return None


def _evaluate(expression, values):
    op, first, *rest = expression
    if op == "arg":
        return values[first]
    if op in {"int", "bool"}:
        return first
    left = _evaluate(first, values)
    if op == "not":
        return not left
    if op == "&&":
        return left and _evaluate(rest[0], values)
    if op == "||":
        return left or _evaluate(rest[0], values)
    return _COMPARE[op](left, _evaluate(rest[0], values))
