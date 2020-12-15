# Copyright (c) 2016, Darius Bacon
# Byterun Copyright (c) 2013, Ned Batchelder

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import ast, collections, types, sys
import dis2 as dis
import contextlib
from functools import reduce
from itertools import chain
from check_subset import check_conformity

def assemble(assembly):
    return bytes(iter(assembly.encode(0, dict(assembly.resolve(0)))))

def plumb_depths(assembly):
    depths = [0]
    assembly.plumb(depths)
    return max(depths)

def make_lnotab(assembly):
    firstlineno, lnotab = None, []
    byte, line = 0, None
    for next_byte, next_line in assembly.line_nos(0):
        if firstlineno is None:
            firstlineno = line = next_line
        elif line < next_line:
            # Py3.6 changed to use signed bytes here, not unsigned.
            # This is a hack to keep the old logic, without taking advantage of
            # the new possibility of negative values.
            while byte+127 < next_byte:
                lnotab.extend([127, 0])
                byte = byte+127
            while line+127 < next_line:
                lnotab.extend([next_byte-byte, 127])
                byte, line = next_byte, line+127
            if (byte, line) != (next_byte, next_line):
                lnotab.extend([next_byte-byte, next_line-line])
                byte, line = next_byte, next_line
    return firstlineno or 1, bytes(lnotab)

def concat(assemblies):
    return sum(assemblies, no_op)

class Assembly:
    def __add__(self, other):
        assert isinstance(other, Assembly), other
        return Chain(self, other)
    length = 0
    def resolve(self, start):
        return ()
    def encode(self, start, addresses):
        return b''
    def line_nos(self, start):
        return ()
    def plumb(self, depths):
        pass

no_op = Assembly()

class Label(Assembly):
    def resolve(self, start):
        return ((self, start),)

class SetLineNo(Assembly):
    def __init__(self, line):
        self.line = line
    def line_nos(self, start):
        return ((start, self.line),)

class Instruction(Assembly):
    length = 2

    def __init__(self, opcode, arg, arg2):
        self.opcode = opcode
        self.arg    = arg
        self.arg2   = arg2
    def encode(self, start, addresses):
        arg, arg2 = self.arg, self.arg2
        if dis.opcodes[self.opcode].is_jump():
            arg2 = addresses[arg2] - (start+4)
        if arg2 is None:
            arg2 = 0
        else:
            assert arg2 >= -32768 and arg2 < 32768
            arg2 = arg2 & 0xFFFF
        argA = self.arg or 0
        argB = arg2 & 0xFF
        argC = (arg2 >> 8)
        return bytes([self.opcode, argA, argB, argC])
    def plumb(self, depths):
        arg = 0 if isinstance(self.arg, Label) else self.arg
        depths.append(depths[-1] + dis.stack_effect(self.opcode, arg))

class Chain(Assembly):
    def __init__(self, assembly1, assembly2):
        self.part1 = assembly1
        self.part2 = assembly2
        self.length = assembly1.length + assembly2.length
    def resolve(self, start):
        return chain(self.part1.resolve(start),
                     self.part2.resolve(start + self.part1.length))
    def encode(self, start, addresses):
        return chain(self.part1.encode(start, addresses),
                     self.part2.encode(start + self.part1.length, addresses))
    def line_nos(self, start):
        return chain(self.part1.line_nos(start),
                     self.part2.line_nos(start + self.part1.length))
    def plumb(self, depths):
        self.part1.plumb(depths)
        self.part2.plumb(depths)

class OffsetStack(Assembly):
    def plumb(self, depths):
        depths.append(depths[-1] - 1)

class Placeholder:
    def __init__(self, visitor):
        self.reg = None
        self.visitor = visitor
    def __index__(self):
        assert self.reg is not None, 'unassigned placeholder'
        return self.reg
    def __call__(self, t):
        assert self.reg is None, 'unassigned placeholder'
        self.reg, instrs = self.visitor.to_register(t)
        return instrs

def denotation(defn):
    opcode = defn.opcode
    opA = defn.opA
    opD = defn.opD
    if opA is None and opD is None:
        return Instruction(opcode, None, None)
    elif opD is None:
        return lambda arg: Instruction(opcode, arg, None)
    elif opA is None:
        return lambda arg2: Instruction(opcode, None, arg2)
    else:
        return lambda arg, arg2: Instruction(opcode, arg, arg2)

