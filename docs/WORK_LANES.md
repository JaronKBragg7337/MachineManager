# Work lanes

Work lanes make active Machine Manager missions visible on the public dashboard
without pretending that an invisible process is doing work. A lane is either:

- a configured runtime signal, such as a constraint audit or local specialist;
- an explicitly registered bounded mission; or
- a recorded completed mission.

The local SQLite store retains the detailed state. The public dashboard receives
only an allowlisted summary: title, lane, owner label, observed state, compact
metrics, next action, and timestamp. It never receives local paths, raw prompts,
credentials, private worker arguments, candidate material, or unverified
progress claims.

## Configuring a lane

Add a `workstreams` array to the local manager configuration. This data belongs
in the ignored local configuration, not the repository, when it describes a
specific machine or private project.

```json
{
  "id": "workspace-constraint-audit",
  "objective_id": "machine-manager-governance",
  "title": "Cross-project constraint audit",
  "lane": "Capability evidence",
  "owner": "evidence-coordinator",
  "summary": "Maps configured source into review leads without changing source files.",
  "next_action": "Continue the saved scan window, then review the evidence-backed leads.",
  "source": {
    "kind": "constraint_audit",
    "id": "active-project-workspace"
  }
}
```

Supported sources are:

- `constraint_audit` — maps the evidence engine's observed audit state and safe
  counts into a lane.
- `agent` — maps a configured bounded local-agent state and completed-run count.
- `worker_profile` — shows when a model/runtime profile is ready or needs a
  retest after a version change.
- `static` — records a deliberately registered mission state, including a
  completed milestone. It is not used to imply background execution.

## State meanings

`RUNNING` means the linked source currently reports active work. `REVIEW` means
the source produced evidence that needs a defined next test. `WAITING` means the
source is idle or waiting for its next scheduled run. `COMPLETE` records a
finished bounded mission. Other states are surfaced plainly rather than being
converted into a misleading success badge.

Work lanes complement process-supervised jobs. The KeyHunt workload remains a
job with its own health and recovery signals; a lane makes surrounding evidence,
research, delivery, and review work understandable alongside it.
