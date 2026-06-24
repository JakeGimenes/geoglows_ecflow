"""
The `ooflow` Python module adds new capabilities to the ecFlow Suite Definition API:

* Objects of type Task, Family, Variable, Repeat, Event and Meter
  can be combined into trigger expressions using native Python operators.

* Null Object helps reducing cyclomatic complexity in suite definitions.

* other convenient features

`ooflow` is backward-compatible with ecflow module, i.e. can be used as
a drop-in replacement, without changing the existing suite definition code.

Copyright 2024 ecmwf

Licensed under the Apache License, Version 2.0 (the "License")

http://www.apache.org/licenses/LICENSE-2.0
"""


import os
from functools import wraps
from packaging.version import parse as parse_version
import ecflow
from .py2 import reduce
try:
    from collections.abc import Iterable
except ImportError:
    from collections import Iterable

from collections import OrderedDict

ECFLOW_VERSION = parse_version(ecflow.__version__)
ECFLOW_4_8_0 = parse_version('4.8.0')
ECFLOW_5_0_0 = parse_version('5.0.0')
ECFLOW_5_2_2 = parse_version('5.2.2')
ECFLOW_5_5_3 = parse_version('5.5.3')


class ECFLOWVersionError(Exception):
    pass


if ECFLOW_VERSION >= ECFLOW_5_0_0 and ECFLOW_VERSION < ECFLOW_5_2_2:
    raise ECFLOWVersionError("ecFlow version {} not supported. "
            "You need ecFlow >= 5.2.2 or ecFlow < 5.0.0; "
            "See https://jira.ecmwf.int/browse/ECFLOW-1604".format(
                ecflow.__version__))


class Singleton(object):
    def __new__(cls, *args, **kwargs):
        if '_inst' not in vars(cls):
            cls._inst = object.__new__(cls, *args, **kwargs)
        return cls._inst



class Null(Singleton):
    """Base class for various Null Object classes"""
    def __init__(self, *args, **kwargs):
        pass




class NullByArg(object):
    """
    NullByArg mix-in. If any arg of the constructor
    is Null, create Null object rather than the normal object.
    """
    def __new__(cls, *args, **kwargs):
        if any([isinstance(arg, Null) for arg in args]):
            for c in cls.mro():
                null_class_name = 'Null' + c.__name__
                null_class = globals().get(null_class_name, None)
                if null_class is not None:
                    return null_class()
            # throw if Null* version of the class could not be found
            raise AttributeError('Unexpected Null argument in constructor')
        else:
            return super(NullByArg, cls).__new__(cls, *args, **kwargs)




class ExprRenderer(object):
    """
    Trigger expression renderer.
    Translates ooflow expression object into ecflow expression string.
    """
    def __init__(self, node):
        self.text = ''   #when visitor finishes, this will be the result.
        self.node = node #node to which expr is attached
        self.rank = 99   #rank of the last visited expr

    def visit_bin_bool_expr(self, expr):
        expr.left.accept(self)
        left_text = self.text
        left_rank = self.rank
        expr.right.accept(self)
        right_text = self.text
        right_rank = self.rank
        if not left_text and not right_text:
            self.text = ''
            return
        if not left_text:
            self.text = right_text
            self.rank = right_rank
            return
        if not right_text:
            self.text = left_text
            self.rank = left_rank
            return
        if left_rank < expr.rank:
            left_text = '(' + left_text + ')'
        if right_rank < expr.rank:
            right_text = '(' + right_text + ')'
        self.text = '{} {} {}'.format(left_text, expr.symb, right_text)
        self.rank = expr.rank

    def visit_bin_non_bool_expr(self, expr):
        expr.left.accept(self)
        left_text = self.text
        left_rank = self.rank
        expr.right.accept(self)
        right_text = self.text
        right_rank = self.rank
        if not left_text or not right_text:
            self.text = ''
            return
        if left_rank < expr.rank:
            left_text = '(' + left_text + ')'
        if right_rank < expr.rank:
            right_text = '(' + right_text + ')'
        self.text = '{} {} {}'.format(left_text, expr.symb, right_text)
        self.rank = expr.rank

    def visit_unary_bool_expr(self, expr):
        expr.operand.accept(self)
        text = self.text
        if not text:
            return
        if self.rank < expr.rank:
            text = '(' + text + ')'
        self.rank = expr.rank
        self.text = 'not ' + text

    def visit_int_literal(self, integer):
        self.text = integer.value
        self.rank = 99

    def visit_str_literal(self, string):
        self.text = string.value
        self.rank = 99

    def visit_node_attr(self, attr):
        target_path = attr.parent_path_relto(self.node)
        if not target_path:
            self.text = ''
            return
        self.text = ':'.join((target_path, attr.name()))
        self.rank = 99

    def visit_node_state(self, expr):
        target_path = expr.node_path_relto(self.node)
        if not target_path:
            self.text = ''
            return
        expected_state = expr.state
        self.text = '{} == {}'.format(target_path, expected_state)
        self.rank = expr.rank

    def visit_always(_self, expr):
        self.text = '1 == 1'
        self.rank = Eq.rank

    def visit_never(self, expr):
        self.text = '1 == 0'
        self.rank = Eq.rank

    def visit_across(self, across):
        across.operand.accept(self)
        this_var_name = across.var_name
        if across.other_var_name is None:
            other_var_name = across.var_name
        else:
            other_var_name = across.other_var_name
        expr_parent = self.node
        this_var = expr_parent.get_variable(this_var_name)
        if not this_var.name():
            return
        for target in across.operand.nodes:
            target_var = target.get_variable(other_var_name)
            if not target_var.name():
                # This target node does not see the variable.
                continue
            if this_var is target_var:
                # The node with the expression and this target node both
                # see the same variable so decoration is not needed.
                continue
            # Found a target which is in different scope.
            # Decorate the operand with "VAR clause" and finish.
            decorated_operand = (this_var < target_var) | (
                                 this_var == target_var) & across.operand
            decorated_operand.accept(self)
            break



class ExprNodeCollector(object):

    """
    Visitor for expression objects.
    Collects nodes referred to in an expression.
    """
    def __init__(self):
        self._nodes = set()

    @property
    def nodes(self):
        return list(self._nodes)

    def visit_node(self, node):
        self._nodes.add(node)

    def visit_node_state(self, expr):
        expr.node.accept(self)

    def visit_node_attr(self, attr):
        attr.parent.accept(self)

    def visit_bin_bool_expr(self, expr):
        expr.right.accept(self)
        expr.left.accept(self)

    def visit_bin_non_bool_expr(self, expr):
        expr.right.accept(self)
        expr.left.accept(self)

    def visit_unary_bool_expr(self, expr):
        expr.operand.accept(self)

    def visit_int_literal(self, expr):
        pass

    def visit_across(self, expr):
        pass


# -----------------------------------------
# Expression objects
# -----------------------------------------


# Mix-ins for expression operands.
# (Classes with these mix-ins can be
# used as operands in an expression)


class Operand(object):

    """
    Base class for expression operand.
    Expressions are also operands!
    """
    @property
    def text(self):
        """operand in ecflow text form"""
        renderer = ExprRenderer(node=self.expr_parent)
        self.accept(renderer)
        return renderer.text

    @property
    def nodes(self):
        """List of nodes referred to by this expression/operand"""
        collector = ExprNodeCollector()
        self.accept(collector)
        return collector.nodes

    @property
    def expr_parent(self):
        """Node to which this expression/operand is attached"""
        if isinstance(self._expr_parent, Node):
            return self._expr_parent
        if isinstance(self._expr_parent, Operand):
            return self._expr_parent.expr_parent
        return NullNode()

    @expr_parent.setter
    def expr_parent(self, parent):
        self._expr_parent = parent

    def across(self, var='YMD', other_var=None):
        """Wraps the expression/operand in an 'YMD clause'"""
        return Across(self, var, other_var)

    def __str__(self):
        return self.text


class NullOperand(Null):
    """
    Base class for various types of Null operands.
    """
    @property
    def text(self):
        return ''
    @property
    def nodes(self):
        return []
    @property
    def expr_parent(self):
        return NullNode()
    @expr_parent.setter
    def expr_parent(self, parent):
        pass
    def __bool__(self):
        return False
    __nonzero__ = __bool__ # for python2
    def __str__(self):
        return self.text


def _lit(x):
    if isinstance(x, int):
        return NumLiteral(x)
    if isinstance(x, str):
        return StrLiteral(x)
    if isinstance(x, float):
        return NumLiteral(x)
    return x


class Enum(Operand):
    """
    Operand that can appear in
    'x == y' and 'x != y' expressions
    """
    # call '.op_XXX()' of the other
    # operand. The result depends on
    # the type of the other operand
    # (double dispatch).

    def __eq__(self, other):
        return _lit(other).op_eq(self)

    def __ne__(self, other):
        return _lit(other).op_ne(self)

    def __req__(self, other):
        return _lit(other).op_req(self)

    def __rne__(self, other):
        return _lit(other).op_rne(self)

    # When the other operand is non-null,
    # it calls these (double dispatch)
    # and these return the expression object.

    def op_eq(self, other):
        return Eq(other, self)

    def op_req(self, other):
        return Eq(self, other)

    def op_ne(self, other):
        return Ne(other, self)

    def op_rne(self, other):
        return Ne(self, other)



