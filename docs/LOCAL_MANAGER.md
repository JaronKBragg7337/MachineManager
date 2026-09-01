# Local Manager Runtime

The `manager/` package is the repo-level implementation of the Machine Manager
boundary. It is intentionally standard-library-only so a local worker can run
without a cloud connector.

## Lifecycle

Jobs use these states:

```text
QUEUED -> STARTING -> VERIFYING -> RUNNING -> COMPLETE
                         |            |
                         v            v
                      STALLED      FAILED -> RETRYING
                                           |
                                           v
                                       ESCALATED
```

`CANCELLED` is available from any active state. A process is not considered
healthy merely because it exists. When configured, the supervisor requires:

1. the worker process to be alive;
2. a fresh heartbeat/progress file; and
3. a passing external resource probe, such as GPU utilization and power.

Retries are bounded by `max_restarts`. Once the budget is exhausted, the job
enters `ESCALATED` instead of restarting forever. A job may set
`retry_reset_after_s` (3600 seconds by default) to clear its old retry count
after that worker has remained continuously healthy for the configured
interval. The reset is recorded as a `retry_budget_reset` event; it does not
erase the earlier failure history or make repeated failures in the same stable
window disappear.

For NVIDIA workloads, the default resource decision uses utilization and power
when both are available, and tolerates a short zero-utilization sample when
dedicated GPU memory and active power still identify a working CUDA process.
An idle device with no worker memory remains unhealthy even if a driver power
sample is noisy.
The in-memory resource probe also retains a bounded five-observation summary:
the current driver sample remains visible, while recent maximum, average,
sample count, and zero-sample count make transient readings auditable without
turning a probe error into a fabricated zero.
The manager also records an allowlisted activity state and evidence basis beside
the raw sample. The public resource cards and Search Evidence view show that
interpretation and recent context when they add information beyond the current
sample.
Host CPU readings carry the same distinction: low load during confirmed GPU
work is recorded as GPU-worker offload, while a zero reading without active GPU
evidence remains host-counter idle.

## Synthetic worker

The deterministic worker is useful for reliability tests and does not touch
the Puzzle #71 workload:

```text
python -m unittest discover -s tests -v
```

The tests cover healthy operation, a live-but-stalled worker, repeated crash
and escalation, required event fields, and public telemetry allowlisting.

## Running a configured job

Copy `manager/config.example.json` to a local-only location and adjust the
worker command. Do not put API tokens, pool credentials, private keys, seed
phrases, or real secret-bearing command lines in the repository.

```text
python -m manager.run --config path/to/local-config.json \
  --status-file path/to/local-status.json \
  --dashboard-dir dashboard
```

The runner can publish compact dashboard files on each observation. The
publisher allowlists worker/job identifiers, states, numeric CPU/GPU metrics,
the GPU resource decision, and short event metadata. It can also publish aggregate worker progress such as
hashrate, tested work units, coverage, batch number, and observation uptime when
the worker reports them. It never copies raw logs, command lines, exception
text, process paths, candidate values, or credentials into `dashboard/data/`.

GPU health also uses the probe's bounded recent utilization window. A complete
zeroed driver sample can be tolerated only when at least two recent samples
contain meaningful utilization; the process and heartbeat checks still apply,
and the window expires as new idle samples arrive. Public telemetry labels this
case as `recent_driver_utilization` so a dashboard reader can distinguish it
from both a current driver reading and an unsupported activity claim.

The optional `progress_file` is a separate worker-owned JSON report. Only the
allowlisted numeric progress fields are read; it should not be the same file as
the liveness heartbeat on Windows because the worker may need to atomically
replace its heartbeat file. KeyHunt jobs with a configured `stdout_file` use a
bounded parser for its aggregate status line, so the public Search view can
show real rate, coverage, batch, and tested-count evidence without publishing
the raw stream.

For the current Puzzle #71 experiment, keep the existing local launch
configuration and separate experiment runtime outside this public repository
until the adapter has been reviewed. The live experiment is evidence for the
manager, not a source of secrets.

## Durable full-app runtime

The same runner is now the foundation for multiple workloads. A local config
can contain either the original single worker object or a jobs array. Each job
has its own objective, worker command, retry budget, progress contract, and
resource probe.

The runner stores local operational state in SQLite:

- job attempt and restart counters survive a manager restart;
- event history is retained locally with a bounded retention limit;
- queued work items are available for future research and specialist jobs;
- `WorkDispatcher` provides an explicit handler registry for future work. A
  registered handler can complete, retry, fail, or escalate a claimed task;
  unknown task kinds are returned to `QUEUED` with a later schedule rather than
  being lost. The protected KeyHunt assignment is intentionally outside this
  dispatch path. The runner can enable its empty-registry observation path with
  `queue_dispatch.enabled`; that path records claims and defers unhandled work
  until a real worker handler is registered.
- a singleton lock prevents two managers from supervising the same config;
- a worker PID lease allows a healthy worker to be adopted after a manager
  restart instead of spawning a duplicate.
- a local manager log records unexpected runtime exceptions for diagnosis;
  it is separate from the sanitized public telemetry files.

The agents array provides bounded specialist slots. The Ollama adapter sends
only a sanitized status context, requests bounded non-thinking JSON, disables
GPU layers by default, and falls back to continue for empty, malformed,
unavailable, or unsupported responses. The public agent timeline records the
bounded action, short reason, and model runtime (separate from manager poll
latency); it does not publish private prompts or hidden reasoning. Agent output
is advisory; the supervisor still owns process control, retry limits,
escalation, and secrets.

Agent review tasks are coordinator-owned. The generic queue dispatcher reserves
that task kind so it cannot mistake an in-flight review for unhandled future
work. If a manager restart leaves a review queued, the coordinator adopts that
same task instead of creating a duplicate. Completed review counts are rebuilt
from the durable task ledger on startup, so the public agent registry does not
reset its history when the manager process restarts.

