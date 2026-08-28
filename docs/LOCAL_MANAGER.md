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