class NullEnum(NullOperand):
    """
    Stands for 'missing' operand in
    'x == y' and 'x != y' expressions.
    Normally these expressions have Bool type,
    but if one or both operands are NullEnum,
    the expression degenerates to NullBool.
    """

    def accept(self, visitor):
        pass

    # call '.op_XXX()' of the other
    # operand. The result depends on
    # the type of the other operand
    # (double dispatch).

    def __eq__(self, other):
        if isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def __req__(self, other):
        if isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def __ne__(self, other):
        if not isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def __rne__(self, other):
        if not isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    # When the other operand is non-null, it calls
    # these (double dispatch) and the expression
    # degenerates to NullBool / TrueNullBool.

    def op_eq(self, other):
        if isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def op_req(self, other):
        if isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def op_ne(self, other):
        if not isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()

    def op_rne(self, other):
        if not isinstance(other, NullEnum):
            return TrueNullBool()
        else:
            return NullBool()



class Ordinal(Enum):
    """
    Operand that can appear in
    relational expressions (a>b, a<b, a>=b, a<=b, a!=b, a==b)
    and in arithmetic expressions (a+b, a-b, a*b, a%b).
    """
    def __lt__(self, other):
        return _lit(other).op_lt(self)
    def __gt__(self, other):
        return _lit(other).op_gt(self)
    def __le__(self, other):
        return _lit(other).op_le(self)
    def __ge__(self, other):
        return _lit(other).op_ge(self)
    def __add__(self, other):
        return _lit(other).op_add(self)
    def __radd__(self, other):
        return _lit(other).op_radd(self)
    def __sub__(self, other):
        return _lit(other).op_sub(self)
    def __rsub__(self, other):
        return _lit(other).op_rsub(self)
    def __mul__(self, other):
        return _lit(other).op_mul(self)
    def __rmul__(self, other):
        return _lit(other).op_rmul(self)
    def __floordiv__(self, other):
        return _lit(other).op_div(self)
    def __rfloordiv__(self, other):
        return _lit(other).op_rdiv(self)
    def __mod__(self, other):
        return _lit(other).op_mod(self)
    def __rmod__(self, other):
        return _lit(other).op_rmod(self)
    # When the other operand of RelationalExpr
    # is non-null, it calls these (double dispatch),
    # and these return the Bool object.
    def op_lt(self, other):
        return Lt(other, self)
    def op_gt(self, other):
        return Gt(other, self)
    def op_le(self, other):
        return Le(other, self)
    def op_ge(self, other):
        return Ge(other, self)
    # When the other operand of ArithmeticExpr
    # is non-null, it calls these (double dispatch)
    # and these return the Ordinal object.
    def op_add(self, other):
        return Add(other, self)
    def op_radd(self, other):
        return Add(self, other)
    def op_sub(self, other):
        return Sub(other, self)
    def op_rsub(self, other):
        return Sub(self, other)
    def op_mul(self, other):
        return Mul(other, self)
    def op_rmul(self, other):
        return Mul(self, other)
    def op_div(self, other):
        return Div(other, self)
    def op_rdiv(self, other):
        return Div(self, other)
    def op_mod(self, other):
        return Mod(other, self)
    def op_rmod(self, other):
        return Mod(self, other)
    def __getattr__(self, name):
        if name.startswith('op_'):
            raise RuntimeError('invalid expression operand')
        else:
            raise AttributeError(
                    '{} object has no attribute {}'.format(self.__class__,name))


class NullOrdinal(NullEnum):
    """
    Stands for 'missing' operand in RelationalExpr
    (a>b, a<b, a>=b, a<=b, a!=b, a==b). Normally these expressions
    are of Bool type, but when one or both operands are
    NullOrdinal, expression degenerates to NullBool.

    Also stands for 'missing' operand in ArithmeticExpr
    (a+b, a-b, a*b, a/b, a%b). Normally these expressions are of
    Ordinal type, but when one or both operands are NullOrdinal,
    expression degenerates to NullOrdinal.
    """
    def accept(self, visitor):
        pass
    def __lt__(self, other):
        return NullBool()
    def __gt__(self, other):
        return NullBool()
    def __le__(self, other):
        return NullBool()
    def __ge__(self, other):
        return NullBool()
    def __add__(self, other):
        return self
    def __radd__(self, other):
        return self
    def __sub__(self, other):
        return self
    def __rsub__(self, other):
        return self
    def __mul__(self, other):
        return self
    def __rmul__(self, other):
        return self
    def __floordiv__(self, other):
        return self
    def __rfloordiv__(self, other):
        return self
    def __mod__(self, other):
        return self
    def __rmod__(self, other):
        return self
    # When the other operand of RelationalExpr is non-null,
    # it calls these (double dispatch) and these return
    # NullBool object (i.e. the expression
    # degenerates to NullBool).
    def op_lt(self, other):
        return NullBool()
    def op_gt(self, other):
        return NullBool()
    def op_le(self, other):
        return NullBool()
    def op_ge(self, other):
        return NullBool()
    # When the other operand of ArithmeticExpr is non-null,
    # it calls these (double dispatch) and these return
    # NullOrdinal object (i.e. the expression
    # degenerates to NullOrdinal).
    def op_add(self, other):
        return self
    def op_radd(self, other):
        return self
    def op_sub(self, other):
        return self
    def op_rsub(self, other):
        return self
    def op_mul(self, other):
        return self
    def op_rmul(self, other):
        return self
    def op_div(self, other):
        return other
    def op_rdiv(self, other):
        return other
    def op_mod(self, other):
        return self
    def op_rmod(self, other):
        return self


class NumLiteral(Ordinal):
    """
    Wrapper for built-in <int> and <float> types.
    """
    def __init__(self, value):
        self._expr_parent = NullNode()
        self.value = str(value)
    def _bool_eq(self, other):
        return self.value == other.value
    def accept(self, visitor):
        return visitor.visit_int_literal(self)


class Bool(Enum):
    """
    Operand that can appear in BinBoolExpr ('a & b', 'a | b', '~a').
    It is also Enum so that one can construct 'a == b' and 'a != b'
    expressions for comparing two Bool objects.
    """
    def __and__(self, other):
        return _lit(other).op_and(self)
    def __iand__(self, other):
        return _lit(other).op_and(self)
    def __rand__(self, other):
        return _lit(other).op_and(self)
    def __or__(self, other):
        return _lit(other).op_or(self)
    def __ior__(self, other):
        return _lit(other).op_or(self)
    def __ror__(self, other):
        return _lit(other).op_or(self)
    def __invert__(self):
        return Not(self)
    # when the other operand is non-null,
    # it calls these (double dispatch)
    # and these return Bool object.
    def op_and(self, other):
        return And(other, self)
    def op_or(self, other):
        return Or(other, self)



class NullBool(NullEnum, Bool):
    """
    Stands for missing operand in BinBoolExpr
    ('a & b', 'a | b'). When one of the operands in
    'a & b' and 'a | b' expressions is NullBool
    then the expression degenerates to the other operand.

    Also stands for 'missing' operand in UnaryBoolExpr (~ a).
    When the operand of '~ a' expression is NullBool,
    the expression degenerates to NullBool.
    """
    def accept(self, visitor):
        pass
    def __and__(self, other):
        return other
    def __iand__(self, other):
        return other
    def __rand__(self, other):
        return other
    def __or__(self, other):
        return other
    def __ior__(self, other):
        return other
    def __ror__(self, other):
        return other
    def __invert__(self):
        return self
    # when the other operand is non-null,
    # it calls these, and the expression
    # degenerates to the other operand.
    def op_and(self, other):
        return other
    def op_or(self, other):
        return other


class TrueNullBool(NullBool):
    # this version of NullBool evaluates
    # to true in boolean contexts.
    # (do we need this one?)
    def __bool__(self):
        return True
    __nonzero__ = __bool__ # for python2


class StrLiteral(Bool):
    """
    Wrapper for built-in <str> type.
    """
    def __init__(self, value):
        self.value = value
        self._expr_parent = NullNode()
    def _bool_eq(self, other):
        return self.value == other.value
    def accept(self, visitor):
        visitor.visit_str_literal(self)



# ---------------------------------------
# Expressions (which are also Operands!)
# ---------------------------------------


class BinBoolExpr(Bool):
    """
    Binary bool expressions "x & y" and "x | y".
    Operands are of Bool type.
    BinBoolExpr is also Bool (can itself
    appear in BinBoolExpr or UnaryBoolExpr).
    """
    def __init__(self, left, right):
        #if not isinstance(left, Bool):
        #    raise TypeError('left operand has invald type {}'.format(type(left)))
        #if not isinstance(right, Bool):
        #    raise TypeError('right operand has invald type {}'.format(type(right)))
        left.expr_parent = self
        right.expr_parent = self
        self.left = left
        self.right = right
        self._expr_parent = NullNode()

    def accept(self, visitor):
        visitor.visit_bin_bool_expr(self)



class UnaryBoolExpr(Bool):
    """
    Unary boolean expression "~ x".
    Operand is of Bool type.
    UnaryBoolExpr is also Bool (can itself
    be an operand in BinBoolExpr or UnaryBoolExpr).
    """
    def __init__(self, operand):
        operand.expr_parent = self
        self.operand = operand
        self._expr_parent = NullNode()
    def accept(self, visitor):
        visitor.visit_unary_bool_expr(self)


