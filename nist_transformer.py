from lark import Transformer, Token, v_args
from dataclasses import dataclass, field
from typing import Union

# AST node classes

@dataclass
class Program:
    lines: list["Line"]

@dataclass
class Line:
    block_delete: bool = False
    line_number: int | None = None
    segments: list["Segment"] = field(default_factory=list)

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
class ParameterRef: # parameter reference (parameter_index) ex: L#100
    index: 'Expression'

@dataclass
class ParameterSet: # parameter setting ex: #1 = 10
    index: 'Expression'
    value: 'Expression'

@dataclass
class Comment:
    text: str
    is_message: bool    # not sure about this one

@dataclass
class RealNumber:
    value: float

Expression = Union[RealNumber, ParameterRef, UnaryOp, BinaryOp]
Segment = Union[MidLineWord, ParameterSet, Comment]

# converts lark parse tree into python structures (basically strings and numbers)
@v_args(inline=True)
class NistTransformer(Transformer):
    def start(self, body):
        return Program(body)

    def body(self, *lines):
        return list(lines)

    def line(self, *items):
        block_delete = False
        line_number = None
        segments = []

        for item in items:
            if item == "/":
                block_delete = True

            elif isinstance(item, int):
                line_number = item

            elif isinstance(item,(MidLineWord, Comment, ParameterSet)):
                segments.append(item)

            else:
                # ignore end_of_line tokens
                pass

        return Line(block_delete, line_number, segments)

    def arc_tangent_combo(self, _arc_tangent, expression_1, _divided_by, expression_2):
        # atan[expr1]/[expr2]
        return BinaryOp("/", UnaryOp("atan", expression_1), expression_2)

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
        items = list(rest)

        for i in range(0, len(items), 2):
            op = items[i]
            right = items[i+1]
            node = BinaryOp(str(op).lower(), node, right)

        return node


    def line_number(self, *items):
        # return only the value without "n"/"N"
        digits = "".join(str(x) for x in items if str(x).isdigit())
        return int(digits)

    def mid_line_word(self, mid_line_letter, real_value):
        return MidLineWord(str(mid_line_letter).upper(), real_value)

    def mid_line_letter(self, item):
        return str(item).upper()

    def ordinary_unary_combo(self, op, expr):
        return UnaryOp(str(op).lower(), expr)

    def ordinary_unary_operation(self, item):
        return str(item).lower()

    def parameter_index(self, item):
        return item

    def parameter_setting(self, _parameter_sign, parameter_index, _equal_sign, real_value): # the parameters not used are there for the transformer to work correctly
        return ParameterSet(index=parameter_index, value=real_value)

    def parameter_value(self, _parameter_sign, parameter_index):
        return ParameterRef(index=parameter_index)

    def real_number(self, token: Token):
        return RealNumber(float(token))

    def real_value(self, item):
        return item

    def comment(self, token: Token):
        raw_text = str(token)
        text = raw_text[1:-1]

        stripped = text.lstrip()
        is_message = stripped.upper().startswith("MSG,")

        return Comment(text=text, is_message=is_message)

    def segment(self, item):
        return item

    def unary_combo(self, item):
        return item


    # all terminal expressions basically
    def absolute_value(self, t): return str(t)
    def and_op(self, *_): return "and"
    def arc_cosine(self, t): return str(t)
    def arc_sine(self, t): return str(t)
    def arc_tangent(self, t): return str(t)
    def block_delete(self, *_): return "/"
    def cosine(self, t): return str(t)
    #def decimal_point(self): return "." # not used anymore, already covered in grammar for real number without needing separate entity
    def digit(self, t): return str(t)
    def divided_by(self, *_): return "/"
    def equal_sign(self, *_): return "="
    def exclusive_or(self, *_): return "xor"
    def e_raised_to(self, t): return str(t)
    def end_of_line(self, *_): return None  # * -> accepts any number of extra positional arguments, _ means unused
    def fix_down(self, t): return str(t)
    def fix_up(self, t): return str(t)

    def letter_a(self, t): return str(t)
    def letter_b(self, t): return str(t)
    def letter_c(self, t): return str(t)
    def letter_d(self, t): return str(t)
    def letter_f(self, t): return str(t)
    def letter_g(self, t): return str(t)
    def letter_h(self, t): return str(t)
    def letter_i(self, t): return str(t)
    def letter_j(self, t): return str(t)
    def letter_k(self, t): return str(t)
    def letter_l(self, t): return str(t)
    def letter_m(self, t): return str(t)
    def letter_n(self, t): return str(t)
    def letter_p(self, t): return str(t)
    def letter_q(self, t): return str(t)
    def letter_r(self, t): return str(t)
    def letter_s(self, t): return str(t)
    def letter_t(self, t): return str(t)
    def letter_x(self, t): return str(t)
    def letter_y(self, t): return str(t)
    def letter_z(self, t): return str(t)

    def minus(self, *_): return "-"
    def modulo(self, *_): return "mod"
    def natural_log_of(self, t): return str(t)
    def non_exclusive_or(self, *_): return "or"
    def parameter_sign(self, *_): return "#"
    def plus(self, *_): return "+"
    def power(self, *_): return "**"
    def round(self, t): return str(t)
    def sine(self, t): return str(t)
    def square_root(self, t): return str(t)
    def tangent(self, t): return str(t)
    def times(self, *_): return "*"

