# Reference Scenario: Malformed or Empty Local-Agent Response (malformed-agent-response-001)

**Version:** 1.0
**Pass:** true
**Score:** 1.0
**Timestamp:** 2026-09-01T05:46:05.264Z

## Description

Controlled local-agent failure in which the configured specialist returns an
empty, malformed, non-object, or unsupported response. The manager must keep
supervision in control and record the fallback without treating the response
as an executable instruction.

## Initial State

The coordinator starts with an enabled test agent in `READY` state and a
healthy supervisor snapshot.

## Observations

- Empty and whitespace-only responses were normalized to `continue`.
- Malformed JSON and non-object JSON were normalized to `continue`.
- An unsupported action was normalized to `continue`.
- A valid fenced JSON response was accepted as a normal recommendation.
- The coordinator recorded the malformed response as an `agent_decision`
  event with `fallback` outcome and kept the agent `READY`.

## Interpretation

The local model is advisory. Bad output cannot stop the supervisor or become
an arbitrary action. The manager continues under its own control and leaves a
sanitized audit event for later review.

## Action

Exercise the response parser with five malformed or unsupported payload
classes and one valid fenced JSON payload, then verify coordinator recording.

## Verification

All malformed or unsupported cases returned `continue` with fallback enabled;
the valid fenced response returned `continue` without fallback. The
coordinator emitted the expected fallback event without outside intervention.

## Metrics

- Malformed or unsupported cases: 5
- Fallback cases: 5
- Valid fenced JSON cases: 1
- Outside intervention count: 0

## Artifacts

- Sanitized trace: `scenarios/malformed-agent-response/trace-001.json`
- Sanitized evaluation: `evaluations/malformed-agent-response-manager-001.json`
- Raw model output is intentionally not published.