class Or(BinBoolExpr):
    rank = 1
    symb = 'or'


class And(BinBoolExpr):
    rank = 2
    symb = 'and'


class Not(UnaryBoolExpr):
    rank = 3
    symb = 'not'


class BinNonBoolExpr(object):
    """
    Base class for BinArithmeticExpr and BinRelExpr (but not for BinBoolExpr).
    """
    def __init__(self, left, right):
        if not isinstance(left, Enum):
            raise TypeError('left operand has invald type {}'.format(type(left)))
        if not isinstance(right, Enum):
            raise TypeError('right operand has invald type {}'.format(type(right)))
        left.expr_parent = self
        right.expr_parent = self
        self.left = left
        self.right = right
        self._expr_parent = NullNode()

    def accept(self, visitor):
        visitor.visit_bin_non_bool_expr(self)


class RelationalExpr(BinNonBoolExpr, Bool):
    """
    Binary relational expression (a>b, a<b, a>=b, a<=b, a==b, a!=b).
    Operands are of Ordinal type.
    RelationalExpr is also Bool (can itself
    be an operand in a BinBoolExpr or UnaryBoolExpr).
    """
    pass


class Lt(RelationalExpr):
    rank = 4
    symb = '<'


class Gt(RelationalExpr):
    rank = 4
    symb = '>'


class Ge(RelationalExpr):
    rank = 4
    symb = '>='


class Le(RelationalExpr):
    rank = 4
    symb = '<='


class Eq(RelationalExpr):
    rank = 4
    symb = '=='
    # __bool__ is called when Eq expression
    # appears in boolean context. Returns True
    # when expression operands are equal, False otherwise.
    def __bool__(self):
        a = self.left
        b = self.right
        if type(a) != type(b):
            return False
        if not (hasattr(a, '_bool_eq') and hasattr(b, '_bool_eq')):
            return False
        return a._bool_eq(b)
    __nonzero__ = __bool__ # for python2


class Ne(RelationalExpr):
    rank = 4
    symb = '!='
    # __bool__ is called when Ne expression
    # appears in boolean context. Returns True
    # when expression operands are not equal, False otherwise.
    def __bool__(self):
        a = self.left
        b = self.right
        if type(a) != type(b):
            return True
        if not (hasattr(a, '_bool_eq') and hasattr(b, '_bool_eq')):
            return True
        return not a._bool_eq(b)
    __nonzero__ = __bool__ # for python2


class ArithmeticExpr(BinNonBoolExpr, Ordinal):
    """
    Binary arithmetic expression (a+b, a-b, a*b, a%b).
    Operands are of Ordinal type.
    ArithmeticExpr is also Ordinal (can itself
    be an operand in an ArithmeticExpr or in RelationalExpr).
    """
    pass


class Add(ArithmeticExpr):
    rank = 4
    symb = '+'


class Sub(ArithmeticExpr):
    rank = 4
    symb = '-'


class Mul(ArithmeticExpr):
    rank = 5
    symb = '*'


class Div(ArithmeticExpr):
    rank = 5
    symb = '/'


class Mod(ArithmeticExpr):
    rank = 5
    symb = '%'


class NodeState(Bool):
    """
    Base class for objects representing node status (complete, aborted, etc.).
    NodeState can appear as an operand in BinBoolExpr or UnaryBoolExpr.
    Translates to "/path/to/node == complete", "/path/to/node == aborted" etc.
    """
    def __init__(self, node):
        """
        Args:
            node (Node): the node
        """
        self.node = node
        self._expr_parent = NullNode()
    @property
    def node_path(self):
        return self.node.path
    def node_path_relto(self, other):
        return self.node.path_relto(other)
    def accept(self, visitor):
        visitor.visit_node_state(self)


class Submitted(NodeState):
    rank = 4
    state = 'submitted'


class Active(NodeState):
    rank = 4
    state = 'active'


class Suspended(NodeState):
    rank = 4
    state = 'suspended'


class Aborted(NodeState):
    rank = 4
    state = 'aborted'


class Queued(NodeState):
    rank = 4
    state = 'queued'


class Completed(NodeState):
    rank = 4
    state = 'complete'


class Unknown(NodeState):
    rank = 4
    state = 'unknown'



class Across(Bool):
    """
    The Across() expression decorates it's
    operand in an 'YMD clause' style if the operand
    (expression) contains a node belonging to a different
    variable scope than the expression parent.
    """
    def __init__(self, expr, var='YMD', other_var=None):
        self.expr_parent = NullNode()
        self.operand = expr
        self.var_name = var
        self.other_var_name = other_var
    def accept(self, visitor):
        visitor.visit_across(self)



# -----------------------------------------------
# Nodes, Node Attributes and their Null versions
# -----------------------------------------------


class NullNode(Null):
    """
    Stands for 'missing' Node object.
    Looks like a Node object but "does nothing".
    """
    def __init__(self):
        self.path = ''
        self.complete = NullBool()
        self.aborted = NullBool()
        self.active = NullBool()
        self.suspended = NullBool()
        self.queued = NullBool()
        self.submitted = NullBool()
        self.unknown = NullBool()
        self.crons = iter([])
        self.dates = iter([])
        self.days = iter([])
        self.events = iter([])
        self.inlimits = iter([])
        self.labels = iter([])
        self.limits = iter([])
        self.meters = iter([])
        self.times = iter([])
        self.todays = iter([])
        self.variables = iter([])
        self.verifies = iter([])
        self.zombies = iter([])
        self.trigger = NullBool()
        self.defuser = NullBool()
    @property
    def parent(self):
        return NullNode()
    @parent.setter
    def parent(self, parent):
        pass
    def find_node_up_the_tree(self, *args, **kwargs):
        return NullNode()
    def get_abs_node_path(self):
        return ''
    def get_complete(self):
        return NullBool()
    def get_trigger(self):
        return NullBool()
    def get_parent(self):
        return NullNode()
    def get_repeat(self):
        return NullRepeat()
    def name(self):
        return ''
    def get_variable(self, name):
        return NullVariable()
    def related_to(self, other):
        return False
    def path_relto(self, other):
        return ''
    def accept(self, visitor):
        pass
    def add_to(self, node):
        pass
    def __bool__(self):
        return False
    __nonzero__ = __bool__ # for python2
    def __get_self(self, *args, **kwargs):
        return self
    def __getattr__(self, name):
        if name.startswith('add_'):
            return self.__get_self
        if name.startswith('delete_'):
            return
        if name.startswith('evaluate_'):
            return False
        if name.startswith('find_'):
            return NullNodeAttr()
        if name.startswith('get_'):
            return NullNodeAttr()
        raise AttributeError(
           '{} object has no attribute {}'.format(self.__class__, name))




class KnowsOwner(object):
    """
    Object with this mix-in has a reference to its parent.
    Mixed into node attributes which can appear in trigger expressions.
    """
    parent = NullNode()

    def parent_path_relto(self, other):
        """
        Path of the owner, relative to another node.
        """
        return self.parent.path_relto(other)

    @property
    def parent_path(self):
        """
        Path of the owner.
        """
        return self.parent.path



class NodeAttr(object):
    """
    Base class for node attributes
    (variable, repeat, event, trigger, complete, defstatus, limit, inlimit,
    ...)
    """
    def accept(self, visitor):
        visitor.visit_node_attr(self)



class NullNodeAttr(NodeAttr, Null):
    """
    Base class for various Null versions of node attributes
    """
    def accept(self, visitor):
        pass
    def name(self):
        return ''
    @property
    def parent(self):
        return NullNode()
    @parent.setter
    def parent(self, node):
        pass
    def add_to(self, node):
        pass



class NullVariable(NullNodeAttr, NullOrdinal):
    def empty(self):
        return True
    def value(self):
        return ''


class NullRepeat(NullVariable, NullNodeAttr):
    def start(self):
        return 0
    def end(self):
        return 0
    def step(self):
        return 0
    def add_to(self, node):
        node.delete_repeat()


class NullRepeatDate(NullRepeat):
    pass


class NullMeter(NullVariable):
    def color_change(self):
        return 0
    def min(self):
        return 0
    def max(self):
        return 0


class NullEvent(NullNodeAttr, NullBool):
    def empty(self):
        return True
    def name(self):
        return ''
    def number(self):
        return (1 << 31) - 1


class NullLimit(NullNodeAttr):
    def limit(self):
        return 0
    @property
    def node_paths(self):
        return []
    def value(self):
        return 0


class NullInLimit(NullNodeAttr):
    def path_to_node(self):
        return ''
    def tokens(self):
        return 0
    @property
    def limit(self):
        return NullLimit()

class SubmitInLimit(ecflow.InLimit):
    def path_to_node(self):
        return ''
    def tokens(self):
        return 0
    @property
    def limit(self):
        return NullLimit()

class FamilyInLimit(ecflow.InLimit):
    def path_to_node(self):
        return ''
    def tokens(self):
        return 0
    @property
    def limit(self):
        return NullLimit()





def _mix_in(cls, *bases):
    # helper function for adding mix-ins to an existing class.
    cls.__bases__ = tuple(bases) + tuple(cls.__bases__)


