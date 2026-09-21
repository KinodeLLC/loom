"""
Loom's runtime handlers.

Loom generates calls to a `workflow` effect, so Loom supplies its handlers
rather than the Ledger knowing anything about workflows. Installing them is a
separate, explicit step: a Ledger with no workflow handlers will refuse the
checkpoint rather than silently running a workflow with no durability.

The handlers themselves are deliberately thin. Checkpointing is just a
journaled effect -- the Ledger's replay mode is what actually makes a workflow
resumable, and adding state here would create a second source of truth that
could disagree with the journal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from canon import values as V


@dataclass
class SignalBox:
    """
    Signals delivered to waiting workflows.

    A signal that has not arrived is a timeout rather than a block: the
    interpreter is synchronous, and a workflow that cannot proceed should
    unwind and be resumed later from its journal rather than hold a thread.
    """
    delivered: dict = field(default_factory=dict)
    waits: list = field(default_factory=list)

    def deliver(self, name: str, payload: str = ""):
        self.delivered[name] = payload

    def take(self, name: str):
        if name in self.delivered:
            return self.delivered.pop(name)
        return None


@dataclass
class WorkflowTrace:
    """What happened during a run, for inspection and for tests."""
    checkpoints: list = field(default_factory=list)
    compensations: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    waits: list = field(default_factory=list)

    def steps_started(self) -> list:
        return [s for _, s, a in self.checkpoints if a == 0]

    def attempts(self, step: str) -> int:
        return len([1 for _, s, _ in self.checkpoints if s == step])

    def to_json(self) -> dict:
        return {"checkpoints": [{"workflow": w, "step": s, "attempt": a}
                                for w, s, a in self.checkpoints],
                "compensations": [{"workflow": w, "step": s}
                                  for w, s in self.compensations],
                "sleeps": list(self.sleeps),
                "waits": list(self.waits)}


def install(ledger, signals: Optional[SignalBox] = None,
            trace: Optional[WorkflowTrace] = None,
            observe_sleep=None):
    """
    Register the `workflow` effect handlers on a Ledger.

    Returns the trace so a caller can see the step sequence without reading
    the journal. Returns the same ledger for chaining.
    """
    signals = signals if signals is not None else SignalBox()
    trace = trace if trace is not None else WorkflowTrace()

    def checkpoint(workflow_name, step, attempt):
        trace.checkpoints.append((workflow_name, step, attempt))
        return V.UNIT

    def compensated(workflow_name, step):
        trace.compensations.append((workflow_name, step))
        return V.UNIT

    def sleep(millis):
        trace.sleeps.append(millis)
        if observe_sleep is not None:
            observe_sleep(millis)
        return V.UNIT

    def await_signal(name, deadline_millis):
        trace.waits.append((name, deadline_millis))
        payload = signals.take(name)
        if payload is None:
            return V.err(f"no signal {name!r} within {deadline_millis}ms")
        return V.ok(payload)

    ledger.handle("workflow.checkpoint", checkpoint)
    ledger.handle("workflow.compensated", compensated)
    ledger.handle("workflow.sleep", sleep)
    ledger.handle("workflow.await_signal", await_signal)

    ledger.loom_trace = trace
    ledger.loom_signals = signals
    return ledger


def trace_of(ledger) -> Optional[WorkflowTrace]:
    return getattr(ledger, "loom_trace", None)


def resume(check_result, ledger_factory, journal, entry: str, args: list,
           budget=None):
    """
    Resume a workflow from a recorded journal.

    Completed steps return their recorded results without being performed
    again, so this continues from the first step that never finished. The
    journal is the only state involved.
    """
    from canon.interp import Budget, Interpreter
    from canon.ledger import REPLAY

    led = ledger_factory(REPLAY, journal)
    it = Interpreter(check_result, led, budget or Budget())
    return it.call(entry, list(args)), led
