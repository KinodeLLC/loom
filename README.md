# Loom

A durable workflow language. Compensation sits next to the step it undoes, and
resumption is journal replay rather than a separate state store.

Part of the [Kinode](../kinode-stack) stack. Lowers to [Canon](../canon).

## Install

```sh
pip install -e .
```

## A workflow

```loom
module fulfilment

workflow originate(request: LoanRequest, applicant: Applicant)
    -> Result<LoanOffer, OriginationError>
  intent "Pull a credit file, decide, book the loan, and confirm it."
  uses bureau.pull, ledger.append, ledger.commit, ledger.reverse, notify.email
  deadline 900000
  idempotent by request
{
  step credit_file = bureau.pull(request.applicant_id)
    retry 2

  let offer = build_offer(request, assess_outcome(...), credit_file)

  step booking: ledger.append(request.reference, offer.amount)
    compensate ledger.reverse(request.reference)

  step settlement: ledger.commit(request.reference)

  step confirmation: notify.email(applicant.email, "Your offer", request.reference)
    on_failure continue

  Ok(offer)
}
```

## Why it is shaped this way

**Compensation is emitted explicitly.** The unwinding for every failure point
is written into the generated Canon rather than driven by a runtime stack — so
it is visible in the code and in the journal before it ever runs, and
provably runs in reverse order:

```
ok  a failed settlement reverses the staged booking:
    settlement failed, staged entry reversed, customer not notified
```

**Durability reuses the Ledger.** Each step checkpoints to the effect journal
before running, so resuming a crashed workflow is ordinary replay — completed
steps return their recorded results without being performed again:

```
ok  an interrupted origination resumes without re-billing the bureau:
    replayed 8 journal entries, no external call repeated, identical offer
```

There is no workflow state store that can fall out of sync with what actually
happened.

**Retries are unrolled, not looped.** This keeps every workflow total and keeps
the number of times an external system can be called a fact visible in the
source. Bounded at 8; each attempt checkpoints with its attempt number.

## Step forms

| Form | Effect |
| --- | --- |
| `step name = expr` | Names the step and binds its value |
| `step name: expr` | Names the step without binding |
| `step expr` | Derives a name from the expression |

Modifiers: `compensate <expr>`, `retry N`, `on_failure continue` / `abort`.

Also available: `await <signal> deadline N`, `timer N`, and ordinary `let`,
`do` and `assert` statements.

Step expressions must return `Result`; the workflow's result type must be
`Result` too, since that is what carries the failure.

## Runtime

Loom supplies its own effect handlers rather than the Ledger knowing about
workflows:

```python
import loom
from canon.ledger import Ledger

led = Ledger(...)
loom.install(led)                    # workflow.checkpoint, compensated, sleep, await_signal
trace = loom.trace_of(led)
print(trace.steps_started())         # ['credit_file', 'booking', 'settlement']
```

A Ledger with no workflow handlers refuses the checkpoint rather than silently
running a workflow with no durability.

## Tests

```sh
python tests/smoke_loom.py
```

## Licence

Apache-2.0. Copyright Kinode.