# Trigger, Defuser, Defstatus node attributes.
# Object of these classes are meant to be passed
# to the generic Node.add() method.


if ECFLOW_VERSION < ECFLOW_4_8_0:

    # Before 4.8.0, ecFlow did not have 'Trigger' class.
    # since 4.8.0, we need to monkey-patch ecFlow.Trigger
    # instead of defining our own Trigger class.

    class Trigger(NodeAttr, KnowsOwner):

        """
        Trigger class holds trigger expression.
        For use as an argument in a generic node.add() method, e.g.:
           task.add(Trigger(event1 & event2))
        Does not exist in ecflow.
        """
        def __init__(self, expr):
            self._parent = NullNode()
            if isinstance(expr, ecflow.PartExpression):
                expr = str(expr)
            elif isinstance(expr, Iterable) and not isinstance(expr, str):
                expr = all_complete(expr)
            elif isinstance(expr, Trigger):
                expr = expr.expr
            self.expr = expr

        @property
        def parent(self):
            return self._parent

        @parent.setter
        def parent(self, node):
            self._parent = node
            if not isinstance(self.expr, str):
                self.expr.expr_parent = node

        def add_to(self, node):
            return node.add_trigger(self)

        def __str__(self):
            return str(self.expr)

        def get_expression(self):
            return str(self.expr)

else:

    # since 4.8.0 we need to monkey-patch ecflow.Trigger class

    from ecflow import Trigger

    def _Trigger_init_wrapper(init):
        @wraps(init)
        def wrapper(self, expr, bool=True):
            init(self, 'dummy_expr') #because expr may be something else than string.
            self._parent = NullNode()
            if isinstance(expr, ecflow.PartExpression):
                expr = str(expr)
            elif isinstance(expr, Iterable) and not isinstance(expr, str):
                expr = all_complete(expr)
            elif isinstance(expr, Trigger):
                expr = expr.expr
            self.expr = expr
        return wrapper

    def _Trigger_add_to(self, node):
        return node.add_trigger(self)

    def _Trigger_parent_getter(self):
        return node._parent

    def _Trigger_parent_setter(self, node):
        self._parent = node
        if not isinstance(self.expr, str):
            self.expr.expr_parent = node

    _mix_in(ecflow.Trigger, NodeAttr, KnowsOwner)
    setattr(ecflow.Trigger, '__init__', _Trigger_init_wrapper(ecflow.Trigger.__init__))
    setattr(ecflow.Trigger, 'add_to', _Trigger_add_to)
    setattr(ecflow.Trigger, 'parent', property(_Trigger_parent_getter, _Trigger_parent_setter))
    setattr(ecflow.Trigger, '__str__', lambda self: str(self.expr))
    setattr(ecflow.Trigger, 'get_expression', lambda self: str(self.expr))



class NullTrigger(Null):

    @property
    def parent(self):
        return NullNode()

    @parent.setter
    def parent(self, node):
        pass

    @property
    def expr(self):
        return NullBool()

    @expr.setter
    def expr(self, e):
        pass

    def add_to(self, node):
        return node.add_trigger(self)



class Defuser(Trigger):
    """
    Defuser class holds 'a complete' expression.
    For use as an argument in a generic node.add() method, e.g.:
       task.add(Defuser(event1 & event2))
    Does not exist in ecflow.
    """
    def add_to(self, node):
        return node.add_complete(self)



class NullDefuser(NullTrigger):
    pass


def _get_dstate(name):
    dstates = dict(
            submitted = ecflow.DState.submitted,
            active = ecflow.DState.active,
            suspended = ecflow.DState.suspended,
            aborted = ecflow.DState.aborted,
            queued = ecflow.DState.queued,
            complete = ecflow.DState.complete,
            unknown = ecflow.DState.unknown,
            )
    return dstates[name]

# shorter...
submitted =  _get_dstate('submitted')
active = _get_dstate('active')
suspended = _get_dstate('suspended')
aborted = _get_dstate('aborted')
queued = _get_dstate('queued')
complete = _get_dstate('complete')
unknown = _get_dstate('unknown')



if ECFLOW_VERSION < ECFLOW_4_8_0:

    # Before 4.8.0, ecFlow did not have 'Defstatus' class.
    # since 4.8.0, we need to monkey-patch ecFlow.Defstatus

    class Defstatus(NodeAttr):
        def __init__(self, status):
            self.status = str(status)
        def __str__(self):
            return self.status
        def state(self):
            return DState(_get_dstate(self.status))
        def add_to(self, node):
            return node.add_defstatus(self.state())

else:

    def _Defstatus_init_wrapper(init):
        @wraps(init)
        def wrapper(self, status):
            init(self, str(status))
            self.status = str(status)
        return wrapper

    def _Defstatus_add_to(self, node):
        return node.add_defstatus(self.state())

    _mix_in(ecflow.Defstatus, NodeAttr)
    setattr(ecflow.Defstatus, '__init__', _Defstatus_init_wrapper(ecflow.Defstatus.__init__))
    setattr(ecflow.Defstatus, 'add_to', _Defstatus_add_to)



# --------------------------------------------------------------------
# monkey-patching ecflow.Variable class:
# - variable objects can appear as operands in trigger expressions.
# - constructor will additionally accept a non-string (e.g. int) value
# --------------------------------------------------------------------




_mix_in(ecflow.Variable, Ordinal, NodeAttr, KnowsOwner, NullByArg)

def _Variable_init_wrapper(init):
    @wraps(init)
    def wrapper(self, name, value):
        return init(self, name, str(value))
    return wrapper

def _Variable_add_to(self, node):
    node.add_full_variable(self)

setattr(ecflow.Variable, '__init__', _Variable_init_wrapper(ecflow.Variable.__init__))
setattr(ecflow.Variable, 'add_to', _Variable_add_to)
setattr(ecflow.Variable, '__copy__', lambda self: Variable(self.name(), self.value()))
# Redefining __eq__ so that it returns Eq expression object rather than bool value.
# Saving the original method as ._bool_eq, to be used by Eq.__bool__()
setattr(ecflow.Variable, '_bool_eq', ecflow.Variable.__eq__)
setattr(ecflow.Variable, '__eq__', lambda self, other: _lit(other).op_eq(self))
# Redefininig __lt__ so that it returns Lt expression object rather than failing
# Since ecFlow 5.4.0
setattr(ecflow.Variable, '__lt__', lambda self, other: _lit(other).op_lt(self))


# ---------------------------------------------------------------------
# monkey-patching ecflow.Repeat* classes:
# - object of Repeat* class can appear as operand in trigger expressions
# - constructor of RepeatEnumerated additionally accepts non-string list
# ---------------------------------------------------------------------


_mix_in(ecflow.RepeatDate, Ordinal, NodeAttr, KnowsOwner)
_mix_in(ecflow.RepeatInteger, Ordinal, NodeAttr, KnowsOwner)
_mix_in(ecflow.RepeatString, Enum, NodeAttr, KnowsOwner)
_mix_in(ecflow.RepeatEnumerated, Ordinal, NodeAttr, KnowsOwner)
# RepatDay is an odd one, it cannot be referred to in a trigger.
_mix_in(ecflow.RepeatDay, NodeAttr, KnowsOwner)


def _RepeatEnumerated_init_wrapper(init):
    @wraps(init)
    def wrapper(self, name, items):
        init(self, name, [str(x) for x in items])
    return wrapper


setattr(ecflow.RepeatEnumerated, '__init__',
        _RepeatEnumerated_init_wrapper(ecflow.RepeatEnumerated.__init__))
# Make repeats acceptable by Node.add() method
setattr(ecflow.RepeatDate, 'add_to', lambda self, node: node.add_full_repeat(self))
setattr(ecflow.RepeatInteger, 'add_to', lambda self, node: node.add_full_repeat(self))
setattr(ecflow.RepeatString, 'add_to', lambda self, node: node.add_full_repeat(self))
setattr(ecflow.RepeatEnumerated, 'add_to', lambda self, node: node.add_full_repeat(self))
setattr(ecflow.RepeatDay, 'add_to', lambda self, node: node.add_full_repeat(self))
# Redefine __eq__ to return Eq expression object.
# Save original __eq__ as _bool_eq to be used in Eq.__bool__()
setattr(ecflow.RepeatDate, '_bool_eq', ecflow.RepeatDate.__eq__)
setattr(ecflow.RepeatDate, '__eq__', lambda self, other: _lit(other).op_eq(self))
setattr(ecflow.RepeatInteger, '_bool_eq', ecflow.RepeatInteger.__eq__)
setattr(ecflow.RepeatInteger, '__eq__', lambda self, other: _lit(other).op_eq(self))
setattr(ecflow.RepeatString, '_bool_eq', ecflow.RepeatString.__eq__)
setattr(ecflow.RepeatString, '__eq__', lambda self, other: _lit(other).op_eq(self))
setattr(ecflow.RepeatEnumerated, '_bool_eq', ecflow.RepeatEnumerated.__eq__)
setattr(ecflow.RepeatEnumerated, '__eq__', lambda self, other: _lit(other).op_eq(self))


# RepeatIntegerList class.
# Don't use this class. It has been used in several
# suites. Should have used RepeatEnumerated instead.
# Deprecated.

