from lark import Transformer, Token, v_args
from dataclasses import dataclass
from typing import List, Union, Optional

# AST node classes

@dataclass
class Program:
    maybe_start_percent: Optional[str]
    body: List['Line']
    maybe_end_percent: Optional[str]

@dataclass
class Line:
    block_delete: Optional[str]
    line_number: Optional[str]
    segments: List[Union['MidLineWord', 'Comment', 'ParameterSet']]
    end_of_line: str

@dataclass
class BinaryOp:
    op: str
    left: 'Expression'
    right: 'Expression'

@dataclass
class UnaryOp:
    op: str
    arg: 'Expression'

@dataclass
class MidLineWord:
    mid_line_letter: str
    real_value: 'Expression'

@dataclass
class ParameterRef:
    value: 'Expression'

@dataclass
class ParameterSet:
    index: 'Expression'
    value: 'Expression'

@dataclass
class Comment:
    text: str

@dataclass
class RealNumber:
    value: float

Expression = Union[RealNumber, ParameterRef, UnaryOp, BinaryOp]

# converts lark parse tree into python structures (basically strings and numbers)
@v_args(inline=True)
class NistTransformer(Transformer):
    def start(self, maybe_start_percent, body, maybe_end_percent):
        start = maybe_start_percent if maybe_start_percent else None
        end = maybe_end_percent if maybe_end_percent else None
        lines = body if isinstance(body, list) else [body] if body is not None else []
        return Program(start, lines, end)

    def maybe_start_percent(self, item = None):
        return item

    def maybe_end_percent(self, item = None):
        return item

    def body(self, *lines):
        return list(lines)

    def line(self, *items):
        block_delete = None
        line_number = None
        segments = []
        end_of_line = None
        for item in items:
            if isinstance(item, str) and item.strip() == "/":
                block_delete = item
                continue

            if isinstance(item, str) and any(ch in item for ch in "\r\n"):
                end_of_line = item
                continue

            if isinstance(item, int):
                line_number = item
                continue

            if isinstance(item,(MidLineWord, Comment, ParameterSet)):
                segments.append(item)
                continue

        return Line(block_delete, line_number, segments, end_of_line)

    def arc_tangent_combo(self, arc_tangent, expression_1, divided_by, expression_2):
        atan_name = str(arc_tangent)
        left = UnaryOp(atan_name, expression_1)
        return BinaryOp("/", left, expression_2)

    def binary_operation(self, item):
        return item

    def binary_operation1(self, item):
        return item

    def binary_operation2(self, item):
        return item

    def binary_operation3(self, item):
        return item

    def expression(self, first, *rest):
        node = first
        it = iter(rest)
        for op, val in zip(it, it):
            op_str = str(op)
            node = BinaryOp(op_str, node, val)
        return node


    def line_number(self, *items):
        # return only the value without "n"/"N"
        s = ''.join(str(x) for x in items if str(x).isdigit())
        if s == "":
            return None
        try:
            return int(s)
        except ValueError:
            return None


    def mid_line_word(self, mid_line_letter, real_value):
        return MidLineWord(mid_line_letter, real_value)

    def ordinary_unary_combo(self, op, expr):
        return UnaryOp(str(op), expr)

    def ordinary_unary_operation(self, item):
        return item

    def parameter_index(self, item):
        return item

    def parameter_setting(self, parameter_sign, parameter_index, equal_sign, real_value):
        return ParameterSet(parameter_index, real_value)

    def parameter_value(self, parameter_sign, parameter_index):
        return ParameterRef(parameter_index)

    def real_value(self, item):
        return item

    def segment(self, item):
        return item

    def unary_combo(self, item):
        return item


    # all terminal expressions basically
    def percent_line(self, token: Token): return str(token)
    def absolute_value(self, token: Token): return str(token)
    def and_op(self): return "and"
    def arc_cosine(self, token: Token): return str(token)
    def arc_sine(self, token: Token): return str(token)
    def arc_tangent(self, token: Token): return str(token)
    def block_delete(self): return "/"
    def comment(self, token: Token): return Comment(str(token))
    def cosine(self, token: Token): return str(token)
    def decimal_point(self, token: Token): return str(token)
    def digit(self, token: Token): return str(token)
    def divided_by(self): return "/"
    def equal_sign(self): return "="
    def exclusive_or(self): return "xor"
    def e_raised_to(self, token: Token): return str(token)
    def end_of_line(self, token: Token): return str(token)
    def fix_down(self, token: Token): return str(token)
    def fix_up(self, token: Token): return str(token)

    def letter_a(self, token: Token): return str(token)
    def letter_b(self, token: Token): return str(token)
    def letter_c(self, token: Token): return str(token)
    def letter_d(self, token: Token): return str(token)
    def letter_f(self, token: Token): return str(token)
    def letter_g(self, token: Token): return str(token)
    def letter_h(self, token: Token): return str(token)
    def letter_i(self, token: Token): return str(token)
    def letter_j(self, token: Token): return str(token)
    def letter_k(self, token: Token): return str(token)
    def letter_l(self, token: Token): return str(token)
    def letter_m(self, token: Token): return str(token)
    def letter_n(self, token: Token): return str(token)
    def letter_p(self, token: Token): return str(token)
    def letter_q(self, token: Token): return str(token)
    def letter_r(self, token: Token): return str(token)
    def letter_s(self, token: Token): return str(token)
    def letter_t(self, token: Token): return str(token)
    def letter_x(self, token: Token): return str(token)
    def letter_y(self, token: Token): return str(token)
    def letter_z(self, token: Token): return str(token)

    def minus(self): return "-"
    def mid_line_letter(self, token: Token): return str(token)
    def modulo(self): return "mod"
    def natural_log_of(self, token: Token): return str(token)
    def non_exclusive_or(self): return "or"
    def parameter_sign(self): return "#"
    def plus(self): return "+"
    def power(self): return "**"
    def real_number(self, token: Token): return float(str(token))
    def round(self, token: Token): return str(token)
    def sine(self, token: Token): return str(token)
    def square_root(self, token: Token): return str(token)
    def tangent(self, token: Token): return str(token)
    def times(self): return "*"

