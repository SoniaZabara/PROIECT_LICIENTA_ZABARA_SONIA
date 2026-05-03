import math

# these dataclasses contain pure expressions
from translator.nist_transformer import (
    RealNumber,
    ParameterRef,
    UnaryOp,
    BinaryOp,
)

class ExpressionEvaluationError(Exception):
    pass

class ExpressionEvaluator:
    def __init__(self, params: dict[int, float]):
        self.params = params

    def eval(self, expr):
        if isinstance(expr, RealNumber):
            return expr.value

        if isinstance(expr, ParameterRef):
            index = self.eval_parameter_index(expr.index)
            return float(self.params.get(index, 0.0))

        if isinstance(expr, UnaryOp):
            return self.eval_unary(expr)

        if isinstance(expr, BinaryOp):
            return self.eval_binary(expr)

        raise ExpressionEvaluationError(f"Unknown expression: {expr}")

    def eval_parameter_index(self, expr):
        value = self.eval(expr)
        index = round(value)

        if abs(value - index) > 0.0001:
            raise ExpressionEvaluationError(
                f"Parameter index must be close to integer, got {value}"
            )

        if not (1 <= index <= 5399):
            raise ExpressionEvaluationError(
                f"Parameter index out of range: {index}"
            )

        return int(index)

    def eval_unary(self, expr):
        op = expr.op.lower()
        value = self.eval(expr.arg)

        # Arguments to unary operations
        # which take angle measures (COS, SIN, and TAN) are in degrees. Values returned by unary
        # operations which return angle measures (ACOS, ASIN, and ATAN) are also in degrees.
        if op == "abs": return abs(value)
        if op == "acos": return math.degrees(math.acos(value))
        if op == "asin": return math.degrees(math.asin(value))
        if op == "atan": return math.degrees(math.atan(value))
        if op == "cos": return math.cos(math.radians(value))
        if op == "exp": return math.exp(value)
        if op == "fix": return math.floor(value)
        if op == "fup": return math.ceil(value)
        if op == "ln": return math.log(value)
        if op == "round": return round(value)
        if op == "sin": return math.sin(math.radians(value))
        if op == "sqrt": return math.sqrt(value)
        if op == "tan": return math.tan(math.radians(value))

        raise ExpressionEvaluationError(f"Unknown unary op: {op}")

    def eval_binary(self, expr):
        op = expr.op.lower()
        left = self.eval(expr.left)
        right = self.eval(expr.right)

        if op == "+": return left + right
        if op == "-": return left - right
        if op == "*": return left * right
        if op == "/": return left / right
        if op == "**": return left ** right
        if op == "mod": return left % right

        if op == "and":
            return 1.0 if left != 0 and right != 0 else 0.0

        if op == "or":
            return 1.0 if left != 0 or right != 0 else 0.0

        if op == "xor":
            return 1.0 if (left != 0) ^ (right != 0) else 0.0

        raise ExpressionEvaluationError(f"Unknown binary op: {op}")