#class RepeatIntegerList(ecflow.RepeatEnumerated):
#    pass




# ----------------------------------------------------------------
# monkey-patching ecflow.Event class
# ----------------------------------------------------------------


_mix_in(ecflow.Event, Bool, NodeAttr, KnowsOwner)

setattr(ecflow.Event, 'add_to', lambda self, node: node.add_full_event(self))
setattr(ecflow.Event, '_expr_parent', NullNode())
setattr(ecflow.Event, '_bool_eq', ecflow.Event.__eq__)
setattr(ecflow.Event, '__eq__', lambda self, other: Eq(self, other))


# ----------------------------------------------------------------
# monkey-patching ecflow.Meter class:
# - Meter objects can now be used as operands in expressions
# ----------------------------------------------------------------


_mix_in(ecflow.Meter, Ordinal, NodeAttr, KnowsOwner)

setattr(ecflow.Meter, 'add_to', lambda self, node: node.add_full_meter(self))
setattr(ecflow.Meter, '_bool_eq', ecflow.Meter.__eq__)
setattr(ecflow.Meter, '__eq__', lambda self, other: _lit(other).op_eq(self))


# ----------------------------------------------------------------
# monkey-patching ecflow.Limit class:
# - Limit object can now be attached with generic .add() method.
# ---------------------------------------------------------------


_mix_in(ecflow.Limit, NodeAttr, KnowsOwner)

setattr(ecflow.Limit, 'add_to', lambda self, node: node.add_full_limit(self))


# ----------------------------------------------------------------
# monkey-patching ecflow.InLimit class
# - InLimit object can be added with generic .add() method.
# - constructor additionally accepts Limit object as an argument.
# - additional .limit property holds reference to the limit.
# ----------------------------------------------------------------


_mix_in(ecflow.InLimit, NodeAttr, NullByArg)

def _SubmitInLimit_init_wrapper(init):
    @wraps(init)
    def wrapper(self, limit, *args):
        if ECFLOW_VERSION < ECFLOW_5_5_3:
            raise ECFLOWVersionError("ecFlow version {} does not support this feature "
                "You need ecFlow >= 5.5.3 for this feature;".format(ECFLOW_VERSION))
        self.parent = NullNode()
        if isinstance(limit, str):
            init(self, limit, '', 1, False, True)
        else:
            # Store reference to the limit object for delayed rendering.
            # self.limit being set determines whether the InLimit
            # is to be added immediately (ecflow inlimit)
            # or for delayed rendering (ooflow inlimit)
            #self.limit = limit
            init(self, limit.name(), *args)
    return wrapper


def _SubmitInLimit_name_wrapper(name):
    @wraps(name)
    def wrapper(self):
        s = self.limit.name()
        if not s:
            return name(self)
        else:
            return s
    return wrapper


def _SubmitInLimit_path_to_node_wrapper(path_to_node):
    @wraps(path_to_node)
    def wrapper(self):
        return self.limit.parent.path_relto(self.parent)
        if not path:
            return path_to_node(self)
        else:
            return path
    return wrapper


def _FamilyInLimit_init_wrapper(init):
    @wraps(init)
    def wrapper(self, limit, *args):
        if ECFLOW_VERSION < ECFLOW_5_5_3:
            raise ECFLOWVersionError("ecFlow version {} does not support this feature "
                "You need ecFlow >= 5.5.3 for this feature;".format(ECFLOW_VERSION))
        self.parent = NullNode()
        if isinstance(limit, str):
            init(self, limit, '', 1, True, False)
        else:
            # Store reference to the limit object for delayed rendering.
            # self.limit being set determines whether the InLimit
            # is to be added immediately (ecflow inlimit)
            # or for delayed rendering (ooflow inlimit)
            self.limit = limit
            #init(self, limit.name(), *args)
    return wrapper


def _FamilyInLimit_name_wrapper(name):
    @wraps(name)
    def wrapper(self):
        s = self.limit.name()
        if not s:
            return name(self)
        else:
            return s
    return wrapper


def _FamilyInLimit_path_to_node_wrapper(path_to_node):
    @wraps(path_to_node)
    def wrapper(self):
        self.parent = NullNode()
        if isinstance(limit, str):
            init(self, limit, *args)
        else:
            # Store reference to the limit object for delayed rendering.
            # self.limit being set determines whether the InLimit
            # is to be added immediately (ecflow inlimit)
            # or for delayed rendering (ooflow inlimit)
            self.limit = limit
            #init(self, limit.name(), *args)
    return wrapper


def _InLimit_init_wrapper(init):
    @wraps(init)
    def wrapper(self, limit, *args):
        self.parent = NullNode()
        if isinstance(limit, str):
            init(self, limit, *args)
        else:
            # Store reference to the limit object for delayed rendering.
            # self.limit being set determines whether the InLimit
            # is to be added immediately (ecflow inlimit)
            # or for delayed rendering (ooflow inlimit)
            self.limit = limit
            #init(self, limit.name(), *args)
    return wrapper


def _InLimit_name_wrapper(name):
    @wraps(name)
    def wrapper(self):
        s = self.limit.name()
        if not s:
            return name(self)
        else:
            return s
    return wrapper


def _InLimit_path_to_node_wrapper(path_to_node):
    @wraps(path_to_node)
    def wrapper(self):
        return self.limit.parent.path_relto(self.parent)
        if not path:
            return path_to_node(self)
        else:
            return path
    return wrapper


setattr(ecflow.InLimit, 'limit', NullLimit())
setattr(ecflow.InLimit, '__init__', _InLimit_init_wrapper(ecflow.InLimit.__init__))
setattr(ecflow.InLimit, 'name', _InLimit_name_wrapper(ecflow.InLimit.name))
setattr(ecflow.InLimit, 'path_to_node', _InLimit_path_to_node_wrapper(ecflow.InLimit.path_to_node))
setattr(ecflow.InLimit, 'add_to', lambda self, node: node.add_inlimit(self))

setattr(SubmitInLimit, '__init__', _SubmitInLimit_init_wrapper(ecflow.InLimit.__init__))
setattr(FamilyInLimit, '__init__', _FamilyInLimit_init_wrapper(ecflow.InLimit.__init__))


# ----------------------------------------------------------------
# monkey-patching other node attributes so that they can
# be attached using generic Node.add() method.
# ----------------------------------------------------------------

_mix_in(ecflow.Autocancel, NodeAttr)
_mix_in(ecflow.Clock, NodeAttr)
_mix_in(ecflow.Cron, NodeAttr)
_mix_in(ecflow.Date, NodeAttr)
_mix_in(ecflow.Day, NodeAttr)
_mix_in(ecflow.Label, NodeAttr)
_mix_in(ecflow.Late, NodeAttr)
_mix_in(ecflow.Time, NodeAttr)
_mix_in(ecflow.Today, NodeAttr)
_mix_in(ecflow.Verify, NodeAttr)
_mix_in(ecflow.ZombieAttr, NodeAttr)

setattr(ecflow.Autocancel, 'add_to', lambda self, node: node.add_autocancel(self))
setattr(ecflow.Clock, 'add_to', lambda self, node: node.add_clock(self))
setattr(ecflow.Cron, 'add_to', lambda self, node: node.add_cron(self))
setattr(ecflow.Date, 'add_to', lambda self, node: node.add_date(self))
setattr(ecflow.Day, 'add_to', lambda self, node: node.add_day(self))
setattr(ecflow.Label, 'add_to', lambda self, node: node.add_label(self))
setattr(ecflow.Late, 'add_to', lambda self, node: node.add_late(self))
setattr(ecflow.Time, 'add_to', lambda self, node: node.add_time(self))
setattr(ecflow.Today, 'add_to', lambda self, node: node.add_today(self))
setattr(ecflow.Verify, 'add_to', lambda self, node: node.add_verify(self))
setattr(ecflow.ZombieAttr, 'add_to', lambda self, node: node.add_zombie(self))



# ---------------------------------------------------------------
# monkey-patching Suite, Family and Task
# ---------------------------------------------------------------


_mix_in(ecflow.Family, KnowsOwner)
_mix_in(ecflow.Task, KnowsOwner)

def _Node_init_wrapper(init):
    @wraps(init)
    def wrapper(self, *args):
        init(self, *args)
        self.parent = NullNode()
        self.__trigger = NullBool()
        self.__defuser =  NullBool()
        self.__repeat = NullRepeat()
        self.__variables = OrderedDict()
        self.__meters = OrderedDict()
        self.__limits = OrderedDict()
        self.__inlimits = OrderedDict()
        self.__events_by_name = OrderedDict()
        self.__events_by_number = OrderedDict()
    return wrapper

setattr(ecflow.Suite, '__init__', _Node_init_wrapper(ecflow.Suite.__init__))
setattr(ecflow.Suite, 'accept', lambda self, visitor: visitor.visit_node(self))

def _Family_add_to(self, node):
    node.ecflow_add_family(self)
    self.parent = node

setattr(ecflow.Family, '__init__', _Node_init_wrapper(ecflow.Family.__init__))
setattr(ecflow.Family, 'accept', lambda self, visitor: visitor.visit_node(self))
setattr(ecflow.Family, 'add_to', _Family_add_to)

def _Task_add_to(self, node):
    node.ecflow_add_task(self)
    self.parent = node

