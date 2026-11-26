# SPDX-FileCopyrightText: 2026 Blender5-Collada Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later

"""Safe recursive-descent evaluator for BeamNG jbeam $=... expressions (no eval/Lua).

Supports the observed grammar in BeamNG flexbodies/props/modifiers:
  - String literals ('...' and "..."), numbers, true / false / nil
  - Concatenation (..), arithmetic (+, -, *, /, %), unary (-, +, not)
  - Comparisons (==, ~=, <, >, <=, >=)
  - Short-circuit logical operators (and, or) following Lua truthiness (only False and None are falsy)
  - Variables: $components.a.b, $var_name, indexing [expr]
  - Safe built-ins: round, floor, ceil, abs, min, max, clamp, sign, case, tostring, tonumber, random
  - Non-random expansion of random(n) / random(a, b) into all candidate integer values
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

__all__ = ["ExprError", "eval_mesh_expr", "eval_modifier_expr", "extract_string_literals"]


class ExprError(ValueError):
    """Raised when a jbeam expression cannot be parsed or safely evaluated."""


@dataclass(frozen=True)
class _RandomCandidates:
    """Marker representing a finite set of integer outputs from random(...)."""

    values: tuple[int, ...]


_TOK_RE = re.compile(
    r"""
    \s*(?:
      (?P<STR>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    | (?P<NUM>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    | (?P<OP>\.\.|==|~=|<=|>=|[+\-*/%<>()\[\].,])
    | (?P<VAR>\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<BAD>.)
    )
    """,
    re.VERBOSE,
)

_STR_LIT_RE = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"")


def _unescape_str(raw: str) -> str:
    inner = raw[1:-1]
    if "\\" not in inner:
        return inner
    out: list[str] = []
    i = 0
    n = len(inner)
    esc_map = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
    while i < n:
        c = inner[i]
        if c == "\\" and i + 1 < n:
            out.append(esc_map.get(inner[i + 1], inner[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def extract_string_literals(expr_str: str) -> list[str]:
    """Extract all quoted string literals from an expression in source order (deduplicated)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _STR_LIT_RE.finditer(expr_str):
        val = _unescape_str(m.group(0))
        if val and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def _lua_truthy(val: Any) -> bool:
    return val is not False and val is not None


def _lua_to_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _lua_round(x: Any) -> int:
    fx = float(x)
    return int(math.floor(fx + 0.5)) if fx >= 0 else int(math.ceil(fx - 0.5))


class _ExprParser:
    def __init__(
        self,
        src: str,
        vars_dict: dict[str, Any],
        components_dict: dict[str, Any],
    ) -> None:
        self.tokens: list[tuple[str, str]] = []
        pos = 0
        n = len(src)
        while pos < n:
            m = _TOK_RE.match(src, pos)
            if not m:
                break
            pos = m.end()
            kind = m.lastgroup
            if kind is None:
                continue
            text = m.group(kind)
            if kind == "BAD":
                raise ExprError(f"Unexpected token {text!r}")
            self.tokens.append((kind, text))
        self.i = 0
        self.vars = vars_dict
        self.components = components_dict

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _consume(self, expected_kind: str | None = None, expected_val: str | None = None) -> tuple[str, str]:
        tok = self._peek()
        if tok is None:
            raise ExprError("Unexpected end of expression")
        if expected_kind is not None and tok[0] != expected_kind:
            raise ExprError(f"Expected {expected_kind}, got {tok[1]!r}")
        if expected_val is not None and tok[1] != expected_val:
            raise ExprError(f"Expected {expected_val!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self) -> Any:
        val = self._parse_or()
        if self._peek() is not None:
            raise ExprError(f"Trailing token {self._peek()[1]!r}")
        return val

    def _parse_or(self) -> Any:
        left = self._parse_and()
        while self._peek() == ("IDENT", "or"):
            self._consume()
            right = self._parse_and()
            if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
                left = self._combine_candidates(left, right, lambda a, b: a if _lua_truthy(a) else b)
            else:
                left = left if _lua_truthy(left) else right
        return left

    def _parse_and(self) -> Any:
        left = self._parse_cmp()
        while self._peek() == ("IDENT", "and"):
            self._consume()
            right = self._parse_cmp()
            if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
                left = self._combine_candidates(left, right, lambda a, b: b if _lua_truthy(a) else a)
            else:
                left = right if _lua_truthy(left) else left
        return left

    def _parse_cmp(self) -> Any:
        left = self._parse_concat()
        while (tok := self._peek()) is not None and tok[0] == "OP" and tok[1] in ("==", "~=", "<", ">", "<=", ">="):
            op = self._consume()[1]
            right = self._parse_concat()
            left = self._apply_cmp(op, left, right)
        return left

    @staticmethod
    def _cmp_scalar(op: str, a: Any, b: Any) -> bool:
        if op == "==":
            if type(a) is bool or type(b) is bool:
                return type(a) is type(b) and a == b
            return a == b
        if op == "~=":
            return not _ExprParser._cmp_scalar("==", a, b)
        if a is None or b is None:
            return False
        fa, fb = float(a), float(b)
        if op == "<":
            return fa < fb
        if op == ">":
            return fa > fb
        if op == "<=":
            return fa <= fb
        if op == ">=":
            return fa >= fb
        raise ExprError(f"Unsupported comparison operator {op!r}")

    def _apply_cmp(self, op: str, left: Any, right: Any) -> Any:
        if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
            return self._combine_candidates(left, right, lambda a, b: self._cmp_scalar(op, a, b))
        return self._cmp_scalar(op, left, right)

    def _parse_concat(self) -> Any:
        left = self._parse_add()
        while self._peek() == ("OP", ".."):
            self._consume()
            right = self._parse_add()
            if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
                left = self._combine_candidates(left, right, lambda a, b: _lua_to_str(a) + _lua_to_str(b))
            else:
                left = _lua_to_str(left) + _lua_to_str(right)
        return left

    def _parse_add(self) -> Any:
        left = self._parse_mul()
        while (tok := self._peek()) is not None and tok[0] == "OP" and tok[1] in ("+", "-"):
            op = self._consume()[1]
            right = self._parse_mul()
            if op == "+":
                fn = lambda a, b: float(a) + float(b)  # noqa: E731
            else:
                fn = lambda a, b: float(a) - float(b)  # noqa: E731
            if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
                left = self._combine_candidates(left, right, fn)
            else:
                if left is None or right is None:
                    raise ExprError(f"Arithmetic {op!r} on nil")
                left = fn(left, right)
        return left

    def _parse_mul(self) -> Any:
        left = self._parse_unary()
        while (tok := self._peek()) is not None and tok[0] == "OP" and tok[1] in ("*", "/", "%"):
            op = self._consume()[1]
            right = self._parse_unary()
            if op == "*":
                fn = lambda a, b: float(a) * float(b)  # noqa: E731
            elif op == "/":
                fn = lambda a, b: float(a) / float(b)  # noqa: E731
            else:
                fn = lambda a, b: float(a) % float(b)  # noqa: E731
            if isinstance(left, _RandomCandidates) or isinstance(right, _RandomCandidates):
                left = self._combine_candidates(left, right, fn)
            else:
                if left is None or right is None:
                    raise ExprError(f"Arithmetic {op!r} on nil")
                left = fn(left, right)
        return left

    def _parse_unary(self) -> Any:
        tok = self._peek()
        if tok == ("IDENT", "not"):
            self._consume()
            val = self._parse_unary()
            if isinstance(val, _RandomCandidates):
                return _RandomCandidates(tuple(not _lua_truthy(v) for v in val.values))  # type: ignore[arg-type]
            return not _lua_truthy(val)
        if tok == ("OP", "-"):
            self._consume()
            val = self._parse_unary()
            if isinstance(val, _RandomCandidates):
                return _RandomCandidates(tuple(-float(v) for v in val.values))  # type: ignore[arg-type]
            if val is None:
                raise ExprError("Unary '-' on nil")
            return -float(val)
        if tok == ("OP", "+"):
            self._consume()
            return self._parse_unary()
        return self._parse_primary()

    def _parse_primary(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise ExprError("Unexpected end of expression")
        kind, text = tok
        if kind == "STR":
            self._consume()
            return _unescape_str(text)
        if kind == "NUM":
            self._consume()
            return int(text) if text.isdigit() else float(text)
        if kind == "OP" and text == "(":
            self._consume()
            val = self._parse_or()
            self._consume("OP", ")")
            return val
        if kind == "VAR":
            self._consume()
            return self._resolve_var_chain(text)
        if kind == "IDENT":
            self._consume()
            if text == "true":
                return True
            if text == "false":
                return False
            if text == "nil":
                return None
            if self._peek() == ("OP", "("):
                return self._parse_call(text)
            raise ExprError(f"Unknown identifier {text!r}")
        raise ExprError(f"Unexpected token {text!r}")

    def _resolve_var_chain(self, root_name: str) -> Any:
        if root_name == "$components":
            val: Any = self.components
        else:
            val = self.vars.get(root_name)
            if val is None and root_name.startswith("$"):
                val = self.vars.get(root_name[1:])
        while True:
            tok = self._peek()
            if tok == ("OP", "."):
                self._consume()
                attr_tok = self._consume()
                if attr_tok[0] not in ("IDENT", "VAR"):
                    raise ExprError(f"Expected attribute name after '.', got {attr_tok[1]!r}")
                key = attr_tok[1]
                val = val.get(key) if isinstance(val, dict) else None
            elif tok == ("OP", "["):
                self._consume()
                idx = self._parse_or()
                self._consume("OP", "]")
                if isinstance(val, dict):
                    val = val.get(idx)
                elif isinstance(val, list) and isinstance(idx, (int, float)):
                    i1 = int(idx) - 1  # Lua 1-based indexing
                    val = val[i1] if 0 <= i1 < len(val) else None
                else:
                    val = None
            else:
                break
        return val

    def _parse_call(self, func_name: str) -> Any:
        self._consume("OP", "(")
        args: list[Any] = []
        if self._peek() != ("OP", ")"):
            while True:
                args.append(self._parse_or())
                if self._peek() == ("OP", ","):
                    self._consume()
                    continue
                break
        self._consume("OP", ")")

        if func_name == "random":
            if len(args) == 0:
                return _RandomCandidates((0, 1))
            if len(args) == 1:
                hi = int(float(args[0]))
                if hi < 0 or hi > 64:
                    raise ExprError(f"random({hi}) out of safe expansion bounds")
                # Expand 0..hi so both round(random(n)) (0..n) and random(n) (1..n) are covered
                return _RandomCandidates(tuple(range(0, hi + 1)))
            if len(args) == 2:
                lo = int(float(args[0]))
                hi = int(float(args[1]))
                if hi < lo or (hi - lo) > 64:
                    raise ExprError(f"random({lo}, {hi}) out of safe expansion bounds")
                return _RandomCandidates(tuple(range(lo, hi + 1)))
            raise ExprError(f"random() expects 0..2 args, got {len(args)}")

        if func_name in ("round", "floor", "ceil", "abs", "sign", "tostring", "tonumber"):
            if len(args) != 1:
                raise ExprError(f"{func_name}() expects 1 arg")
            fn_map = {
                "round": _lua_round,
                "floor": lambda x: int(math.floor(float(x))),
                "ceil": lambda x: int(math.ceil(float(x))),
                "abs": lambda x: abs(float(x)),
                "sign": lambda x: (1 if float(x) > 0 else (-1 if float(x) < 0 else 0)),
                "tostring": _lua_to_str,
                "tonumber": lambda x: float(x) if x is not None else None,
            }
            fn = fn_map[func_name]
            a0 = args[0]
            if isinstance(a0, _RandomCandidates):
                vals: list[Any] = []
                seen_u: set[tuple[type, Any]] = set()
                for v in a0.values:
                    rv = fn(v)
                    key = (type(rv), rv)
                    if key not in seen_u:
                        seen_u.add(key)
                        vals.append(rv)
                return _RandomCandidates(tuple(vals))  # type: ignore[arg-type]
            return fn(a0)

        if func_name in ("min", "max") and args:
            fn = min if func_name == "min" else max
            if any(isinstance(a, _RandomCandidates) for a in args):
                raise ExprError(f"{func_name}() on random candidates not supported")
            return fn(float(a) for a in args)

        if func_name == "clamp" and len(args) == 3:
            v, lo, hi = float(args[0]), float(args[1]), float(args[2])
            return max(lo, min(hi, v))

        if func_name == "case" and len(args) >= 2:
            sel = args[0]
            choices = args[1:]
            if isinstance(sel, bool):
                idx = 0 if sel else 1
            else:
                idx = int(math.floor(float(sel))) - 1
            return choices[idx] if 0 <= idx < len(choices) else choices[-1]

        raise ExprError(f"Unsupported function {func_name!r}")

    MAX_TOTAL_CANDIDATES = 256

    @staticmethod
    def _combine_candidates(left: Any, right: Any, fn) -> _RandomCandidates:
        lvals = left.values if isinstance(left, _RandomCandidates) else (left,)
        rvals = right.values if isinstance(right, _RandomCandidates) else (right,)
        out: list[Any] = []
        seen: set[tuple[type, Any]] = set()
        limit = _ExprParser.MAX_TOTAL_CANDIDATES
        for a in lvals:
            for b in rvals:
                v = fn(a, b)
                key = (type(v), v)
                if key not in seen:
                    if len(out) >= limit:
                        raise ExprError(f"random() candidate count exceeds limit ({limit})")
                    seen.add(key)
                    out.append(v)
        return _RandomCandidates(tuple(out))  # type: ignore[arg-type]


def _strip_expr_prefix(expr_str: str) -> str:
    s = expr_str.strip()
    if s.startswith("$="):
        return s[2:].strip()
    return s


def eval_mesh_expr(
    expr_str: str,
    vars_dict: dict[str, Any],
    components_dict: dict[str, Any],
    known_meshes: set[str],
) -> tuple[list[str], bool, list[str]]:
    """Safely evaluate a BeamNG mesh-name expression.

    Returns ``(candidates, is_multi_candidate, warnings)``:
      - Normal deterministic result: ``([mesh_name], False, [])`` (or ``([], False, [])`` if nil/empty)
      - ``random(...)`` expansion: ``([cand1, cand2, ...], True, [])`` filtered to ``known_meshes``
      - Fallback on evaluation failure: extracts all string literals in ``known_meshes``,
        sets ``is_multi_candidate=True``, and includes a warning with the expression text.
    """
    body = _strip_expr_prefix(expr_str)
    try:
        val = _ExprParser(body, vars_dict, components_dict).parse()
    except (ExprError, TypeError, ValueError, ZeroDivisionError, RecursionError) as exc:
        lits = [lit for lit in extract_string_literals(expr_str) if lit in known_meshes]
        return lits, True, [f"Could not evaluate mesh expression {expr_str!r} ({exc}); using literal candidates"]

    if isinstance(val, _RandomCandidates):
        cands: list[str] = []
        seen: set[str] = set()
        for item in val.values:
            if isinstance(item, str) and item and item in known_meshes and item not in seen:
                seen.add(item)
                cands.append(item)
        if not cands:
            # If known_meshes is empty (e.g. unit test without mesh index) keep all string values
            if not known_meshes:
                for item in val.values:
                    if isinstance(item, str) and item and item not in seen:
                        seen.add(item)
                        cands.append(item)
        if not cands:
            return [], True, [f"Mesh expression {expr_str!r} produced no matching mesh candidates"]
        return cands, True, []

    if val is None or val is False or val == "":
        return [], False, []
    if isinstance(val, str):
        return [val], False, []
    lits = [lit for lit in extract_string_literals(expr_str) if lit in known_meshes]
    return lits, True, [f"Mesh expression {expr_str!r} evaluated to non-string {val!r}"]


def eval_modifier_expr(
    val: Any,
    vars_dict: dict[str, Any],
    components_dict: dict[str, Any],
) -> tuple[float | None, str | None]:
    """Safely evaluate a numeric modifier value (or $= expression). Returns (float_or_None, warning)."""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val), None
    if not isinstance(val, str):
        return None, None
    s = val.strip()
    if not s:
        return None, None
    if s.startswith("$="):
        body = s[2:].strip()
        try:
            res = _ExprParser(body, vars_dict, components_dict).parse()
            if isinstance(res, _RandomCandidates):
                res = res.values[0] if res.values else None
            if isinstance(res, (int, float)) and not isinstance(res, bool) and math.isfinite(float(res)):
                return float(res), None
            return None, f"Modifier expression {val!r} evaluated to non-number {res!r}"
        except (ExprError, TypeError, ValueError, ZeroDivisionError, RecursionError) as exc:
            return None, f"Could not evaluate modifier expression {val!r}: {exc}"
    if s.startswith("$"):
        v = vars_dict.get(s)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v), None
        return None, f"Unresolved modifier variable {val!r}"
    try:
        return float(s), None
    except ValueError:
        return None, f"Non-numeric modifier value {val!r}"
