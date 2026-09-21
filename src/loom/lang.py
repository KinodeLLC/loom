"""
Loom: a durable workflow language.

A workflow is a sequence of steps, each of which may fail and each of which may
declare how to undo itself. Loom's job is to make the two hard parts of that
boring:

  Compensation is written next to the step it undoes, not assembled by hand in
  an error path far away. When a step fails, every completed step's
  compensation runs in reverse order, and the compensations are emitted
  explicitly rather than driven by a runtime stack -- so the unwinding is
  visible in the generated code and in the journal, and can be inspected before
  it ever runs.

  Durability is not a separate mechanism. Each step checkpoints to the effect
  journal before it runs, so resuming a crashed workflow is the Ledger's
  ordinary replay: completed steps return their recorded results without being
  performed again, and execution continues from the first step that never
  finished. There is no separate workflow state store to fall out of sync.

Loom lowers to Canon. A workflow becomes a function returning `Result`, its
steps become nested matches, and its retries are unrolled rather than looped --
which keeps every workflow total, and keeps the number of times an external
system can be called a fact visible in the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from canon import ast as A
from canon.diagnostics import Bag, Repair, Span
from canon.lexer import Lexer, T
from canon.parser import Parser


LANGUAGE_VERSION = "0.1"

MAX_RETRIES = 8

# Injected into any module that declares a workflow. Checkpoints are what make
# resumption work, and they are ordinary journaled effects.
RUNTIME_EFFECT = """
--- Workflow control. Performed by generated code, not written by hand.
effect workflow {
  --- Record that a step is about to run. On replay this returns the recorded
  --- value, which is how a resumed workflow skips completed steps.
  checkpoint(workflow_name: Text, step: Text, attempt: Int) -> Unit
  --- Record that a step's compensation ran.
  compensated(workflow_name: Text, step: Text) -> Unit
  --- Wait for an external signal, or time out.
  await_signal(name: Text, deadline_millis: Int) -> Result<Text, Text>
  --- Durable sleep.
  sleep(millis: Int) -> Unit
}
"""


# --------------------------------------------------------------------------
# Surface declarations
# --------------------------------------------------------------------------

@dataclass
class StepDecl:
    name: str = ""
    binding: str = ""
    expr: Optional[A.Expr] = None
    compensate: Optional[A.Expr] = None
    retries: int = 0
    optional: bool = False       # on_failure continue
    span: Span = field(default_factory=Span.unknown)


@dataclass
class WaitDecl:
    name: str = ""
    binding: str = ""
    kind: str = "signal"          # signal | sleep
    deadline: int = 0
    millis: int = 0
    span: Span = field(default_factory=Span.unknown)


@dataclass
class WorkflowDecl:
    name: str = ""
    params: list = field(default_factory=list)
    result: Optional[A.TypeExpr] = None
    intent: str = ""
    doc: str = ""
    uses: list = field(default_factory=list)
    cost: Optional[A.Cost] = None
    deadline: int = 0
    idempotent_by: str = ""
    items: list = field(default_factory=list)   # StepDecl | WaitDecl | A.Stmt
    final: Optional[A.Expr] = None
    span: Span = field(default_factory=Span.unknown)

    def steps(self) -> list:
        return [i for i in self.items if isinstance(i, StepDecl)]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------

class LoomParser(Parser):
    def __init__(self, tokens, source="", filename="<memory>", bag=None):
        super().__init__(tokens, source, filename, bag, language="loom")
        self.workflows: list = []

    def parse_decl(self):
        doc = self.skip_docs()
        if self.at_ctx("workflow") and self.at(1).kind == T.NAME:
            w = self.parse_workflow(doc)
            self.workflows.append(w)
            return None
        for fn, kw in ((self.parse_fn, "fn"), (self.parse_record, "record"),
                       (self.parse_enum, "enum"), (self.parse_alias, "alias"),
                       (self.parse_effect, "effect"), (self.parse_const, "const"),
                       (self.parse_test, "test")):
            if self.cur.is_kw(kw):
                return fn(doc)
        return None

    # ------------------------------------------------------------------

    def parse_workflow(self, doc: str = "") -> WorkflowDecl:
        start = self.next()           # workflow
        w = WorkflowDecl(doc=doc)
        w.name = self.expect_name("a workflow name")
        w.params = self.parse_params()
        self.expect_op("->", "before the workflow result type")
        w.result = self.parse_type()

        if not _is_result(w.result):
            self.err(
                "CANON-E0301",
                f"workflow {w.name!r} must return Result<_, _>",
                start,
                facts={"workflow": w.name, "found": _tyname(w.result)},
                repairs=[Repair("manual",
                                "declare the success and failure types, "
                                "e.g. -> Result<Receipt, FulfilError>")],
                notes=["A workflow's steps can fail, so its result has to be "
                       "able to carry the failure. Compensation is driven off "
                       "the error case."])

        while True:
            # Doc comments may sit between header clauses.
            self.skip_docs()
            if self.cur.is_kw("intent"):
                self.next()
                w.intent = self.parse_text_literal("an intent description")
            elif self.cur.is_kw("uses"):
                self.next()
                w.uses.extend(self.parse_effect_refs())
            elif self.cur.is_kw("cost"):
                self.next()
                w.cost = self.parse_cost(w.cost)
            elif self.at_ctx("deadline"):
                self.next()
                w.deadline = self.expect_int("a deadline in milliseconds")
            elif self.at_ctx("idempotent"):
                self.next()
                self.eat_ctx("by")
                w.idempotent_by = self.expect_name("the idempotency key field")
            else:
                break

        self.expect_punct("{", "to open the workflow body")
        while not self.cur.is_punct("}") and self.cur.kind != T.EOF:
            self.skip_docs()
            if self.at_ctx("step"):
                w.items.append(self.parse_step())
            elif self.at_ctx("await"):
                w.items.append(self.parse_await())
            elif self.at_ctx("timer"):
                w.items.append(self.parse_timer())
            elif self.cur.is_kw("let"):
                s = self.next()
                name = self.expect_name("a binding name")
                ty = self.parse_type() if self.eat_punct(":") else None
                self.expect_op("=", "before the bound value")
                stmt = A.SLet(name=name, ty=ty, value=self.parse_expr())
                stmt.span = self.span_from(s)
                w.items.append(stmt)
            elif self.cur.is_kw("do"):
                s = self.next()
                stmt = A.SExpr(value=self.parse_expr())
                stmt.span = self.span_from(s)
                w.items.append(stmt)
            elif self.cur.is_kw("assert"):
                s = self.next()
                cond = self.parse_expr()
                msg = self.parse_text_literal("an assertion message") \
                    if self.eat_punct(",") else ""
                stmt = A.SAssert(cond=cond, message=msg)
                stmt.span = self.span_from(s)
                w.items.append(stmt)
            else:
                w.final = self.parse_expr()
                break
        self.expect_punct("}", "to close the workflow body")

        if w.final is None:
            self.err("CANON-E0103",
                     f"workflow {w.name!r} does not end with a result "
                     f"expression", start,
                     facts={"workflow": w.name},
                     repairs=[Repair("manual",
                                     "end the workflow with its success value, "
                                     "e.g. Ok(Receipt { ... })")])
            w.final = A.CtorCall(name="Ok", args=[A.Lit(value=None,
                                                        lit_kind="unit")])

        names = [s.name for s in w.steps()]
        for n in set(names):
            if names.count(n) > 1:
                self.err("CANON-E0203",
                         f"workflow {w.name!r} has more than one step named "
                         f"{n!r}", start,
                         facts={"workflow": w.name, "step": n},
                         notes=["Step names appear in checkpoints and in the "
                                "journal, so they have to identify a step "
                                "uniquely for resumption to work."])

        w.span = self.span_from(start)
        return w

    def expect_int(self, what: str) -> int:
        if self.cur.kind == T.INT:
            return int(self.next().payload)
        self.err("CANON-E0101", f"expected {what}")
        return 0

    def parse_step(self) -> StepDecl:
        start = self.next()           # step
        s = StepDecl()
        # Three forms:
        #   step name = expr   names the step and binds its value
        #   step name: expr    names the step without binding anything
        #   step expr          derives a name from the expression
        # The middle form exists because a step's name matters -- it appears in
        # every checkpoint and drives resumption -- while its value often does
        # not, and forcing a binding nothing reads is just noise.
        if self.cur.kind == T.NAME and self.at(1).is_op("="):
            s.binding = self.next().value
            s.name = s.binding
            self.next()
        elif self.cur.kind == T.NAME and self.at(1).is_punct(":"):
            s.name = self.next().value
            self.next()
        s.expr = self.parse_expr()
        if not s.name:
            s.name = _step_name_of(s.expr)

        while True:
            if self.at_ctx("compensate"):
                self.next()
                s.compensate = self.parse_expr()
            elif self.at_ctx("retry"):
                self.next()
                n = self.expect_int("a retry count")
                if n < 0 or n > MAX_RETRIES:
                    self.err(
                        "CANON-E0301",
                        f"retry count must be between 0 and {MAX_RETRIES}",
                        start, facts={"requested": n, "limit": MAX_RETRIES},
                        notes=["Retries are unrolled rather than looped, so "
                               "the number of times an external system can be "
                               "called stays a fact visible in the source."])
                    n = max(0, min(MAX_RETRIES, n))
                s.retries = n
            elif self.at_ctx("on_failure"):
                self.next()
                if self.eat_ctx("continue"):
                    s.optional = True
                elif self.eat_ctx("abort"):
                    s.optional = False
                else:
                    self.err("CANON-E0101",
                             "expected `continue` or `abort` after on_failure")
            else:
                break

        s.span = self.span_from(start)
        return s

    def parse_await(self) -> WaitDecl:
        start = self.next()           # await
        w = WaitDecl(kind="signal")
        if self.cur.kind == T.NAME and self.at(1).is_op("="):
            w.binding = self.next().value
            self.next()
        if self.cur.kind == T.TEXT:
            w.name = self.next().payload
        else:
            w.name = self.expect_name("a signal name")
        if self.eat_ctx("deadline"):
            w.deadline = self.expect_int("a deadline in milliseconds")
        w.span = self.span_from(start)
        return w

    def parse_timer(self) -> WaitDecl:
        start = self.next()           # timer
        w = WaitDecl(kind="sleep")
        w.millis = self.expect_int("a duration in milliseconds")
        w.name = f"sleep_{w.millis}"
        w.span = self.span_from(start)
        return w


# --------------------------------------------------------------------------
# Lowering
# --------------------------------------------------------------------------

def _lit(v, k="text"):
    return A.Lit(value=v, lit_kind=k)


def _var(n):
    return A.Var(name=n)


def _perform(effect, op, args):
    return A.Perform(effect=effect, op=op, args=list(args))


class Lowering:
    def __init__(self, bag: Bag):
        self.bag = bag

    def lower_module(self, mod: A.Module, workflows: list) -> A.Module:
        if workflows:
            mod.decls.extend(_runtime_decls())
        for w in workflows:
            mod.decls.append(self.lower_workflow(w))
        mod.language = "loom"
        return mod

    # ------------------------------------------------------------------

    def lower_workflow(self, w: WorkflowDecl) -> A.FnDecl:
        uses = list(w.uses)
        for key in ("checkpoint", "compensated"):
            uses.append(A.EffectRef(effect="workflow", op=key))
        if any(isinstance(i, WaitDecl) and i.kind == "signal" for i in w.items):
            uses.append(A.EffectRef(effect="workflow", op="await_signal"))
        if any(isinstance(i, WaitDecl) and i.kind == "sleep" for i in w.items):
            uses.append(A.EffectRef(effect="workflow", op="sleep"))

        fn = A.FnDecl(
            name=w.name,
            doc=w.doc,
            intent=w.intent or f"Run the {w.name} workflow.",
            params=list(w.params),
            result=w.result,
            uses=uses,
            cost=_with_deadline(w.cost, w.deadline),
            origin="loom",
            span=w.span)

        if w.idempotent_by:
            fn.laws.append(A.LawRef(name="idempotent_by",
                                    args=[_var(w.idempotent_by)]))

        # Built back to front: each step wraps everything that follows it, so
        # the failure path of step k has the compensations of steps 0..k-1
        # available in written order.
        body = self._lower_items(w, list(w.items), completed=[])
        fn.body = body if isinstance(body, A.Block) \
            else A.Block(stmts=[], result=body)
        return fn

    def _lower_items(self, w: WorkflowDecl, items: list, completed: list):
        """
        Lower the remaining items, given the steps already completed.

        `completed` is the compensation stack as it stands at this point. It is
        threaded through lowering rather than maintained at runtime, so the
        unwinding for every failure point is written out explicitly.
        """
        if not items:
            return w.final

        item = items[0]
        rest = items[1:]

        if isinstance(item, A.Stmt):
            inner = self._lower_items(w, rest, completed)
            return _prefix(inner, [item])

        if isinstance(item, WaitDecl):
            return self._lower_wait(w, item, rest, completed)

        return self._lower_step(w, item, rest, completed)

    # ------------------------------------------------------------------

    def _lower_step(self, w: WorkflowDecl, step: StepDecl, rest: list,
                    completed: list):
        attempt_stmts = []
        # Checkpoint before the attempt. On replay this is what lets the
        # Ledger recognise a step that already ran.
        attempt_stmts.append(A.SExpr(value=_perform(
            "workflow", "checkpoint",
            [_lit(w.name), _lit(step.name), _lit(0, "int")])))

        tmp = f"_{step.name}_result"
        attempt_stmts.append(A.SLet(name=tmp, value=step.expr))

        # Retries are unrolled. Each retry re-checkpoints with its attempt
        # number so the journal shows exactly how many calls were made.
        for attempt in range(1, step.retries + 1):
            prev = tmp
            tmp = f"_{step.name}_retry{attempt}"
            attempt_stmts.append(A.SLet(
                name=tmp,
                value=A.If(
                    cond=_call_q("Result", "is_err", [_var(prev)]),
                    then=A.Block(
                        stmts=[A.SExpr(value=_perform(
                            "workflow", "checkpoint",
                            [_lit(w.name), _lit(step.name),
                             _lit(attempt, "int")]))],
                        result=step.expr),
                    otherwise=_var(prev))))

        ok_binding = step.binding or f"_{step.name}_value"
        now_completed = completed + [step]

        success = self._lower_items(w, rest, now_completed)
        success_arm = A.MatchArm(
            pattern=A.PCtor(name="Ok", args=[A.PVar(name=ok_binding)]),
            body=success)

        if step.optional:
            # A step marked `on_failure continue` does not unwind; it carries
            # on with the failure recorded in the journal.
            failure_body = self._lower_items(w, rest, now_completed)
            failure_body = _prefix(failure_body, [A.SExpr(value=_perform(
                "workflow", "compensated",
                [_lit(w.name), _lit(step.name + ":skipped")]))])
            failure_arm = A.MatchArm(
                pattern=A.PCtor(name="Err", args=[A.PVar(name="_skipped")]),
                body=_rebind(failure_body, ok_binding, step))
        else:
            failure_arm = A.MatchArm(
                pattern=A.PCtor(name="Err", args=[A.PVar(name="_failure")]),
                body=self._unwind(w, completed, "_failure"))

        match = A.Match(scrutinee=_var(tmp), arms=[success_arm, failure_arm],
                        span=step.span)
        return A.Block(stmts=attempt_stmts, result=match, span=step.span)

    def _lower_wait(self, w: WorkflowDecl, wait: WaitDecl, rest: list,
                    completed: list):
        if wait.kind == "sleep":
            inner = self._lower_items(w, rest, completed)
            return _prefix(inner, [A.SExpr(value=_perform(
                "workflow", "sleep", [_lit(wait.millis, "int")]))])

        tmp = f"_await_{wait.name}"
        stmts = [A.SLet(name=tmp, value=_perform(
            "workflow", "await_signal",
            [_lit(wait.name), _lit(wait.deadline, "int")]))]
        binding = wait.binding or f"_{wait.name}_payload"
        success = self._lower_items(w, rest, completed)
        match = A.Match(
            scrutinee=_var(tmp),
            arms=[
                A.MatchArm(pattern=A.PCtor(name="Ok",
                                           args=[A.PVar(name=binding)]),
                           body=success),
                A.MatchArm(pattern=A.PCtor(name="Err",
                                           args=[A.PVar(name="_timeout")]),
                           body=self._unwind(w, completed, "_timeout")),
            ],
            span=wait.span)
        return A.Block(stmts=stmts, result=match, span=wait.span)

    def _unwind(self, w: WorkflowDecl, completed: list, error_var: str):
        """Run the compensations of completed steps in reverse, then fail."""
        stmts = []
        for step in reversed(completed):
            if step.compensate is None:
                continue
            stmts.append(A.SExpr(value=step.compensate))
            stmts.append(A.SExpr(value=_perform(
                "workflow", "compensated",
                [_lit(w.name), _lit(step.name)])))
        return A.Block(stmts=stmts,
                       result=A.CtorCall(name="Err", args=[_var(error_var)]))


def _call_q(module, name, args):
    return A.Call(fn=A.QualVar(module=module, name=name), args=list(args))


def _prefix(expr, stmts: list):
    if isinstance(expr, A.Block):
        return A.Block(stmts=list(stmts) + list(expr.stmts),
                       result=expr.result, span=expr.span)
    return A.Block(stmts=list(stmts), result=expr)


def _rebind(expr, name, step: StepDecl):
    """
    Give an optional step's binding a value on the failure path.

    A step marked `on_failure continue` still binds a name, and the code after
    it has to typecheck on both paths. There is no sensible success value to
    invent, so the binding is only introduced when nothing reads it; a step
    that binds a name it then uses cannot be optional.
    """
    return expr


def _with_deadline(cost: Optional[A.Cost], deadline: int) -> Optional[A.Cost]:
    if not deadline:
        return cost
    c = cost or A.Cost()
    if c.millis is None:
        c.millis = deadline
    return c


def _runtime_decls() -> list:
    src = "module _loom_runtime\n" + RUNTIME_EFFECT
    lx = Lexer(src, "<loom-runtime>")
    toks = lx.run()
    p = Parser(toks, src, "<loom-runtime>", lx.bag)
    mod = p.parse_module()
    if p.bag.has_errors:
        raise RuntimeError("loom runtime effect failed to parse:\n"
                           + p.bag.render(src))
    return mod.decls


def _is_result(t) -> bool:
    return isinstance(t, A.TName) and t.name == "Result" and len(t.args) == 2


def _tyname(t) -> str:
    return t.name if isinstance(t, A.TName) else "?"


def _step_name_of(expr) -> str:
    if isinstance(expr, A.Perform):
        return f"{expr.effect}_{expr.op}"
    if isinstance(expr, A.Call):
        fn = expr.fn
        if isinstance(fn, A.Var):
            return fn.name
        if isinstance(fn, A.QualVar):
            return f"{fn.module}_{fn.name}"
    return "step"


# --------------------------------------------------------------------------

def parse_loom(source: str, filename: str = "<memory>"):
    """Parse and lower Loom source. Returns (Canon Module, Bag)."""
    lx = Lexer(source, filename)
    toks = lx.run()
    p = LoomParser(toks, source, filename, lx.bag)
    mod = p.parse_module()
    mod = Lowering(p.bag).lower_module(mod, p.workflows)
    return mod, p.bag
