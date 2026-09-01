# Reference Scenario: Objective Change While Running (objective-change-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T06:10:07.127Z

## Description

Controlled objective handoff request while a synthetic worker is healthy and
running. The new objective must become a durable queue item without silently
changing the active job or its worker assignment.

## Initial State

The synthetic worker is `RUNNING` on `objective-current` with no restart
history.

## Observations

- A request for `objective-next` was written to the durable work queue.
- The active job continued to report `objective-current`.
- The active worker stayed `RUNNING` and its assignment was not replaced.
- The queued objective task was claimed and completed independently.
- The manager recorded `objective_change_queued` with
  `active_job_preserved` outcome.

## Interpretation

An objective change is a handoff, not an implicit mutation of work already in
flight. This keeps the current result interpretable and gives a future worker
or explicit scheduler step a durable place to start the next objective.

## Action

Run a healthy synthetic job, queue a different objective, claim and complete
the queue item, and verify that the original worker continues unchanged.

## Verification

The queue request was durable, the active objective and worker assignment were
preserved, the worker remained healthy, and restart count stayed at zero.

## Metrics

- Active objective preserved: true
- Requested objective: `objective-next`
- Queue task kind: `objective_change`
- Worker continued running: true
- Restart count: 0
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/objective-change/trace-001.json`
- Sanitized evaluation: `evaluations/objective-change-manager-001.json`
- Worker process details are intentionally not published.
