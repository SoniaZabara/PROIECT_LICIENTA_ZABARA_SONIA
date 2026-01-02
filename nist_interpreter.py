from dataclasses import dataclass
from typing import Optional, List, Dict, Any
import math

# IR (Intermediate Representation) dataclasses
@dataclass
class RapidMove:
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]
    def __repr__(self):
        return f"RapidMove(x={self.x}, y={self.y}, z={self.z})"

@dataclass
class LinearMove:
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]
    feed: Optional[float]
    def __repr__(self):
        return f"LinearMove(x={self.x}, y={self.y}, z={self.z}, feed={self.feed})"

@dataclass
class ArcMove:
    cw: bool    #clockwise True = G2, False = G3
    x: Optional[float]
    y: Optional[float]
    i: Optional[float]
    j: Optional[float]
    def __repr__(self):
        d = "CW" if self.cw else "CCW"
        return f"ArcMove({d}, x={self.x}, y={self.y}), i={self.i}, j={self.j})"

@dataclass
class SetFeed:
    feed: float
    def __repr__(self):
        return f"SetFeed(feed={self.feed})"

@dataclass
class SetUnits:
    units: str
    def __repr__(self):
        return f"SetUnits(units={self.units})"

@dataclass
class SetTool:
    tool: int
    def __repr__(self):
        return f"SetTool(tool={self.tool})"

@dataclass
class ProgramEnd:
    def __repr__(self):
        return "ProgramEnd()"