setattr(ecflow.Task, '__init__', _Node_init_wrapper(ecflow.Task.__init__))
setattr(ecflow.Task, 'accept', lambda self, visitor: visitor.visit_node(self))
setattr(ecflow.Task, 'add_to', _Task_add_to)


# ---------------------------------------------------------------
# monkey-patching ecflow.Defs class
# ---------------------------------------------------------------


def _Defs_add_suite_wrapper(add_suite):
    @wraps(add_suite)
    def wrapper(self, suite):
        add_suite(self, suite)
    return wrapper

setattr(ecflow.Defs, 'add_suite', _Defs_add_suite_wrapper(ecflow.Defs.add_suite))


# ----------------------------------------------------------------
# monkey-patching ecflow.Node class:
# - generic .add() method
# - decorated add_variable(), find_variable(), delete_variable() and .variables
# - decorated add_repeat(), get_repeat(), delete_repeat() and .repeats
# - decorated add_meter(), find_meter(), delete_meter() and .meters
# - .add_trigger() now accepts expression object as an argument
# - .get_trigger() returns original rather than copy
# - trigger can be also set by assigning expression to .trigger property
# - .add_complete() now accepts expression object
# - .get_complete() returns original object rather than copy
# - a complete can be also set by assigning expression to .defuser property
# - properties for querying node status in trigger expressions
# - methods/properties for querying relationships to other nodes.
# ----------------------------------------------------------------



# The generic .add() method


def _Node_add(self, *items):
    for item in items:
        if isinstance(item, Iterable) and not isinstance(item, Node):
            self.add(*item)
        else:
            item.add_to(self)
    return self



# Properties for querying node
# status in trigger expressions
# (e.g. t3.trigger = t1.complete & t2.aborted)


def _Node_submitted_getter(self):
    return Submitted(self)

def _Node_active_getter(self):
    return Active(self)

def _Node_suspended_getter(self):
    return Suspended(self)

def _Node_aborted_getter(self):
    return Aborted(self)

def _Node_queued_getter(self):
    return Queued(self)

def _Node_complete_getter(self):
    return Completed(self)

def _Node_unknown_getter(self):
    return Unknown(self)



# this .add_trigger() method additionally accepts
# ooflow.Expr and ooflow.Trigger objects.


def _Node_add_trigger(self, expr):
    if isinstance(expr, str):
        # set ecFlow trigger directly
        self._add_trigger(expr)
        # also store str as expression object in case someone
        # later wants to e.g. amend the trigger (e.g. self.trigger &= blah)
        expr = StrLiteral(expr)
        expr.expr_parent = self
        self.__trigger = expr
    elif isinstance(expr, ecflow.Expression):
        # set ecFlow trigger directly
        self._add_trigger(expr)
        # also store Expression object...
        expr = StrLiteral(str(expr))
        expr.expr_parent = self
        self.__trigger = expr
    elif isinstance(expr, NullTrigger):
        self.delete_trigger()
    elif isinstance(expr, Trigger) and isinstance(expr.expr, str):
        # set ecFlow trigger, bypass ooflow
        self._add_trigger(expr.expr)
        # also store Expression object...
        expr = StrLiteral(str(expr.expr))
        expr.expr_parent = self
        self.__trigger = expr
    elif isinstance(expr, Trigger):
        if isinstance(expr.expr, NullBool):
            self.delete_trigger()
        elif isinstance(expr.expr, Bool):
            # store ooflow trigger for delayed rendering
            expr.expr.expr_parent = self
            self.__trigger = expr.expr
    elif isinstance(expr, Bool):
        # store ooflow trigger for delayed rendering
        if isinstance(expr, NullBool):
            self.delete_trigger()
        else:
            expr.expr_parent = self
            self.__trigger = expr
    else:
        raise TypeError(
        "unexpected trigger type ({})".format(type(expr)))
    return self


# .trigger property (getter and setter)

def _Node_trigger_getter(self):
    self.__trigger.expr_parent = self
    return self.__trigger

def _Node_trigger_setter(self, expr):
    self.add_trigger(expr)


def _Node_delete_trigger(self):
    # delete ooflow trigger
    self.__trigger = NullBool()
    # delete ecFlow trigger
    self._delete_trigger()


# .defuser property (known as "a complete" in ecflow)
# It has been named ".defuser" rather than ".complete" because:
# - ".complete" is already used for as a node status property
# - "defuser" is a noun, like "trigger".


# Wrappper for the ecflow.Node add_complete,
# get_complete and delete_complete methods.
# - .add_complete() additionally accepts ooflow.Expr and ooflow.Trigger objects.


def _Node_add_complete(self, expr):
    if isinstance(expr, str):
        self._add_complete(expr)
        expr = StrLiteral(expr)
        expr.expr_parent = self
        self.__defuser = expr
    elif isinstance(expr, ecflow.Expression):
        self._add_complete(expr)
        expr = StrLiteral(str(expr))
        expr.expr_parent = self
        self.__defuser = expr
    elif isinstance(expr, NullDefuser):
        self.delete_complete()
    elif isinstance(expr, Defuser) and isinstance(expr.expr, str):
        self._add_complete(expr.expr)
        expr = StrLiteral(str(expr.expr))
        expr.expr_parent = self
        self.__defuser = expr
    elif isinstance(expr, Defuser):
        if isinstance(expr.expr, NullBool):
            self.delete_complete()
        elif isinstance(expr.expr, Bool):
            # store ooflow 'complete' expression for delayed rendering
            expr.expr.expr_parent = self
            self.__defuser = expr.expr
    elif isinstance(expr, Bool):
        # store ooflow 'complete' expression for delayed rendering
        if isinstance(expr, NullBool):
            self.delete_complete()
        else:
            expr.expr_parent = self
            self.__defuser = expr
    else:
        raise TypeError(
        "unexpected complete type ({})".format(type(expr)))
    return self

# .defuser property (getter and setter)

def _Node_defuser_getter(self):
    self.__defuser.expr_parent = self
    return self.__defuser

def _Node_defuser_setter(self, expr):
    self.add_complete(expr)

def _Node_delete_complete(self):
    # delete ooflow trigger
    self.__defuser = NullBool()
    # delete ecFlow trigger
    self._delete_complete()

#
# Wrapping ecflow.Node add_variable, find_variable and delete_variable methods.
# - .add_variable() also accepts NullVariable objects
# - .find_variable() returns the same object that was added (not a copy)
# - .variables property returns a list of variables added rather than copies
# - .delete_variable() removes variable object from .__variables dict
#
# ecflow.Node.get_variable() method:
# - returns Variable or Repeat given its name, searches up the tree if not found
#

def _Node_add_full_variable(add_variable):
    @wraps(add_variable)
    def wrapper(self, variable):
        if self.find_variable(variable.name()) is not None:
            self.delete_variable(variable.name())
        add_variable(self, variable)
        self.__variables[variable.name()] = variable
        variable.parent = self
    return wrapper

def _Node_add_variable(self, variable, *args):
    def add_single_variable(self, variable, *args):
        if isinstance(variable, str):
            variable = Variable(variable, *args)
        variable.add_to(self)
    if isinstance(variable, dict):
        for k, v in variable.items():
            add_single_variable(self, k, v)
    else:
        add_single_variable(self, variable, *args)
    return self


def _Node_find_variable_wrapper(find_variable):
    @wraps(find_variable)
    def wrapper(self, name):
        return self.__variables.get(name, None)
    return wrapper


def _Node_delete_variable_wrapper(delete_variable):
    @wraps(delete_variable)
    def wrapper(self, name):
        if name in self.__variables:
            self.__variables[name].parent = NullNode()
            del self.__variables[name]
        delete_variable(self, name)
    return wrapper


def _Node_variables_getter_wrapper(variables):
    def wrapper(self):
        return iter(self.__variables.values())
    return wrapper


def _Node_get_variable(self, name):
    """
    Returns Variable or Repeat object of a given name.
    If not found in this node, will be searched up the tree.
    If not found up the tree, returns NullVariable.
    """
    if name in self.__variables:
        return self.__variables[name]
    elif self.repeat.name() == name:
        return self.repeat
    else:
        return self.parent.get_variable(name)

#
# Implementing ecflow.Node.repeat property:
#
# - Repeat can be attached by assigning repeat object to this property.
# - One can get the repeat object from this property.
# - Repeat can be deleted by setting this property to None or NullOrdinal
#
# Decorating add_repeat, get_repeat and delete_repeat methods:
#
# - .add_repeat() additionally sets .parent property of the repeat object.
# - .get_repeat() returns the same object that has been attached rather than copy.
# - .delete_repeat() additionally sets .repeat property to NullOrdinal and
#              also the .parent propery of the repeat object to NullNode
#


def _Node_repeat_getter(self):
    return self.__repeat


def _Node_repeat_setter(self, repeat):
    repeat.add_to(self)


def _Node_add_full_repeat(add_repeat):
    @wraps(add_repeat)
    def wrapper(self, repeat):
        self.__repeat = repeat
        repeat.parent = self
        return add_repeat(self, repeat)
    return wrapper


def _Node_get_repeat_wrapper(get_repeat):
    @wraps(get_repeat)
    def wrapper(self):
        if not isinstance(self.repeat, NullOrdinal):
            return self.repeat
        else:
            return None
    return wrapper