op = type('op', (), dict([(bytecode.name, denotation(bytecode))
                          for bytecode in dis.bytecodes]))

def register_scope(visitor):
    def visit(self, t):
        top = self.next_register
        ret = visitor(self, t)
        self.next_register = top
        return ret
    return visit

class CodeGen(ast.NodeVisitor):
    def __init__(self, filename, scope):
        self.filename  = filename
        self.scope     = scope
        self.constants = constants()
        self.names     = self.constants
        self.varnames  = make_table()
        self.nlocals = 0
        self.next_register = 0
        self.max_registers = 0

    def compile_module(self, t, name):
        assembly = self(t.body) + self.load_const(None) + op.RETURN_VALUE
        return self.make_code(assembly, name, 0, False, False)

    def make_code(self, assembly, name, argcount, has_varargs, has_varkws):
        posonlyargcount = 0
        kwonlyargcount = 0
        nlocals = len(self.varnames)
        # stacksize = plumb_depths(assembly)
        framesize = 0
        flags = (  (0x02 if nlocals                  else 0)
                 | (0x04 if has_varargs              else 0)
                 | (0x08 if has_varkws               else 0)
                 | (0x10 if self.scope.freevars      else 0)
                 | (0x40 if not self.scope.derefvars else 0))
        firstlineno, lnotab = make_lnotab(assembly)
        code = assemble(assembly)
        print('here we go!')
        return types.Code2Type(code,
                               self.constants.collect(),
                               argcount=argcount,
                               posonlyargcount=posonlyargcount,
                               kwonlyargcount=kwonlyargcount,
                               nlocals=nlocals,
                               framesize=framesize,
                               varnames=collect(self.varnames),
                               filename=self.filename,
                               name=name,
                               firstlineno=firstlineno,
                               linetable=lnotab,
                               freevars=self.scope.freevars,
                               cellvars=self.scope.cellvars)
        # return types.Code2Type(argcount, posonlyargcount, kwonlyargcount,
        #                       nlocals, stacksize, flags, code,
        #                       self.collect_constants(),
        #                       collect(self.names), collect(self.varnames),
        #                       self.filename, name, firstlineno, lnotab,
        #                       self.scope.freevars, self.scope.cellvars)

    def load_const(self, constant):
        return op.LOAD_CONST(self.constants[constant])

    def new_register(self):
        reg = self.next_register
        self.next_register += 1
        if self.next_register > self.max_registers:
            self.max_registers = self.next_register
        return reg

    def register(self):
        return Placeholder(self)

    def to_register(self, t):
        if isinstance(t, ast.Name):
            access = self.scope.access(t.id)
            if access == 'fast':
                return self.varnames[t.id], no_op
        reg = self.new_register()
        return reg, self(t) + op.STORE_FAST(reg)

    def visit_NameConstant(self, t): return self.load_const(t.value)
    def visit_Num(self, t):          return self.load_const(t.n)
    def visit_Str(self, t):          return self.load_const(t.s)
    visit_Bytes = visit_Str

    def visit_Name(self, t):
        if   isinstance(t.ctx, ast.Load):  return self.load(t.id)
        elif isinstance(t.ctx, ast.Store): return self.store(t.id)
        else: assert False

    def load(self, name):
        access = self.scope.access(name)
        if   access == 'fast':  return op.LOAD_FAST(self.varnames[name])
        elif access == 'deref': return op.LOAD_DEREF(self.cell_index(name))
        elif access == 'name':  return op.LOAD_NAME(self.names[name])
        else: assert False

    def store(self, name):
        access = self.scope.access(name)
        if   access == 'fast':  return op.STORE_FAST(self.varnames[name])
        elif access == 'deref': return op.STORE_DEREF(self.cell_index(name))
        elif access == 'name':  return op.STORE_NAME(self.names[name])
        else: assert False

    def cell_index(self, name):
        return self.scope.derefvars.index(name)

    def visit_Call(self, t):
        assert len(t.args) < 256 and len(t.keywords) < 256
        # FIXME base register
        base = 0
        opcode = (
                  # op.CALL_FUNCTION_VAR_KW if t.starargs and t.kwargs else
                  # op.CALL_FUNCTION_VAR    if t.starargs else
                  # op.CALL_FUNCTION_KW     if t.kwargs else
                  op.CALL_FUNCTION)
        return (self(t.func)
                + self(t.args)
                + self(t.keywords)
                # + (self(t.starargs) if t.starargs else no_op)
                # + (self(t.kwargs)   if t.kwargs   else no_op)
                + opcode(base, len(t.args)))

    def visit_keyword(self, t):
        return self.load_const(t.arg) + self(t.value)

    def __call__(self, t):
        if isinstance(t, list): return concat(map(self, t))
        assembly = self.visit(t)
        return SetLineNo(t.lineno) + assembly if hasattr(t, 'lineno') else assembly

    def generic_visit(self, t):
        assert False, t

    def visit_Expr(self, t):
        # TODO: skip constants as optimization
        return self(t.value) + op.CLEAR_ACC

    def visit_AugAssign(self, t):
        pass
        return no_op

    def visit_Assign(self, t):
        # FIXME: multiple assignment
        return self(t.value) + self(t.targets[0])

    def visit_If(self, t):
        orelse, after = Label(), Label()
        return (           self(t.test) + op.POP_JUMP_IF_FALSE(orelse)
                         + self(t.body) + op.JUMP_FORWARD(after)
                + orelse + self(t.orelse)
                + after)

    def visit_IfExp(self, t):
        orelse, after = Label(), Label()
        return (           self(t.test) + op.POP_JUMP_IF_FALSE(orelse)
                         + self(t.body) + op.JUMP_FORWARD(after)
                + OffsetStack()
                + orelse + self(t.orelse)
                + after)

    def visit_Dict(self, t):
        return (op.BUILD_MAP(min(0xFFFF, len(t.keys)))
                + concat([self(v) + self(k) + op.STORE_MAP
                          for k, v in zip(t.keys, t.values)]))

    @register_scope
    def visit_Subscript(self, t):
        reg = self.register()
        return (reg(t.value) +
                self(t.slice.value) +
                self.subscr_ops[type(t.ctx)](reg))
    subscr_ops = {ast.Load: op.BINARY_SUBSCR, ast.Store: op.STORE_SUBSCR}

    def visit_Attribute(self, t):
        sub_op = self.attr_ops[type(t.ctx)]
        return self(t.value) + sub_op(self.names[t.attr])
    attr_ops = {ast.Load: op.LOAD_ATTR, ast.Store: op.STORE_ATTR}

    def visit_List(self, t):  return self.visit_sequence(t, op.BUILD_LIST)
    def visit_Tuple(self, t): return self.visit_sequence(t, op.BUILD_TUPLE)

    def visit_sequence(self, t, build_op):
        if   isinstance(t.ctx, ast.Load):
            return self(t.elts) + build_op(len(t.elts))
        elif isinstance(t.ctx, ast.Store):
            # FIXME: register
            base = 0
            return op.UNPACK_SEQUENCE(base, len(t.elts)) + self(t.elts)
        else:
            assert False

    def visit_UnaryOp(self, t):
        return self(t.operand) + self.ops1[type(t.op)]
    ops1 = {ast.UAdd: op.UNARY_POSITIVE,  ast.Invert: op.UNARY_INVERT,
            ast.USub: op.UNARY_NEGATIVE,  ast.Not:    op.UNARY_NOT}

    @register_scope
    def visit_BinOp(self, t):
        reg = self.register()
        return reg(t.left) + self(t.right) + self.ops2[type(t.op)](reg)

    ops2 = {ast.Pow:    op.BINARY_POWER,  ast.Add:  op.BINARY_ADD,
            ast.LShift: op.BINARY_LSHIFT, ast.Sub:  op.BINARY_SUBTRACT,
            ast.RShift: op.BINARY_RSHIFT, ast.Mult: op.BINARY_MULTIPLY,
            ast.BitOr:  op.BINARY_OR,     ast.Mod:  op.BINARY_MODULO,
            ast.BitAnd: op.BINARY_AND,    ast.Div:  op.BINARY_TRUE_DIVIDE,
            ast.BitXor: op.BINARY_XOR,    ast.FloorDiv: op.BINARY_FLOOR_DIVIDE}

    def visit_Compare(self, t):
        [operator], [right] = t.ops, t.comparators
        cmp_index = dis.cmp_op.index(self.ops_cmp[type(operator)])
        return self(t.left) + self(right) + op.COMPARE_OP(cmp_index)
    ops_cmp = {ast.Eq: '==', ast.NotEq: '!=', ast.Is: 'is', ast.IsNot: 'is not',
               ast.Lt: '<',  ast.LtE:   '<=', ast.In: 'in', ast.NotIn: 'not in',
               ast.Gt: '>',  ast.GtE:   '>='}

    def visit_BoolOp(self, t):
        op_jump = self.ops_bool[type(t.op)]
        def compose(left, right):
            after = Label()
            return left + op_jump(after) + OffsetStack() + right + after
        return reduce(compose, map(self, t.values))
    ops_bool = {ast.And: op.JUMP_IF_FALSE_OR_POP,
                ast.Or:  op.JUMP_IF_TRUE_OR_POP}

    def visit_Pass(self, t):
        return no_op

    def visit_Raise(self, t):
        return self(t.exc) + op.RAISE_VARARGS(1)

    def visit_Import(self, t):
        return concat([self.import_name(0, None, alias.name)
                       + self.store(alias.asname or alias.name.split('.')[0])
                       for alias in t.names])

    def visit_ImportFrom(self, t):
        fromlist = tuple([alias.name for alias in t.names])
        return (self.import_name(t.level, fromlist, t.module)
                + concat([op.IMPORT_FROM(self.names[alias.name])
                          + self.store(alias.asname or alias.name)
                         for alias in t.names])
                + op.CLEAR_ACC)

    def import_name(self, level, fromlist, name):
        return (self.load_const(level)
                + self.load_const(fromlist)
                + op.IMPORT_NAME(self.names[name]))

    def visit_While(self, t):
        loop, end = Label(), Label()
        return (  loop + self(t.test) + op.POP_JUMP_IF_FALSE(end)
                       + self(t.body) + op.JUMP_ABSOLUTE(loop)
                + end)

    def visit_For(self, t):
        loop, end = Label(), Label()
        return (         self(t.iter) + op.GET_ITER
                + loop + op.FOR_ITER(end) + self(t.target)
                       + self(t.body) + op.JUMP_ABSOLUTE(loop)
                + end  + OffsetStack())

    def visit_Return(self, t):
        return ((self(t.value) if t.value else self.load_const(None))
                + op.RETURN_VALUE)

    def visit_Function(self, t):
        code = self.sprout(t).compile_function(t)
        return self.make_closure(code, t.name)

    def sprout(self, t):
        return CodeGen(self.filename, self.scope.children[t])

    def make_closure(self, code, name):
        if code.co_freevars:
            return (concat([op.LOAD_CLOSURE(self.cell_index(freevar))
                            for freevar in code.co_freevars])
                    + op.BUILD_TUPLE(len(code.co_freevars))
                    + self.load_const(code) + self.load_const(name)
                    + op.MAKE_FUNCTION(0x08))
        else:
            return (self.load_const(code) + self.load_const(name)
                    + op.MAKE_FUNCTION(0))

    def compile_function(self, t):
        self.load_const(ast.get_docstring(t))
        for arg in t.args.args:
            self.varnames[arg.arg]
        if t.args.vararg: self.varnames[t.args.vararg.arg]
        if t.args.kwarg:  self.varnames[t.args.kwarg.arg]
        assembly = self(t.body) + self.load_const(None) + op.RETURN_VALUE
        return self.make_code(assembly, t.name,
                              len(t.args.args), t.args.vararg, t.args.kwarg)

    def visit_ClassDef(self, t):
        code = self.sprout(t).compile_class(t)
        return (op.LOAD_BUILD_CLASS + self.make_closure(code, t.name)
                                    + self.load_const(t.name)
                                    + self(t.bases)
                + op.CALL_FUNCTION(2 + len(t.bases))
                + self.store(t.name))

    def compile_class(self, t):
        docstring = ast.get_docstring(t)
        assembly = (  self.load('__name__')      + self.store('__module__')
                    + self.load_const(t.name)    + self.store('__qualname__')
                    + (no_op if docstring is None else
                       self.load_const(docstring) + self.store('__doc__'))
                    + self(t.body)
                    + self.load_const(None) + op.RETURN_VALUE)
        return self.make_code(assembly, t.name, 0, False, False)