# Interpreter
class NistInterpreter:
    def __init__(self):
        # machine state - may make a different class later
        self.units = 'mm'           # 'mm' or 'inch' (G21/G20)
        self.absolute = True        # True = G90, False = G91
        self.motion_mode = 'G0'     # G0, G1, G2, G3,
        self.feed: Optional[float] = None
        self.tool: Optional[int] = None
        # see modal groups for whole possibilities

        #  machine position (current)
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0

        # parameter table 5400 values?? not sure where useful, should keep values between uses
        self.params: Dict[int, float] = {}

        # modal group mapping
        # self.modal_groups = {}

    # accept Program AST -> list of IR
    def interpret(self, program) -> List[Any]:
        out: List[Any] = []
        if program is None:
            return out
        for line in getattr(program, "body", []) or []:
            try:
                # if line.block_delete != "/":    #ignore deleted lines
                # out.extend(self._interpret_line(line))
                out.extend(self._interpret_line(line) or [])
            except Exception as e:
                print(f"[Interpreter] Error interpreting line {line}: {e}")
        return out

    # interpret a single Line AST (_ because is internal)
    def _interpret_line(self, line) -> List:
        ir: List[Any] = []

        if line is None:
            return ir

        # process parameter
        for seg in line.segments:
            if self._is_instance_name(seg, "ParameterSet"):
                try:
                    self._apply_param_set(seg)
                except Exception as e:
                    print(f"[Interpreter] Parameter set error: {e}")

        words: Dict[str, Any] = {}
        for seg in line.segments:
            # comments I might skipp
            if self._is_instance_name(seg, 'Comment'):
                ir.append(getattr(seg, "text", None))
                continue

            if self._is_instance_name(seg, 'MidLineWord'):
                letter = getattr(seg, "mid_line_letter", None)
                if letter is None:
                    continue
                letter = str(letter).strip().upper()
                # real_value could be RealNumber, BinaryOp, etc
                words[letter] = getattr(seg, "real_value", None)
                continue


        # handle G, M, F, S, T
        if 'G' in words:
            gval = self._eval_expr(words['G'])
            self._handle_g_code(gval, ir)

        if 'M' in words:
            mval = self._eval_expr(words['M'])
            self._handle_m_code(mval, ir)

        if 'F' in words:
            fval = self._eval_expr(words['F'])
            if fval is not None:
                self.feed = fval
                ir.append(SetFeed(self.feed))

        if 'S' in words:
            sval = words['S']
            # create and append SetSpindle if needed in the future

        if 'T' in words:
            tval = words['T']
            if tval is not None:
                self.tool = int(round(tval))
                ir.append(SetTool(self.tool))

        # extract coordinates
        coords: Dict[str, Optional[float]] = {}
        for axis in ('X', 'Y', 'Z', 'I', 'J', 'R'): #XY plane
            if axis in words:
                coords[axis] = self._eval_expr(words.get(axis))
            else:
                coords[axis] = None

        # convert units, convert inch into mm internally
        if self.units == 'inch':
            for a in ('X', 'Y', 'Z', 'I', 'J', 'R'):
                if coords[a] is not None:
                    coords[a] = coords[a] * 25.4

        # resolve target position according to absolute/relative
        #target = {'X': self.x, 'Y': self.y, 'Z': self.z}
        target = {'X': 0.0, 'Y': 0.0, 'Z': self.z}
        if coords['X'] is not None:
            target['X'] = coords['X'] if self.absolute else (self.x + coords['X'])
        if coords['Y'] is not None:
            target['Y'] = coords['Y'] if self.absolute else (self.y + coords['Y'])
        if coords['Z'] is not None:
            target['Z'] = coords['Z'] if self.absolute else (self.z + coords['Z'])

        # emit motion IR according to motion_mode
        if self.motion_mode == 'G0':
            # Rapid move: only emit if any axis changed
            if(target['X'], target['Y'], target['Z']) != (self.x, self.y, self.z):
                ir.append(RapidMove(target['X'], target['Y'], target['Z']))
                # update
                self.x, self.y, self.z = target['X'], target['Y'], target['Z']
        elif self.motion_mode == 'G1':
            if (target['X'], target['Y'], target['Z']) != (self.x, self.y, self.z):
                ir.append(LinearMove(target['X'], target['Y'], target['Z'], self.feed))
                # update
                self.x, self.y, self.z = target['X'], target['Y'], target['Z']
        elif self.motion_mode == 'G2':
            # for arcs I/J
            # R not implemented
            i = coords.get('I')
            j = coords.get('J')
            r = coords.get('R')
            cw = (self.motion_mode == 'G2')

            if(target['X'] is None) and (target['Y'] is None):
                # nothing to do for arc if endpoint missing
                pass
            # use I/J if present
            else:
                if i is not None or j is not None:
                    ir.append(ArcMove(cw, target['X'], target['Y'], i, j))
                    # update position
                    if target['X'] is not None: self.x = target['X']
                    if target['Y'] is not None: self.x = target['Y']
                elif r is not None:
                    # not implemented only approximated
                    ir.append(ArcMove(cw, target['X'], target['Y'], None, None))
                    if target['X'] is not None: self.x = target['X']
                    if target['Y'] is not None: self.x = target['Y']
                # else: non motion line TBD

        return ir

    # put parameters found in params
    def _apply_param_set(self, paramset):
        if paramset is None:
            return
        idx_expr = getattr(paramset, "index", None)
        val_expr = getattr(paramset, "value", None)
        idx_raw = self._eval_expr(idx_expr)
        if idx_raw is None:
            return
        try:
            idx = int(round(idx_raw))
        except Exception:
            return
        val = self._eval_expr(val_expr)
        self.params[idx] = val if val is not None else 0.0

    # handle G code that change modal state
    def _handle_g_code(self, gval, ir):
        if gval is None:
            return
        try:
            gnum = int(round(gval)) ##BEVARE THERE IS ALSO G38.2 this doesnt APPLY!!!!
        except Exception:
            return
        if gnum == 20:
            self.units = 'inch'
            ir.append(SetUnits('inch'))
        elif gnum == 21:
            self.units = 'mm'
            ir.append(SetUnits('mm'))
        elif gnum == 90:
            self.absolute = True
        elif gnum == 91:
            self.absolute = False
        elif gnum in (0, 1, 2, 3):
            self.motion_mode = f'G{gnum}'
        # else ignore other G codes for now

    def _handle_m_code(self, mval, ir):
        if mval is None:
            return
        try:
            mnum = int(round(mval)) # not correct ???
        except Exception:
            return
        # M codes
        if mnum == 3:
            # spindle on
            pass
        elif mnum == 5:
            # spindle off
            pass
        elif mnum in (2, 30):
            ir.append(ProgramEnd())

    # Expression evaluator
    def _eval_expr(self, expr) -> Optional[float]:
        if expr is None:
            return None

        if isinstance(expr, (int, float)):
            return float(expr)

        if isinstance(expr, tuple) and len(expr) >= 2:
            tag = expr[0]
            if tag == 'RealNumber':
                try:
                    return float(expr[1])
                except Exception:
                    return None
            if tag == 'ParameterRef' and len(expr) >= 2:
                return self._eval_expr(expr[1])

        # from here chatgpt!!! to end of function cuz I really don't know why it didn't work the way I implemented it

        # try to detect by attribute names (works for your dataclasses)
        # RealNumber -> .value
        if hasattr(expr, "value") and not hasattr(expr, "left") and not hasattr(expr, "arg"):
            # ParameterSet/ParameterRef/RealNumber share .value in different contexts.
            # Disambiguate: if value is numeric, treat as RealNumber-like; if value is expr and this object type is ParameterRef, treat accordingly.
            # If object also has a field that suggests parameter-ref, handle below.
            try:
                # if .value is basic numeric -> return it (RealNumber)
                if isinstance(expr.value, (int, float)):
                    return float(expr.value)
            except Exception:
                pass

        # ParameterRef (many variants: .value, .index, .index_expr)
        if self._is_instance_name(expr, "ParameterRef") or hasattr(expr, "index") or (
                hasattr(expr, "value") and self._looks_like_paramref(expr)):
            # index expression might be in .value or .index or .param_index or .index_expr
            idx_expr = getattr(expr, "value", None) if self._is_instance_name(expr, "ParameterRef") else None
            idx_expr = idx_expr or getattr(expr, "index", None) or getattr(expr, "index_expr", None) or getattr(
                expr, "param_index", None)
            idx_val = self._eval_expr(idx_expr)
            if idx_val is None:
                return 0.0
            try:
                idx = int(round(idx_val))
            except Exception:
                return 0.0
            return float(self.params.get(idx, 0.0))

        # RealNumber dataclass with .value
        if self._is_instance_name(expr, "RealNumber") or hasattr(expr, "value") and isinstance(
                getattr(expr, "value", None), (int, float)):
            try:
                return float(getattr(expr, "value"))
            except Exception:
                return None

        # UnaryOp
        if self._is_instance_name(expr, "UnaryOp") or hasattr(expr, "op") and hasattr(expr, "arg"):
            arg_val = self._eval_expr(getattr(expr, "arg", None))
            if arg_val is None:
                return None
            op = str(getattr(expr, "op", "")).upper()
            # trig functions expect degrees
            if op == 'SIN':
                return math.sin(math.radians(arg_val))
            if op == 'COS':
                return math.cos(math.radians(arg_val))
            if op == 'TAN':
                return math.tan(math.radians(arg_val))
            if op == 'ASIN':
                return math.degrees(math.asin(self._clamp(arg_val, -1, 1)))
            if op == 'ACOS':
                return math.degrees(math.acos(self._clamp(arg_val, -1, 1)))
            if op == 'ATAN':
                return math.degrees(math.atan(arg_val))
            if op == 'SQRT':
                return math.sqrt(arg_val)
            if op == 'EXP':
                return math.exp(arg_val)
            if op == 'LN':
                return math.log(arg_val)
            if op == 'ABS':
                return abs(arg_val)
            if op == 'FIX':
                return math.floor(arg_val)
            if op == 'FUP':
                return math.ceil(arg_val)
            if op == 'ROUND':
                return round(arg_val)
            # unknown unary -> passthrough
            return arg_val

        # BinaryOp
        if self._is_instance_name(expr, "BinaryOp") or (
                hasattr(expr, "left") and hasattr(expr, "right") and hasattr(expr, "op")):
            left = self._eval_expr(getattr(expr, "left", None))
            right = self._eval_expr(getattr(expr, "right", None))
            if left is None or right is None:
                return None
            op = str(getattr(expr, "op", "")).lower()
            if op == '+':
                return left + right
            if op == '-':
                return left - right
            if op == '*':
                return left * right
            if op == '/':
                return left / right
            if op == 'mod':
                return left % right
            if op == 'and':
                return 1.0 if (left != 0 and right != 0) else 0.0
            if op == 'or':
                return 1.0 if (left != 0 or right != 0) else 0.0
            if op == 'xor':
                return 1.0 if ((left != 0) ^ (right != 0)) else 0.0
            # fallback unknown operator
            return None

        # fallback: unknown shape -> None
        return None

    def _clamp(self, v, a, b):
        return max(a, min(b, v))

    def _is_instance_name(self, obj, name: str) -> bool:
        # Return True if obj is instance and its class name matches name
        try:
            return obj is not None and obj.__class__.__name__ == name
        except Exception:
            return False

    def _looks_like_paramref(self, obj) -> bool:
        try:
            if hasattr(obj, "value") and not isinstance(getattr(obj, "value"), (int, float)):
                if obj.__class__.__name__ == "RealNumber":
                    return True
        except Exception:
            pass
        return False