def _Node_delete_repeat_wrapper(delete_repeat):
    @wraps(delete_repeat)
    def wrapper(self):
        self.__repeat.parent = NullNode()
        self.__repeat = NullRepeat()
        delete_repeat(self)
    return wrapper



# Decorating add_meter, get_meter and delete_meter methods:
# - .add_meter() additionally sets .parent property of the meter object.
# - .get_meter() returns the same object that has been attached rather than copy.
# - .delete_meter() additionally sets .parent propery of the meter object to Null


def _Node_add_full_meter(add_meter):
    @wraps(add_meter)
    def wrapper(self, meter):
        add_meter(self, meter)
        meter.parent = self
        self.__meters[meter.name()] = meter
    return wrapper

def _Node_add_meter(self, meter, *args):
    if isinstance(meter, str):
        meter = ecflow.Meter(meter, *args)
    meter.add_to(self)
    return self



def _Node_find_meter_wrapper(find_meter):
    @wraps(find_meter)
    def wrapper(self, name):
        return self.__meters.get(name, None)
    return wrapper


def _Node_delete_meter_wrapper(delete_meter):
    @wraps(delete_meter)
    def wrapper(self, name):
        if name in self.__meters:
            self.__meters[name].parent = NullNode()
            del self.__meters[name]
        delete_meter(self, name)
    return wrapper


def _Node_meters_getter_wrapper(meters):
    def wrapper(self):
        return iter(self.__meters.values())
    return wrapper


#
# Wrapping add_limit, find_limit, delete_limit, .limits:
# - add_limit() additionally sets limit.parent attribute
# - find_limit() returns original rather than copy
# - delete_limit() additionally sets limit.parent to Null
# - .limits property returns originals rather than copies
#

def _Node_add_full_limit(add_limit):
    @wraps(add_limit)
    def wrapper(self, limit):
        if self.find_limit(limit.name()):
            self.delete_limit(limit.name())
        self.__limits[limit.name()] = limit
        limit.parent = self
        add_limit(self, limit)
    return wrapper


def _Node_add_limit(self, limit, *args):
    if isinstance(limit, str):
        limit = Limit(limit, *args)
    limit.add_to(self)
    return self


def _Node_find_limit_wrapper(find_limit):
    @wraps(find_limit)
    def wrapper(self, name):
        return self.__limits.get(name, None)
    return wrapper


def _Node_delete_limit_wrapper(delete_limit):
    @wraps(delete_limit)
    def wrapper(self, name):
        if name in self.__limits:
            self.__limits[name].parent = NullNode()
            del self.__limits[name]
        delete_limit(self, name)
    return wrapper


def _Node_limits_getter_wrapper(limits_getter):
    def wrapper(self):
        return iter(self.__limits.values())
    return wrapper



#
# Wrapping add_inlimit, delete_inlimit, inlimits
#
def _Node_add_family_inlimit_wrapper(add_inlimit):
    @wraps(add_inlimit)
    def wrapper(self, inlimit, *args):
        if isinstance(inlimit, str):
            inlimit = ecflow.InLimit(inlimit, *args, limit_this_node_only=True, limit_submission=False)
        elif isinstance(inlimit, ecflow.Limit):
            inlimit = ecflow.InLimit(inlimit)
        if isinstance(inlimit.limit, NullLimit):
            # this InLimit object doesn't have reference to the Limit object
            # which means it is referring to the Limit by name or path.
            # No need to delay path resolving; this InLimit can be attached now.
            if inlimit.name():
                add_inlimit(self, inlimit)
        inlimit.parent = self
        self.__inlimits[inlimit.name()] = inlimit
        return self
    return wrapper

def _Node_add_submit_inlimit_wrapper(add_inlimit):
    @wraps(add_inlimit)
    def wrapper(self, inlimit, *args):
        if isinstance(inlimit, str):
            inlimit = ecflow.InLimit(inlimit, *args, limit_this_node_only=False, limit_submission=True)
        elif isinstance(inlimit, ecflow.Limit):
            inlimit = ecflow.InLimit(inlimit)
        if isinstance(inlimit.limit, NullLimit):
            # this InLimit object doesn't have reference to the Limit object
            # which means it is referring to the Limit by name or path.
            # No need to delay path resolving; this InLimit can be attached now.
            if inlimit.name():
                add_inlimit(self, inlimit)
        inlimit.parent = self
        self.__inlimits[inlimit.name()] = inlimit
        return self
    return wrapper


def _Node_add_inlimit_wrapper(add_inlimit):
    @wraps(add_inlimit)
    def wrapper(self, inlimit, *args):
        if isinstance(inlimit, str):
            inlimit = ecflow.InLimit(inlimit, *args)
        elif isinstance(inlimit, ecflow.Limit):
            inlimit = ecflow.InLimit(inlimit)
        if isinstance(inlimit.limit, NullLimit):
            # this InLimit object doesn't have reference to the Limit object
            # which means it is referring to the Limit by name or path.
            # No need to delay path resolving; this InLimit can be attached now.
            if inlimit.name():
                add_inlimit(self, inlimit)
        inlimit.parent = self
        self.__inlimits[inlimit.name()] = inlimit
        return self
    return wrapper


def _Node_delete_inlimit_wrapper(delete_inlimit):
    @wraps(delete_inlimit)
    def wrapper(self, name):
        if name in self.__inlimits:
            self.__inlimits[name].parent = NullNode()
            del self.__inlimits[name]
        delete_inlimit(self, name)
    return wrapper


def _Node_inlimits_getter_wrapper(inlimits):
    def wrapper(self):
        return iter(self.__inlimits.values())
    return wrapper


#
# Wrapping .add_event, .find_event, .delete_event and .events
#


def _Node_add_full_event(add_event):
    @wraps(add_event)
    def wrapper(self, event):
        add_event(self, event)
        event.parent = self
        name = event.name()
        number = event.number()
        if name != '':
            self.__events_by_name[str(name)] = event
        if number < 999999: #2^31-1 seems to stand for no number
            self.__events_by_number[str(number)] = event
        return self
    return wrapper

def _Node_add_event(self, event, *args):
    if isinstance(event, str) or isinstance(event, int):
        event = ecflow.Event(event, *args)
    event.add_to(self)
    return self


def _Node_find_event_wrapper(find_event):
    @wraps(find_event)
    def wrapper(self, name):
        s = str(name)
        e = self.__events_by_name.get(s, None)
        if e is None:
            e = self.__events_by_number.get(s, None)
        return e
    return wrapper


def _Node_delete_event_wrapper(delete_event):
    @wraps(delete_event)
    def wrapper(self, name):
        s = str(name)
        if s in self.__events_by_name:
            e = self.__events_by_name[s]
            e.parent = NullNode()
            del self.__events_by_name[s]
            try:
                del self.__events_by_number[str(e.number())]
            except KeyError:
                pass
        if s in self.__events_by_number:
            e = self.__events_by_number[s]
            e.parent = NullNode()
            del self.__events_by_number[s]
            n = e.name()
            try:
                del self.__events_by_name[n]
            except KeyError:
                pass
        delete_event(self, s)
    return wrapper


def _Node_events_getter_wrapper(events):
    def wrapper(self):
        d = self.__events_by_name.copy()
        d.update(self.__events_by_number)
        return iter(d.values())
    return wrapper


#
# Methods and properties for querying
# relationships with other nodes
#


def _Node_lineage_getter(self):
    node = self
    lin = []
    while not isinstance(node, NullNode):
        lin.append(node)
        node = node.parent
    return lin


def _Node_root_getter(self):
    return self.lineage[-1]


def _Node_descends_from(self, other):
    for node in self.lineage:
        if node is other:
            return True
    return False


def _Node_related_to(self, other):
    return self.descends_from(other.root)


def mrca(a, b):
    """ Most Recent Common Ancestor of two nodes """
    for node in a.lineage:
        if b.descends_from(node):
            return node
    return NullNode()


def _Node_mrca(self, other):
    return mrca(self, other)


def _relpath(path, start):
    basename = os.path.basename(path)
    if path == start:
        return basename
    start_parent_path = os.path.dirname(start)
    start_parent_name = os.path.basename(start_parent_path)
    relpath = os.path.relpath(path, start_parent_path)
    if relpath.startswith('../..'):
        return path # give up
    if relpath.endswith('..'):
        #return relpath + '/../' + basename
        return path # give up
    if relpath == '.':
        return '../' + basename
    return relpath


def _Node_path_getter(self):
    return self.get_abs_node_path()


def _Node_path_relto(self, other):
    if not other.related_to(self):
        return ''
    assert(isinstance(other, ecflow.Node))
    if isinstance(mrca(self, other), ecflow.Suite):
        # relative path would pass through root - return abs.path
        return self.path
    return _relpath(self.path, other.path)


def _Node_dependencies(self):
    """
    Returns list of nodes that trigger this node.
    Deprecated. Use .nodes property of an expression object.
    """
    return self.trigger.nodes


#
# Rendering triggers, defusers, limits
#


def _Node_finish(self):
    self._render_triggers()
    self._render_defusers()
    self._render_inlimits()


