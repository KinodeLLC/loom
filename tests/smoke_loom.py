"""Loom: compensation, retries, checkpoints, and resumption by replay."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "canon" / "src"))
sys.path.insert(0, str(ROOT / "loom" / "src"))

from canon import format_module  # noqa: E402
from canon import values as V  # noqa: E402
from canon.checker import check  # noqa: E402
from canon.interp import Budget, Fault, Interpreter  # noqa: E402
from canon.ledger import REPLAY, AuditLog, CapabilityBroker, Ledger  # noqa: E402
import loom  # noqa: E402
from loom import parse_loom  # noqa: E402

SRC = r'''
module fulfilment

record Order {
  id: Text
  customer: Text
  sku: Text
  qty: Int
  total: Int
}

record Receipt {
  order_id: Text
  charged: Int
}

enum FulfilError {
  | OutOfStock
  | PaymentDeclined
  | NotifyFailed
  | TimedOut
}

effect inventory {
  reserve(order_id: Text, sku: Text, qty: Int) -> Result<Text, FulfilError>
  release(order_id: Text) -> Unit
}

effect payments {
  charge(customer: Text, amount: Int) -> Result<Text, FulfilError>
  refund(customer: Text, amount: Int) -> Unit
}

effect mail {
  send(to: Text, subject: Text, body: Text) -> Result<Text, FulfilError>
}

workflow fulfil(order: Order) -> Result<Receipt, FulfilError>
  intent "Reserve stock, take payment, and confirm the order."
  uses inventory.reserve, inventory.release, payments.charge, payments.refund, mail.send
  deadline 3600000
  idempotent by order
{
  step reservation = inventory.reserve(order.id, order.sku, order.qty)
    compensate inventory.release(order.id)
    retry 2

  step payment = payments.charge(order.customer, order.total)
    compensate payments.refund(order.customer, order.total)

  step confirmation = mail.send(order.customer, "Order confirmed", order.id)
    on_failure continue

  Ok(Receipt { order_id: order.id, charged: order.total })
}
'''


def build(src):
    mod, bag = parse_loom(src, "fulfilment.loom")
    if bag.has_errors:
        return None, bag, mod
    res = check([mod], bag)
    return res, res.bag, mod


class Scripted:
    """A handler set that can be told which effects fail."""

    def __init__(self, fail=(), fail_times=None):
        self.fail = set(fail)
        self.fail_times = dict(fail_times or {})
        self.calls = []

    def install(self, led: Ledger):
        def handler(key, err_ctor):
            def run(*args):
                self.calls.append((key, args))
                n = self.fail_times.get(key)
                if n is not None:
                    if len([c for c, _ in self.calls if c == key]) <= n:
                        return V.err(V.Variant(err_ctor, (), "FulfilError"))
                    return V.ok("ok-" + key)
                if key in self.fail:
                    return V.err(V.Variant(err_ctor, (), "FulfilError"))
                return V.ok("ok-" + key)
            return run

        def unit_handler(key):
            def run(*args):
                self.calls.append((key, args))
                return V.UNIT
            return run

        led.handle("inventory.reserve", handler("inventory.reserve", "OutOfStock"))
        led.handle("payments.charge", handler("payments.charge", "PaymentDeclined"))
        led.handle("mail.send", handler("mail.send", "NotifyFailed"))
        led.handle("inventory.release", unit_handler("inventory.release"))
        led.handle("payments.refund", unit_handler("payments.refund"))
        return led

    def ops(self):
        return [k for k, _ in self.calls]


def make_ledger(scripted, mode="live", source=None, signals=None):
    audit = AuditLog(actor="workflow")
    broker = CapabilityBroker(audit=audit)
    broker.grant("workflow", ["*"], reason="smoke test")
    led = Ledger(broker=broker, audit=audit, mode=mode, actor="workflow")
    led._source = source
    loom.install(led, signals=signals)
    return scripted.install(led)


def order(qty=2, total=5000):
    return V.Record("Order", (("id", "o-1"), ("customer", "c-1"),
                              ("sku", "sku-9"), ("qty", qty), ("total", total)))


def main():
    res, bag, mod = build(SRC)
    if res is None or bag.has_errors:
        print(bag.render(SRC))
        return 1
    errs = [d for d in bag if d.severity.value == "error"]
    if errs:
        for d in errs:
            print(d.render(SRC))
        return 1

    failures = []

    def case(name, fn):
        try:
            print(f"  ok    {name}: {fn()}")
        except AssertionError as ae:
            failures.append(name)
            print(f"  FAIL  {name}: {ae}")

    print("lowering")

    def t_lowered():
        fi = res.env.fns["fulfilment.fulfil"]
        keys = sorted({e.key() for e in fi.decl.uses})
        assert "workflow.checkpoint" in keys, keys
        assert "workflow.compensated" in keys, keys
        laws = [l.name for l in fi.decl.laws]
        assert "idempotent_by" in laws, laws
        assert fi.decl.cost is not None and fi.decl.cost.millis == 3600000
        return f"uses {len(keys)} operations, deadline {fi.decl.cost.millis}ms"
    case("a workflow lowers to a Canon function with its runtime effects",
         t_lowered)

    def t_renders():
        text = format_module(mod)
        assert "fn fulfil(order: Order) -> Result<Receipt, FulfilError>" in text
        assert "workflow.checkpoint" in text
        return f"{len(text.splitlines())} lines of canonical Canon"
    case("the lowered workflow renders as ordinary Canon", t_renders)

    print("\nhappy path")

    def t_success():
        s = Scripted()
        led = make_ledger(s)
        it = Interpreter(res, led, Budget())
        r = it.call("fulfil", [order()])
        assert V.is_ok(r), V.show(r)
        receipt = r.args[0]
        assert receipt.get("charged") == 5000
        assert s.ops() == ["inventory.reserve", "payments.charge", "mail.send"], \
            s.ops()
        return f"3 steps ran in order, receipt charged {receipt.get('charged')}"
    case("all steps run in order on success", t_success)

    def t_checkpoints():
        s = Scripted()
        led = make_ledger(s)
        Interpreter(res, led, Budget()).call("fulfil", [order()])
        cps = [e for e in led.journal if e.op == "workflow.checkpoint"]
        names = [e.args[1] for e in cps]
        assert names == ["reservation", "payment", "confirmation"], names
        return f"checkpointed {names}"
    case("every step checkpoints before it runs", t_checkpoints)

    print("\ncompensation")

    def t_compensate():
        s = Scripted(fail=["payments.charge"])
        led = make_ledger(s)
        it = Interpreter(res, led, Budget())
        r = it.call("fulfil", [order()])
        assert V.is_err(r), V.show(r)
        assert r.args[0].ctor == "PaymentDeclined", V.show(r.args[0])
        assert "inventory.release" in s.ops(), s.ops()
        assert "payments.refund" not in s.ops(), \
            "compensated a step that never completed"
        comp = [e.args[1] for e in led.journal if e.op == "workflow.compensated"]
        assert comp == ["reservation"], comp
        return f"payment failed, compensated {comp}, returned PaymentDeclined"
    case("a failed step unwinds the steps before it", t_compensate)

    def t_no_compensation_needed():
        s = Scripted(fail=["inventory.reserve"])
        led = make_ledger(s)
        r = Interpreter(res, led, Budget()).call("fulfil", [order()])
        assert V.is_err(r) and r.args[0].ctor == "OutOfStock", V.show(r)
        assert "inventory.release" not in s.ops(), s.ops()
        assert "payments.charge" not in s.ops(), s.ops()
        return "first step failed, nothing to compensate, nothing downstream ran"
    case("a first-step failure compensates nothing", t_no_compensation_needed)

    def t_reverse_order():
        # Force the last mandatory step to fail so both earlier compensations
        # run, and check they run in reverse.
        src = SRC.replace(
            "  step confirmation = mail.send(order.customer, \"Order confirmed\", order.id)\n"
            "    on_failure continue\n",
            "  step confirmation = mail.send(order.customer, \"Order confirmed\", order.id)\n"
            "    compensate mail.send(order.customer, \"Order cancelled\", order.id)\n")
        r2, b2, _ = build(src)
        assert r2 is not None and not b2.has_errors, b2.render(src)
        s = Scripted(fail=["mail.send"])
        led = make_ledger(s)
        result = Interpreter(r2, led, Budget()).call("fulfil", [order()])
        assert V.is_err(result), V.show(result)
        comp = [e.args[1] for e in led.journal if e.op == "workflow.compensated"]
        assert comp == ["payment", "reservation"], comp
        return f"compensated in reverse: {comp}"
    case("compensations run in reverse order", t_reverse_order)

    print("\nretries")

    def t_retry():
        # reserve fails twice then succeeds; the step declares retry 2.
        s = Scripted(fail_times={"inventory.reserve": 2})
        led = make_ledger(s)
        r = Interpreter(res, led, Budget()).call("fulfil", [order()])
        assert V.is_ok(r), V.show(r)
        attempts = len([k for k in s.ops() if k == "inventory.reserve"])
        assert attempts == 3, attempts
        cps = [(e.args[1], e.args[2]) for e in led.journal
               if e.op == "workflow.checkpoint" and e.args[1] == "reservation"]
        assert cps == [("reservation", 0), ("reservation", 1),
                       ("reservation", 2)], cps
        return f"{attempts} attempts, each checkpointed with its attempt number"
    case("a retried step is attempted the declared number of times", t_retry)

    def t_retry_exhausted():
        s = Scripted(fail_times={"inventory.reserve": 9})
        led = make_ledger(s)
        r = Interpreter(res, led, Budget()).call("fulfil", [order()])
        assert V.is_err(r), V.show(r)
        attempts = len([k for k in s.ops() if k == "inventory.reserve"])
        assert attempts == 3, attempts
        assert "payments.charge" not in s.ops(), s.ops()
        return f"gave up after {attempts} attempts without proceeding"
    case("retries are bounded by the declared count", t_retry_exhausted)

    print("\noptional steps")

    def t_optional():
        s = Scripted(fail=["mail.send"])
        led = make_ledger(s)
        r = Interpreter(res, led, Budget()).call("fulfil", [order()])
        assert V.is_ok(r), V.show(r)
        assert "inventory.release" not in s.ops(), \
            "an optional failure caused an unwind"
        skipped = [e.args[1] for e in led.journal
                   if e.op == "workflow.compensated"]
        assert skipped == ["confirmation:skipped"], skipped
        return "notification failed, workflow still succeeded, failure recorded"
    case("a step marked on_failure continue does not unwind", t_optional)

    print("\nresumption")

    def t_resume():
        # Run to completion, then resume the same workflow from the journal.
        # Replay must produce the same result without calling any handler.
        s = Scripted()
        led = make_ledger(s)
        first = Interpreter(res, led, Budget()).call("fulfil", [order()])

        s2 = Scripted()
        led2 = make_ledger(s2, mode=REPLAY, source=led.journal)
        second = Interpreter(res, led2, Budget()).call("fulfil", [order()])

        assert V.compare(first, second) == 0, \
            f"{V.show(first)} != {V.show(second)}"
        assert s2.calls == [], \
            f"resumption re-performed effects: {s2.ops()}"
        return (f"replayed {len(led.journal)} journal entries, "
                f"0 handler calls, identical result")
    case("a workflow resumes from its journal without repeating effects",
         t_resume)

    print("\nstructural requirements")

    def t_must_return_result():
        bad = SRC.replace("workflow fulfil(order: Order) -> Result<Receipt, FulfilError>",
                          "workflow fulfil(order: Order) -> Receipt")
        _, b, _ = build(bad)
        assert b.has_errors, "a workflow not returning Result compiled"
        d = next(x for x in b if "must return Result" in x.message)
        return d.message
    case("a workflow must return Result", t_must_return_result)

    def t_duplicate_steps():
        bad = SRC.replace("  step payment = payments.charge(order.customer, order.total)",
                          "  step reservation = payments.charge(order.customer, order.total)")
        _, b, _ = build(bad)
        assert b.has_errors, "duplicate step names compiled"
        d = next(x for x in b if x.code == "CANON-E0203")
        return d.message
    case("step names must be unique within a workflow", t_duplicate_steps)

    def t_retry_bound():
        bad = SRC.replace("    retry 2", "    retry 99")
        _, b, _ = build(bad)
        assert b.has_errors, "an unbounded retry count compiled"
        d = next(x for x in b if "retry count must be" in x.message)
        return d.message
    case("retry counts are bounded", t_retry_bound)

    print("\nRESULT:", "pass" if not failures else f"FAIL ({failures})")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