class constants(collections.defaultdict):
    def __init__(self):
        super().__init__(lambda: len(self))
    def __getitem__(self, key):
        return super().__getitem__((key, type(key)))
    def collect(self):
        return tuple(key for key,_ in self.keys())

def make_table():
    table = collections.defaultdict(lambda: len(table))
    return table

def collect(table):
    return tuple(sorted(table, key=table.get))

def load_file(filename, module_name):
    f = open(filename)
    source = f.read()
    f.close()
    return module_from_ast(module_name, filename, ast.parse(source))

def module_from_ast(module_name, filename, t):
    code = code_for_module(module_name, filename, t)
    print('made code')
    module = types.ModuleType(module_name, ast.get_docstring(t))
    print(dis.dis(code))
    # exec(code, module.__dict__)
    return module

def code_for_module(module_name, filename, t):
    t = desugar(t)
    check_conformity(t)
    return CodeGen(filename, top_scope(t)).compile_module(t, module_name)

def desugar(t):
    return ast.fix_missing_locations(Desugarer().visit(t))

def rewriter(rewrite):
    def visit(self, t):
        return ast.copy_location(rewrite(self, self.generic_visit(t)),
                                 t)
    return visit

def Call(fn, args):
    return ast.Call(fn, args, [])

class Desugarer(ast.NodeTransformer):

    @rewriter
    def visit_Assert(self, t):
        return ast.If(t.test,
                      [],
                      [ast.Raise(Call(ast.Name('AssertionError', load),
                                      [] if t.msg is None else [t.msg]),
                                 None)])

    @rewriter
    def visit_Lambda(self, t):
        return Function('<lambda>', t.args, [ast.Return(t.body)])

    @rewriter
    def visit_FunctionDef(self, t):
        fn = Function(t.name, t.args, t.body)
        for d in reversed(t.decorator_list):
            fn = Call(d, [fn])
        return ast.Assign([ast.Name(t.name, store)], fn)

    @rewriter
    def visit_ListComp(self, t):
        result_append = ast.Attribute(ast.Name('.0', load), 'append', load)
        body = ast.Expr(Call(result_append, [t.elt]))
        for loop in reversed(t.generators):
            for test in reversed(loop.ifs):
                body = ast.If(test, [body], [])
            body = ast.For(loop.target, loop.iter, [body], [])
        fn = [body,
              ast.Return(ast.Name('.0', load))]
        args = ast.arguments(None, [ast.arg('.0', None)], None, [], None, [], [])

        return Call(Function('<listcomp>', args, fn),
                    [ast.List([], load)])

