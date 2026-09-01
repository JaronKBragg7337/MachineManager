# Reference Scenario: Stalled but Alive Worker (stalled-worker-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T05:37:55.415Z

## Description

Controlled synthetic-worker failure in which the process remains alive but its
heartbeat stops advancing. This is the failure class that process-only
monitoring misses.

## Initial State

The synthetic worker starts healthy in `RUNNING` state with a 100 ms heartbeat
freshness threshold. The evaluation then switches it to a mode that keeps the
process alive without writing another heartbeat.

## Observations

- The worker process remained present after the induced stall.
- The heartbeat became stale.
- The supervisor classified the job as `STALLED`, not healthy.
- A bounded restart restored a fresh heartbeat and verified `RUNNING` state.

## Interpretation

Useful work requires current evidence, not merely a living process. The
manager detected and recovered the stalled worker without outside intervention.

## Action

Induce heartbeat expiry, observe the multi-signal state, restart once with the
healthy synthetic mode, and verify the recovered heartbeat.

## Verification

The recovered worker passed the configured heartbeat check and the supervisor
returned to `RUNNING`.

## Metrics

- Detection after the stale threshold: 0.072 s
- Recovery to verified running: 0.109 s
- Restart count: 1
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/stalled-worker/trace-001.json`
- Sanitized evaluation: `evaluations/stalled-worker-manager-001.json`
- Raw local process details are intentionally not published.
