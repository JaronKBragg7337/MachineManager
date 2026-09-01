# Reference Scenario: Repeated Failure Escalation (repeated-failure-escalation-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T05:52:06.094Z

## Description

Controlled synthetic-worker failure in which the worker exits twice and the
configured retry budget permits only one restart. The manager must retry once,
then escalate instead of restarting forever.

## Initial State

The synthetic worker is launched under manager supervision with a maximum of
one restart.

## Observations

- The first worker exit was classified as `FAILED`.
- The manager performed one bounded retry.
- The second worker exit exhausted the retry budget.
- The manager entered `ESCALATED` and did not launch another worker.
- No outside intervention was used.

## Interpretation

Autonomous recovery needs a terminal boundary. A repeated failure is evidence
that another restart may only repeat the same failure, so the manager preserves
the history and escalates instead of creating an unbounded loop.

## Action

Launch the deterministic crash worker, observe the first failure, allow one
retry, observe the second failure, and verify terminal escalation.

## Verification

The final state was `ESCALATED`, the restart count was exactly one, the worker
was stopped, and the escalation event reported `retry_limit_reached`.

## Metrics

- Failure observations: 2
- Maximum restarts: 1
- Restart count: 1
- First retry launch: 0.016 s
- Escalation decision: 0.000 s
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/repeated-failure-escalation/trace-001.json`
- Sanitized evaluation: `evaluations/repeated-failure-escalation-manager-001.json`
- Raw local process details are intentionally not published.