The public snapshot also includes a bounded recent task ledger. It contains
only task id, allowlisted kind, objective id, state, attempt count, a sanitized
timestamp, and (when a lifecycle event supplied one) a short redacted result
summary or outcome. Task payloads and unrestricted model output remain local in
SQLite.

Registered task handlers may also return a short `public_message`. The dispatcher
redacts and length-limits that note before it is written to the lifecycle event,
so the public Research and Activity views can show a useful result summary while
keeping prompts, credentials, local paths, and unrestricted model output local.

The Operations capability list is built from the handlers loaded during startup.
Research, repository verification, durable queue dispatch, and authenticated
revenue discovery therefore appear as separate enabled or unavailable surfaces;
the dashboard does not claim that a worker exists merely because a feature is
documented for future use.

## Research worker

The optional `research_worker` configuration connects the durable `research`
task kind to a bounded public-source handler. It accepts a task question and a
short list of public HTTP(S) source URLs, limits response size and source count,
rejects local/private URLs, and writes one local evidence artifact per task.
With `mode: "ollama"`, the configured local model receives only the extracted
source excerpts and runs with GPU layers disabled. With `mode: "evidence_only"`,
the handler records source evidence without a model summary. The runner does
not enable this lane by default; enabling it requires both
`queue_dispatch.enabled` and `research_worker.enabled` in the local config.
Set `queue_dispatch.background` to true for network or model-backed handlers
so they execute outside the protected worker's health-sampling loop.

## Recurring objectives

`recurring_tasks` contains durable task templates. Each enabled template gets a
deterministic task id per interval, records its cursor in SQLite, and avoids
overlapping a prior `QUEUED` or `RUNNING` task. A restart resumes the same
queued task instead of creating a duplicate; missed intervals produce one
bounded catch-up task rather than a burst. Recurring scheduling is active only
when queue dispatch is enabled.

The optional `verification_worker` connects the `verification` task kind to a
fixed, shell-free `python -m unittest discover -s tests -q` run in a configured
repository root. It writes a small local evidence artifact and publishes only
the test count, pass state, and duration. A timed-out process or failed evidence
write is retried by the normal bounded dispatcher; a real test failure remains
visible as `FAILED` instead of being reported as success.

## Visible work lanes

The optional `workstreams` array is a public-safe ledger for missions beyond
the primary process-supervised job. A lane can follow a configured constraint
audit, bounded local agent, worker capability profile, or a deliberately
registered static milestone. The local manager persists the current observed
state and publishes only an allowlisted summary: title, owner label, state,
compact metrics, next action, and timestamp.

`RUNNING` is reserved for an observed active source. `REVIEW` means the source
has produced evidence that deserves a defined next test. `COMPLETE` is a
recorded bounded outcome, not a claim that a background worker is still active.
Details are in [`docs/WORK_LANES.md`](WORK_LANES.md).

## Public telemetry upload

Local publication and public publication are separate steps:

1. The local telemetry publisher writes sanitized JSON on every observation.
2. The GitHub Pages publisher can batch those files into a normal GitHub
   commit at a configured interval (two minutes by default).
3. GitHub Pages serves the compact files to anyone without a login.
4. The dashboard refreshes the public files and marks them stale after five
   minutes without a new snapshot.

On Windows, a short filesystem lock on a dashboard JSON file is retried with a
bounded delay. If a public snapshot still cannot be written, the failure is
recorded in local-only runtime diagnostics and its remote upload is skipped.
The supervisor and protected worker keep running; telemetry output is not
allowed to terminate the manager.

Enable the uploader only after creating a dedicated fine-grained GitHub token
with access limited to this repository's contents. Put the value in a local
ignored environment file referenced by env_file; never put it in JSON,
PowerShell arguments, source, logs, or a worker command line:

```text
MACHINE_MANAGER_GITHUB_TOKEN=github_pat_replace_locally
```

The uploader refuses prohibited fields, private paths, invalid JSON, and
oversized files before making a network request. Automated telemetry commits
include the required Codex attribution trailer. Without the token, the local
manager continues to work and the public page intentionally shows the last
known snapshot as stale.

## One-time operator resume after escalation

Repeated worker failure intentionally becomes `ESCALATED` instead of restarting
forever. When the operator explicitly approves one new recovery attempt, add a
local-only acknowledgement to the ignored runtime config:

```json
"operator_resume": {
  "id": "resume-20260831-001",
  "job_id": "job-p71-001"
}
```

On its next start, the manager consumes that acknowledgement only if the named
job is already `ESCALATED`. It preserves the historical attempt count, clears
only that job's retry budget, records an `operator_resume_authorized` event,
and will not consume the same identifier again. A different acknowledgement is
required for any later escalation. This is not a substitute for normal bounded
recovery or a way to erase prior failures.

## Windows restart behavior

Install a user-level Task Scheduler entry from a PowerShell session:

```text
powershell -ExecutionPolicy Bypass -File scripts/install_machine_manager.ps1 -RepoPath C:/path/to/MachineManager -ConfigPath C:/path/to/local-manager-config.json -PythonPath C:/path/to/python.exe
```

The task starts a small local watchdog at interactive logon. The watchdog
relaunches a failed manager child after a bounded delay; the manager lock and
worker PID lease prevent duplicate supervision and allow a healthy worker to be
adopted after a manager-process failure. Task Scheduler remains the outer
restart layer for the watchdog itself. The task allows battery operation and
keeps a local runtime error trail.

It does not make a powered-off or sleeping laptop into an always-on server.
True 24/7 operation requires an awake, connected machine or an always-on host
for the worker and/or public telemetry service.
