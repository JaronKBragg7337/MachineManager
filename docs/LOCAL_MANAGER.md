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
enters `ESCALATED` instead of restarting forever.

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
publisher allowlists worker/job identifiers, states, numeric GPU metrics, and
short event metadata. It never copies raw logs, command lines, exception text,
process paths, or credentials into `dashboard/data/`.

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
- a singleton lock prevents two managers from supervising the same config;
- a worker PID lease allows a healthy worker to be adopted after a manager
  restart instead of spawning a duplicate.
- a local manager log records unexpected runtime exceptions for diagnosis;
  it is separate from the sanitized public telemetry files.

The agents array provides bounded specialist slots. The Ollama adapter sends
only a sanitized status context, requests strict JSON, disables GPU layers by
default, and falls back to continue for empty, malformed, unavailable, or
unsupported responses. Agent output is advisory; the supervisor still owns
process control, retry limits, escalation, and secrets.

## Public telemetry upload

Local publication and public publication are separate steps:

1. The local telemetry publisher writes sanitized JSON on every observation.
2. The GitHub Pages publisher can batch those files into a normal GitHub
   commit at a configured interval.
3. GitHub Pages serves the compact files to anyone without a login.
4. The dashboard refreshes the public files and marks them stale after five
   minutes without a new snapshot.

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

## Windows restart behavior

Install a user-level Task Scheduler entry from a PowerShell session:

```text
powershell -ExecutionPolicy Bypass -File scripts/install_machine_manager.ps1 -RepoPath C:/path/to/MachineManager -ConfigPath C:/path/to/local-manager-config.json -PythonPath C:/path/to/python.exe
```

The task starts at interactive logon, restarts the manager after failure, and
keeps a local `var/manager.log` error trail,
allows battery operation, and ignores duplicate instances through the manager
lock. It does not make a powered-off or sleeping laptop into an always-on
server. True 24/7 operation requires an awake, connected machine or an
always-on host for the worker and/or public telemetry service.