class Function(ast.FunctionDef):
    _fields = ('name', 'args', 'body')

load, store = ast.Load(), ast.Store()

def top_scope(t):
    top = Scope(t, ())
    top.visit(t)
    top.analyze(set())
    return top

class Scope(ast.NodeVisitor):
    def __init__(self, t, defs):
        self.t = t
        self.children = {}       # Enclosed sub-scopes
        self.defs = set(defs)    # Variables defined
        self.uses = set()        # Variables referenced

    def visit_ClassDef(self, t):
        self.defs.add(t.name)
        for expr in t.bases: self.visit(expr)
        subscope = Scope(t, ())
        self.children[t] = subscope
        for stmt in t.body: subscope.visit(stmt)

    def visit_Function(self, t):
        all_args = list(t.args.args) + [t.args.vararg, t.args.kwarg]
        subscope = Scope(t, [arg.arg for arg in all_args if arg])
        self.children[t] = subscope
        for stmt in t.body: subscope.visit(stmt)

    def visit_Import(self, t):
        for alias in t.names:
            self.defs.add(alias.asname or alias.name.split('.')[0])

    def visit_ImportFrom(self, t):
        for alias in t.names:
            self.defs.add(alias.asname or alias.name)

    def visit_Name(self, t):
        if   isinstance(t.ctx, ast.Load):  self.uses.add(t.id)
        elif isinstance(t.ctx, ast.Store): self.defs.add(t.id)
        else: assert False

    def analyze(self, parent_defs):
        self.local_defs = self.defs if isinstance(self.t, Function) else set()
        for child in self.children.values():
            child.analyze(parent_defs | self.local_defs)
        child_uses = set([var for child in self.children.values()
                              for var in child.freevars])
        uses = self.uses | child_uses
        self.cellvars = tuple(child_uses & self.local_defs)
        self.freevars = tuple(uses & (parent_defs - self.local_defs))
        self.derefvars = self.cellvars + self.freevars

    def access(self, name):
        return ('deref' if name in self.derefvars else
                'fast'  if name in self.local_defs else
                'name')

if __name__ == '__main__':
    sys.argv.pop(0)
    load_file(sys.argv[0], '__main__')