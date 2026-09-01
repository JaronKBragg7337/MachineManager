# Reference Scenario: Manager Restart Recovery (manager-restart-recovery-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T06:01:48.329Z

## Description

Controlled manager-only interruption during the live proving-ground run. The
protected KeyHunt worker must survive while the watchdog restores one manager,
which then adopts and verifies the existing worker.

## Initial State

The scheduled manager task is running with exactly one manager process and one
protected worker. The published local state is `HEALTHY` with a `RUNNING` job.

## Observations

- The manager child was stopped without stopping the protected worker.
- The worker count remained one immediately after the manager interruption.
- The watchdog restored exactly one manager.
- The durable event ledger recorded `worker_adopted` after restart.
- The resumed manager verified the job as `RUNNING` and `HEALTHY`.

## Interpretation

Manager availability and worker availability are separate concerns. A brief
manager restart need not interrupt useful work when the worker identity and
health evidence can be verified and adopted safely.

## Action

Verify singleton state, stop only the manager child, observe worker continuity,
wait for watchdog recovery, and confirm adoption plus healthy running state.

## Verification

Exactly one worker remained present throughout the interruption window, one
manager returned, and the SQLite event ledger recorded the adoption before the
next healthy state transition. Outside intervention count was zero.

## Metrics

- Manager count before and after: 1 / 1
- Protected worker count before, immediately after, and after: 1 / 1 / 1
- Adoption events after restart: 1
- Recovery to adopted worker: 23.697 s
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/manager-restart-recovery/trace-001.json`
- Sanitized evaluation: `evaluations/manager-restart-recovery-manager-001.json`
- Process identifiers and local paths are intentionally not published.
