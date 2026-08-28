# Machine Manager

**Persistent autonomous system for teaching a computer how to operate itself.**

## Current mission

**Bitcoin Puzzle #71** is the current real-world workload being supervised by Machine Manager.

The active job is a continuous local GPU search using a KeyHunt-based worker. Machine Manager is being developed around this workload to prove persistent supervision, multi-signal health checks, failure detection, autonomous recovery, telemetry, and manager-vs-reference evaluation under real operating conditions.

The Bitcoin workload is the current proving ground, not the long-term limit of the project. The goal of Machine Manager is to become a general local runtime capable of supervising many different specialist workers and workloads.

**Current reward plan:** if the active Puzzle #71 workload succeeds, **2 BTC** is allocated to the designated service/developer payout wallet and the remaining recovered balance is directed to the configured public payout address below.

> The dashboard displays the latest published sanitized snapshot. If the local
> publisher is not running, it shows the last snapshot and marks it stale.
> This section documents the active mission and intended workload, not
> real-time process health.

## Live control center

The GitHub Pages root forwards directly to the visual Machine Manager control center.

Dashboard:

`https://jaronkbragg7337.github.io/MachineManager/`

The repository page remains the source/documentation view. The Pages site is the operational dashboard.

## Hierarchy

```text
Jaron (human)
  ↓
Grok (executive reasoning / CEO)
  ↓
Machine Manager (local persistent manager)
  ↓
Job scheduler + Evaluator + Capability registry
  ↓
Specialist Workers
```

## Core ideas

- Multi-signal health evaluation (process + resource activity + progress + logs)
- Reference Scenario Suite: Grok performs competent behavior → local Manager is evaluated against it
- Shared event/telemetry pipeline
- Visual control center (GitHub Pages dashboard)
- Workers are dynamically registerable; each can have its own minimal GitHub identity
- Grok remains the escalation and teaching layer

## Local manager runtime

The repository now includes a dependency-free Python runtime under `manager/`.
It provides a job registry, explicit lifecycle states, multi-signal worker
health checks, bounded retries, escalation, and an atomic public telemetry
publisher. The synthetic worker and tests cover healthy operation, live-but-stalled
false liveness, crashes, retry exhaustion, and the event contract.

Run the local checks from the repository root:

```text
python -m unittest discover -s tests -v
```

`manager/config.example.json` is a safe synthetic example. Keep real worker
commands, credentials, targets, and private machine paths in a local config
outside the repository. See [`docs/LOCAL_MANAGER.md`](docs/LOCAL_MANAGER.md)
for the configuration and telemetry contract.

## Current status (2026-08-28)

- Repository initialized
- Current mission documented as Bitcoin Puzzle #71
- Reference Scenario Suite started (healthy-operation + worker-death)
- Reusable local supervisor runtime and synthetic reliability tests added
- Sanitized telemetry publisher added for dashboard data files
- `worker-death-manager-002` passed with zero CEO intervention
- Local worker supervision under multi-signal evaluation
- Secrets layout prepared (never committed)
- GitHub Pages control center deployed from `dashboard/`

## Payout address (public only)

`bc1qszpdhrmupcw9ncnjfy2v0v3k3t6t63g54yva9h`

This is a public receiving address only. Never store private keys or seed phrases in this repository.

## License

MIT (or as decided by owner)
