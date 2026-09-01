# Reference Scenario: Resource Pressure (resource-pressure-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T05:58:19.675Z

## Description

Controlled synthetic-worker evaluation in which the process remains alive and
its heartbeat remains fresh while the resource probe reports pressure. The
manager must treat the missing resource signal as unhealthy, then return to
`RUNNING` when capacity is restored.

## Initial State

The synthetic worker starts in `RUNNING` state with an active resource signal,
fresh heartbeat, and no restart history.

## Observations

- The worker stayed present during the pressure window.
- The heartbeat stayed fresh during the pressure window.
- The resource probe changed to inactive and the supervisor entered `STALLED`.
- Restoring the resource signal returned the supervisor to `RUNNING`.
- No restart or outside intervention was needed.

## Interpretation

Resource availability is part of useful-work evidence. A live worker with a
fresh heartbeat is not enough when the required resource is unavailable, but a
transient pressure signal need not cause an unnecessary restart if capacity
returns before recovery is requested.

## Action

Run the synthetic worker, switch its resource probe to a pressured state,
observe the multi-signal classification, restore capacity, and verify healthy
operation without a restart.

## Verification

The pressured observation was `STALLED` with process and heartbeat still
healthy; the next observation was verified `RUNNING` with restart count zero.

## Metrics

- Worker alive during pressure: true
- Fresh heartbeat during pressure: true
- Resource active during pressure: false
- Restart count: 0
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/resource-pressure/trace-001.json`
- Sanitized evaluation: `evaluations/resource-pressure-manager-001.json`
- Raw local resource details are intentionally not published.
