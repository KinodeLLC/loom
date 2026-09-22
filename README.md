# Loom

durable workflows. the compensation for a step sits next to the step, and
resuming a crashed workflow is replay off the journal.

part of [kinode](../kinode-stack). lowers to [canon](../canon).

## install

```sh
pip install -e .
```

## example

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

## compensation

when a step fails every step before it gets undone in reverse order, and the
code that does the undoing gets written into the generated canon when you
compile, so you can read exactly what will happen for every failure point
before you run anything. with a runtime stack you only find out during an
actual failure

```
ok  a failed settlement reverses the staged booking:
    settlement failed, staged entry reversed, customer not notified
```

`on_failure continue` on a step means do not unwind what came before it, so a
confirmation email that fails does not reverse a loan you already settled

## resumption

every step writes a checkpoint before it runs, so resuming a crashed workflow
is just replay. the steps that already finished hand back what they recorded
instead of running again, and you pick up at the first one that never finished

```
ok  an interrupted origination resumes without re-billing the bureau:
    replayed 8 journal entries, no external call repeated, identical offer
```

there is no separate state store for any of this so there is nothing that can
get out of sync with what actually happened

## retries

unrolled, not looped, so every workflow stays total and the number of times you
can hit an external system is something you can read in the source. capped at
8, and every attempt checkpoints with its attempt number on it

## step forms

| form | what it does |
| --- | --- |
| `step name = expr` | names the step and binds the value |
| `step name: expr` | names it, no binding |
| `step expr` | derives a name off the expression |

modifiers are `compensate <expr>`, `retry N`, and `on_failure continue` or
`abort`. you also get `await <signal> deadline N`, `timer N`, and normal `let`
`do` `assert` statements

step expressions return `Result` and so does the workflow, since that is what
carries the failure back out

## runtime

loom brings its own handlers, the journal does not know anything about
workflows

```python
import loom
from canon.ledger import Ledger

led = Ledger(...)
loom.install(led)
trace = loom.trace_of(led)
print(trace.steps_started())   # ['credit_file', 'booking', 'settlement']
```

if you skip `loom.install` it errors on the first checkpoint. that is on
purpose, you would rather that than a workflow that looks like it is running
and is not recording anything

## tests

```sh
python tests/smoke_loom.py
```

## licence

Apache-2.0, Kinode.
