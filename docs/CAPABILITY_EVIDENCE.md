# Capability evidence and constraint review

Machine Manager uses evidence before it turns a limitation into a system rule.
The purpose is not to remove every guardrail. It is to avoid mistaking a
temporary model limitation, a provider behavior, or an old copied warning for
Jaron's permanent project policy.

## The operating rule

> Discover capability → test capability → record evidence → delegate work →
> retest after a material worker change.

An operator authorization, a worker's actual capability, a tool's available
surface, and a service's terms are separate facts. A capable worker should keep
moving an authorized objective forward. When a service presents a real
credential, 2FA, identity, payment, or equivalent service-owned step, the
manager records a precise handoff and resumes after it is completed.

## Worker capability profiles

Each configured profile has a provider, model/runtime version, a version that
was actually verified, and compact observations. Observation states are:

| State | Meaning |
| --- | --- |
| `TESTED_PASS` | A defined test passed for this version. |
| `TESTED_FAIL` | A defined test did not pass; the evidence is retained. |
| `OBSERVED` | The worker was seen doing the work, but no formal test is recorded yet. |
| `UNKNOWN` | No conclusion has been reached. |
| `UNAVAILABLE` | The required tool or connection was not available. |

If `model_version` differs from `verified_model_version`, the profile is marked
`RETEST_REQUIRED`. That is a reminder to run the relevant test suite, not a
claim that the new version cannot do the work.

Example local configuration:

```json
{
  "evidence": {
    "worker_profiles": [
      {
        "id": "codex-desktop",
        "provider": "openai",
        "model": "Codex desktop",
        "model_version": "current-release",
        "verified_model_version": "current-release",
        "last_verified": "2026-08-30T00:00:00Z",
        "capabilities": [
          {
            "id": "repository-build-and-test",
            "status": "TESTED_PASS",
            "summary": "Completed the local build and test contract.",
            "evidence_id": "codex-build-check-001",
            "observed_at": "2026-08-30T00:00:00Z"
          }
        ]
      }
    ]
  }
}
```

Profiles are local state. The public dashboard receives only a sanitized
summary: worker identity, public model label/version, observation state, and a
short safe summary.

## Constraint audit

The auditor scans only explicitly listed project directories. It ignores Git
metadata, dependency folders, logs, local state, `secrets/`, environment files,
and non-text files. It looks for candidate phrases such as approval gates,
manual-only language, autonomy prohibitions, scope boundaries, and
sensitive-data boundaries.

Its output is deliberately non-destructive:

1. It records local findings with a relative file location and short excerpt.
2. It publishes only counts and categories to the dashboard.
3. It never edits, removes, or labels a rule redundant by itself.
4. A reviewer decides whether the rule is required, provider-enforced,
   duplicated, obsolete, project-specific, or needs a real capability test.

This lets a project keep genuine authorization, privacy, or platform terms
without accidentally accumulating old model caveats forever.

## Adding projects safely

Add a local target to the ignored runtime config, not to public telemetry:

```json
{
  "evidence": {
    "audit_interval_s": 86400,
    "constraint_audits": [
      {
        "id": "project-alpha",
        "label": "Project Alpha",
        "path": "C:\\Projects\\ProjectAlpha",
        "enabled": true,
        "interval_s": 3600,
        "max_files": 1200,
        "max_findings": 250
      }
    ]
  }
}
```

The manager runs due audits in a low-priority background thread so worker
health checks are not paused. When a target has more files than its configured
window, the next scheduled pass resumes after the last local source file it
processed. It wraps to the beginning only after the full target has been
covered. Failed or unavailable audit targets become a recorded event; they do
not stop a protected workload.

## New worker onboarding

Before a new worker receives an execution role, record the capabilities it has
actually demonstrated for the intended job:

1. Inspect the available tools and connection state.
2. Run bounded capability tests.
3. Record passes, failures, observations, and unknowns.
4. Compare those results to the objective's requirements.
5. Add a project-level control only when evidence shows one is needed.
6. Retest after a material model, provider, tool, or permission change.

The goal is an operational record that gets more accurate over time—not an
ever-growing list of imagined limitations.
