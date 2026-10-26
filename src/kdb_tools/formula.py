"""The formula language: numbers, + - * / **, parentheses and /<page>.md#<name> references.

A formula states a value as a calculation over other values. The caller supplies lookup(page_rel,
name), which returns the referenced number, so the same evaluator serves any page field. A
reference uses the value as written, so a formula carries its own unit factors.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from typing import Callable

from . import bundle as B

REF = re.compile(r"(/[\w./-]+\.md)#(\w+)")
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
       ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


class FormulaError(ValueError):
    pass


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def pages_by_rel(b: B.Bundle) -> dict[str, B.Page]:
    index = getattr(b, "_pages_by_rel", None)
    if index is None:
        index = {p.rel: p for p in b.concepts}
        b._pages_by_rel = index
    return index


def evaluate(b: B.Bundle, formula: str, lookup: Callable[[str, str], float]) -> float:
    """The formula's value. lookup(page_rel, name) raises FormulaError when it cannot answer."""
    names: dict[str, float] = {}

    def name_for(m: re.Match) -> str:
        if m.group(1).lstrip("/") not in pages_by_rel(b):
            raise FormulaError(f"{m.group(1)} is not a page")
        value = lookup(m.group(1).lstrip("/"), m.group(2))
        if not is_number(value):
            raise FormulaError(f"{m.group(1)}#{m.group(2)} is not a number")
        key = f"q{len(names)}"
        names[key] = float(value)
        return key

    try:
        tree = ast.parse(REF.sub(name_for, formula), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"cannot parse it: {exc.msg}") from exc

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and is_number(node.value):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.operand))
        raise FormulaError("it may use only numbers, values written as /<page>.md#<name>, + - * / ** and parentheses")

    try:
        value = walk(tree)
    except ZeroDivisionError as exc:
        raise FormulaError("it divides by zero") from exc
    except OverflowError as exc:
        raise FormulaError("it is too large to compute") from exc
    if not math.isfinite(value):  # float overflow gives inf (or nan) without raising
        raise FormulaError("its result is not a finite number")
    return value