def _Node__render_triggers(self):
    if isinstance(self, ecflow.NodeContainer):
        for node in self.nodes:
            node._render_triggers()
    text = self.trigger.text
    if text:
        self._delete_trigger()
        self._add_trigger(text)


def _Node__render_defusers(self):
    if isinstance(self, ecflow.NodeContainer):
        for node in self.nodes:
            node._render_defusers()
    text = self.defuser.text
    if text:
        self._delete_complete()
        self._add_complete(text)


def _Node__render_inlimits(self):
    if isinstance(self, ecflow.NodeContainer):
        for node in self.nodes:
            node._render_inlimits()
    if self.__inlimits:
        for inlimit in self.__inlimits.values():
            name = inlimit.name()
            path = inlimit.limit.parent.path_relto(self)
            if path:
                self.add_inlimit(name, path)



setattr(ecflow.Node, 'add', _Node_add)

setattr(ecflow.Node, 'submitted', property(_Node_submitted_getter))
setattr(ecflow.Node, 'active', property(_Node_active_getter))
setattr(ecflow.Node, 'suspended', property(_Node_suspended_getter))
setattr(ecflow.Node, 'aborted', property(_Node_aborted_getter))
setattr(ecflow.Node, 'queued', property(_Node_queued_getter))
setattr(ecflow.Node, 'complete', property(_Node_complete_getter))
setattr(ecflow.Node, 'unknown', property(_Node_unknown_getter))

setattr(ecflow.Node, '_delete_trigger', ecflow.Node.delete_trigger) #save original delete_trigger method
setattr(ecflow.Node, 'delete_trigger', _Node_delete_trigger)
setattr(ecflow.Node, '_add_trigger', ecflow.Node.add_trigger) #save original add_trigger method
setattr(ecflow.Node, 'add_trigger', _Node_add_trigger)
setattr(ecflow.Node, 'trigger', property(_Node_trigger_getter, _Node_trigger_setter))

setattr(ecflow.Node, '_delete_complete', ecflow.Node.delete_complete) #save original delete_complete method
setattr(ecflow.Node, 'delete_complete', _Node_delete_complete)
setattr(ecflow.Node, '_add_complete', ecflow.Node.add_complete)
setattr(ecflow.Node, 'add_complete', _Node_add_complete)
setattr(ecflow.Node, 'defuser', property(_Node_defuser_getter, _Node_defuser_setter))

setattr(ecflow.Node, 'add_full_variable', _Node_add_full_variable(ecflow.Node.add_variable))
setattr(ecflow.Node, 'add_variable', _Node_add_variable)
setattr(ecflow.Node, 'find_variable', _Node_find_variable_wrapper(ecflow.Node.find_variable))
setattr(ecflow.Node, 'delete_variable', _Node_delete_variable_wrapper(ecflow.Node.delete_variable))
setattr(ecflow.Node, 'variables', property(_Node_variables_getter_wrapper(ecflow.Node.variables.__get__)))
setattr(ecflow.Node, 'get_variable', _Node_get_variable)

setattr(ecflow.Node, 'repeat', property(_Node_repeat_getter, _Node_repeat_setter))
setattr(ecflow.Node, 'add_full_repeat', _Node_add_full_repeat(ecflow.Node.add_repeat))
setattr(ecflow.Node, 'add_repeat', lambda self, repeat: repeat.add_to(self))
setattr(ecflow.Node, 'get_repeat', _Node_get_repeat_wrapper(ecflow.Node.get_repeat))
setattr(ecflow.Node, 'delete_repeat', _Node_delete_repeat_wrapper(ecflow.Node.delete_repeat))

setattr(ecflow.Node, 'add_full_meter', _Node_add_full_meter(ecflow.Node.add_meter))
setattr(ecflow.Node, 'add_meter', _Node_add_meter)
setattr(ecflow.Node, 'find_meter', _Node_find_meter_wrapper(ecflow.Node.find_meter))
setattr(ecflow.Node, 'delete_meter', _Node_delete_meter_wrapper(ecflow.Node.delete_meter))
setattr(ecflow.Node, 'meters', property(_Node_meters_getter_wrapper(ecflow.Node.meters.__get__)))

setattr(ecflow.Node, 'add_full_limit', _Node_add_full_limit(ecflow.Node.add_limit))
setattr(ecflow.Node, 'add_limit', _Node_add_limit)
setattr(ecflow.Node, 'find_limit', _Node_find_limit_wrapper(ecflow.Node.find_limit))
setattr(ecflow.Node, 'delete_limit', _Node_delete_limit_wrapper(ecflow.Node.delete_limit))
setattr(ecflow.Node, 'limits', property(_Node_limits_getter_wrapper(ecflow.Node.limits.__get__)))

setattr(ecflow.Node, 'add_submit_inlimit', _Node_add_submit_inlimit_wrapper(ecflow.Node.add_inlimit))
setattr(ecflow.Node, 'add_family_inlimit', _Node_add_family_inlimit_wrapper(ecflow.Node.add_inlimit))
setattr(ecflow.Node, 'add_inlimit', _Node_add_inlimit_wrapper(ecflow.Node.add_inlimit))
setattr(ecflow.Node, 'delete_inlimit', _Node_delete_inlimit_wrapper(ecflow.Node.delete_inlimit))
setattr(ecflow.Node, 'inlimits', property(_Node_inlimits_getter_wrapper(ecflow.Node.inlimits.__get__)))

setattr(ecflow.Node, 'add_full_event', _Node_add_full_event(ecflow.Node.add_event))
setattr(ecflow.Node, 'add_event', _Node_add_event)
setattr(ecflow.Node, 'find_event', _Node_find_event_wrapper(ecflow.Node.find_event))
setattr(ecflow.Node, 'delete_event', _Node_delete_event_wrapper(ecflow.Node.delete_event))
setattr(ecflow.Node, 'events', property(_Node_events_getter_wrapper(ecflow.Node.events.__get__)))

setattr(ecflow.Node, 'path', property(_Node_path_getter))
setattr(ecflow.Node, 'lineage', property(_Node_lineage_getter))
setattr(ecflow.Node, 'root', property(_Node_root_getter))
setattr(ecflow.Node, 'descends_from', _Node_descends_from)
setattr(ecflow.Node, 'related_to', _Node_related_to)
setattr(ecflow.Node, 'mrca', _Node_mrca)
setattr(ecflow.Node, 'path_relto', _Node_path_relto)

setattr(ecflow.Node, 'dependencies', property(_Node_dependencies))

# translates ooflow triggers into strings
setattr(ecflow.Node, 'finish', _Node_finish)

# private methods
setattr(ecflow.Node, '_render_triggers', _Node__render_triggers)
setattr(ecflow.Node, '_render_defusers', _Node__render_defusers)
setattr(ecflow.Node, '_render_inlimits', _Node__render_inlimits)

# deprecated - use .get_variable() instead. leaving it for backward-compatibility
setattr(ecflow.Node, 'var', _Node_get_variable)


# ---------------------------------------------------------
# monkey-patching ecflow.NodeContainer class
# .add_task(t) sets .parent attribute, accepts NullNode objects.
# .add_family(f) sets .parent attribute, accepts NullNode objects.
# .children returns a list of children.
# ---------------------------------------------------------


def _NodeContainer_add_family(self, family):
    ret = None
    if isinstance(family, str):
        family = ecflow.Family(family)
        ret = family
    family.add_to(self)
    return ret

def _NodeContainer_add_task(self, task):
    ret = None
    if isinstance(task, str):
        task = ecflow.Task(task)
        ret = task
    task.add_to(self)
    return ret

setattr(ecflow.NodeContainer, 'ecflow_add_task', ecflow.NodeContainer.add_task)
setattr(ecflow.NodeContainer, 'add_task', _NodeContainer_add_task)
setattr(ecflow.NodeContainer, 'ecflow_add_family', ecflow.NodeContainer.add_family)
setattr(ecflow.NodeContainer, 'add_family', _NodeContainer_add_family)
setattr(ecflow.NodeContainer, 'children', property(lambda self: [x for x in self.nodes]))



# -------------------------------------------------------------------------
# translate ooflow triggers into strings when generating text of the suite
# -------------------------------------------------------------------------

def _Defs_str_wrapper(__str__):
    def wrapper(self):
        for s in list(self.suites):
            s.finish()
        return __str__(self)
    return wrapper

setattr(ecflow.Defs, '__str__', _Defs_str_wrapper(ecflow.Defs.__str__))


def _Node_str_wrapper(__str__):
    def wrapper(self):
        self.finish()
        return __str__(self)
    return wrapper

setattr(ecflow.Suite, '__str__', _Node_str_wrapper(ecflow.Suite.__str__))
setattr(ecflow.Family, '__str__', _Node_str_wrapper(ecflow.Family.__str__))
setattr(ecflow.Task, '__str__', _Node_str_wrapper(ecflow.Task.__str__))



# ------------------------------------------------------
# convenience function, generates 'and'
# trigger expression for a list of nodes.
#-------------------------------------------------------


def all_complete(nodes):
    """
    Given a list of nodes, return an expression which
    evaluates to true when all nodes are complete.
    """
    return reduce(lambda a, b: a & b, [
        StrLiteral('{} == complete'.format(node)) if isinstance(node, str) \
                else node.complete for node in nodes], NullBool())



from ecflow import *