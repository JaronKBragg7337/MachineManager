# Machine Manager

Persistent local runtime for supervised autonomous work.

[Open the public live dashboard](https://jaronkbragg7337.github.io/MachineManager/dashboard/) · [Read the operating charter](docs/OPERATING_CHARTER.md) · [View the local runtime guide](docs/LOCAL_MANAGER.md)

Machine Manager keeps declared work moving while its host is awake and connected. It supervises workers, records durable local state, performs bounded recovery, and publishes a compact public view of what is actually happening. The local manager is the source of truth; this README describes the system, while the dashboard shows the latest published snapshot.

## What is running today

Bitcoin Puzzle #71 is the current real-world proving-ground workload. A local GPU search worker gives the manager a demanding, long-running job against which it can prove health checks, recovery, restart behavior, telemetry, and visible work evidence.

That workload is not the project’s limit. Machine Manager is being built as a general runtime for separate research, engineering, delivery, and opportunity-seeking objectives—each with its own worker, evidence, resource budget, success criteria, and next action.

Live status intentionally is not hard-coded here. Use the [public dashboard](https://jaronkbragg7337.github.io/MachineManager/dashboard/) for the current published state.

## System map

```text
Jaron (operator)
  |
  v
Machine Manager (persistent local runtime)
  |
  +-- Job registry, scheduler, event store, and capability evidence
  |
  +-- Health checks, bounded recovery, escalation, and resume records
  |
  +-- Specialist workers: GPU jobs, synthetic reliability workers,
  |   bounded local agents, and future task-specific workers
  |
  +-- Sanitized telemetry publisher
         |
         v
     GitHub Pages dashboard (public, read-only)
```

Reference runs can establish what competent recovery or task completion looks like. Manager evaluations then measure whether the local runtime can produce the needed result without external intervention. Those are distinct records, not interchangeable claims.

## What the runtime does

The repository-level Python runtime in [`manager/`](manager/) currently provides:

- Explicit job states: `QUEUED`, `STARTING`, `VERIFYING`, `RUNNING`, `COMPLETE`, `STALLED`, `FAILED`, `RETRYING`, `ESCALATED`, and `CANCELLED`.
- Multi-signal health checks: a worker must be alive and, when configured, show fresh progress plus a passing resource probe. A process alone is not treated as proof of useful work.
- Truthful machine telemetry: the public snapshot can show measured host CPU activity alongside GPU signals; a missing reading is displayed as unavailable rather than invented as `0%`.
- GPU activity presentation: when a driver reports a transient low utilization sample but the resource probe confirms active dedicated memory and power, the dashboard labels the signal `ACTIVE` and retains the raw sample in its explanation.
- Durable SQLite state for jobs, attempts, retries, events, queued work, worker adoption after a manager restart, and singleton protection against duplicate managers.
- Bounded recovery: retry budgets prevent an endless restart loop; exhausted jobs enter `ESCALATED` with their history intact. A configurable `retry_reset_after_s` clears old retries only after continuous verified health, so a long-lived worker gets a fresh budget without hiding repeated failures in the same run.
- GPU evidence tolerance: the NVIDIA probe accepts a brief zero-utilization sample when dedicated worker memory and active power still prove CUDA activity, while idle/no-memory readings remain unhealthy.
- A recorded one-time operator-resume mechanism for a specific escalated job, rather than silently erasing a failed history. See [the recovery guide](docs/LOCAL_MANAGER.md#one-time-operator-resume-after-escalation).
- Resilient public telemetry: transient dashboard-file locks retry locally, and a persistent telemetry failure defers that snapshot instead of terminating protected work.
- Synthetic workers and reliability tests for healthy work, live-but-stalled work, crashes, malformed local-agent output, resource pressure, escalation, manager restart recovery, event contracts, and public-telemetry boundaries. The recorded stalled-worker evaluation proves that a live process with stale evidence is detected and recovered; the malformed-response evaluation proves that bad model output is contained and logged while supervision continues; the repeated-failure evaluation proves that exhausted retries escalate instead of looping forever; the resource-pressure evaluation proves that capacity is part of health without forcing an unnecessary restart; the manager-restart evaluation proves that the watchdog can restore supervision while adopting the protected worker that survived.
- Bounded local specialist slots that can give safe, compact operational advice while the manager retains control of worker lifecycle and retry policy.
- Public agent views show whether a specialist is currently working, its bounded run timing, completed task count, and next run without publishing prompts or unrestricted reasoning.

The runtime is intentionally standard-library-first and can run without relying on a cloud connector.

## Public dashboard

The [Machine Manager dashboard](https://jaronkbragg7337.github.io/MachineManager/dashboard/) is a no-login, read-only operations view. It presents the published snapshot through views for:

- Overview, jobs, workers, search evidence, and agent activity
- Evaluations, research, autonomy, capability evidence, and work lanes
- A timestamped activity stream and operations view

Its data flow is deliberately one-way:

```text
Local manager observation
  -> sanitized telemetry projection
  -> validation before upload
  -> compact GitHub commit
  -> GitHub Pages dashboard
```

The local publisher writes a sanitized snapshot on each observation. When public upload is configured, ordinary telemetry is batched on a short cadence, while meaningful manager, worker, job, or work-lane state changes are published immediately after local validation. The page refreshes its published data once per minute and visibly marks stale snapshots, so it does not pretend to be a direct live connection to the laptop.

The public projection contains only allowlisted operational summaries and aggregate progress. It does not mirror raw logs, command lines, private machine details, credentials, or protected workload material. Details of the publishing boundary are in [the dashboard guide](docs/GITHUB_PAGES.md).

## Evidence-led autonomy

Machine Manager follows an **execute-and-report** operating model: discover useful work, qualify it, build or research, verify, submit or publish appropriate evidence, follow up, measure value, and continue.

Capabilities are treated as facts to test and record—not permanent assumptions based on an old model limitation or a copied warning. Each worker can have an evidence profile with observed, passing, failing, unavailable, and unknown capabilities. When a provider, model, tool, or permission changes materially, the relevant test should be rerun.

This keeps the system action-oriented without confusing operator authority, actual worker capability, available tooling, and a service’s own requirements. A genuine sign-in, 2FA, identity, payment, or other service-owned step becomes a precise handoff; the task and its evidence remain intact for resumption.

Read more in:

- [Operating Charter](docs/OPERATING_CHARTER.md)
- [Capability Evidence and Constraint Review](docs/CAPABILITY_EVIDENCE.md)
- [Constraint Evidence Map](docs/CONSTRAINT_EVIDENCE_MAP.md)

## Beyond the first workload

The Bitcoin proving ground is one job, not the entire mission. New work becomes a separate objective with a declared worker, observable state, bounded resources, evidence requirements, and a clear completion or escalation path.

Public-safe [work lanes](docs/WORK_LANES.md) make ongoing research, audits, and specialist work visible without falsely presenting a queued or review task as an active worker. The initial [revenue-lane research](docs/REVENUE_LANES.md) records source-backed opportunities that can later become properly scoped objectives.

## Run and verify locally

Run the dependency-free reliability suite from the repository root:

```text
python -m unittest discover -s tests -v
```

[`manager/config.example.json`](manager/config.example.json) is a safe synthetic example. Live worker settings and any service credentials stay in ignored local configuration; the [local runtime guide](docs/LOCAL_MANAGER.md) explains the supported configuration, restart behavior, telemetry contract, and public-update process.

## Documentation map

| Document | Purpose |
| --- | --- |
| [Operating Charter](docs/OPERATING_CHARTER.md) | Execution mandate, authority model, outreach, and value review. |
| [Local Manager Runtime](docs/LOCAL_MANAGER.md) | Lifecycle, reliability behavior, local configuration, recovery, and Windows restart behavior. |
| [Dashboard Guide](docs/GITHUB_PAGES.md) | Public dashboard, freshness, and local-to-Pages publishing flow. |
| [Work Lanes](docs/WORK_LANES.md) | Honest visible state for missions beyond the primary supervised worker. |
| [Capability Evidence](docs/CAPABILITY_EVIDENCE.md) | Evidence profiles, tests, and worker onboarding. |
| [Constraint Evidence Map](docs/CONSTRAINT_EVIDENCE_MAP.md) | How audit leads become testable engineering questions. |
| [Revenue-Lane Research](docs/REVENUE_LANES.md) | Initial source-backed opportunity research. |

## License

No open-source license has been selected. The source is publicly visible, but reuse rights have not been granted through a license.
