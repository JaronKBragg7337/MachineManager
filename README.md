# Machine Manager

**Persistent autonomous system for teaching a computer how to operate itself.**

## Current mission

**Bitcoin Puzzle #71** is the current real-world workload being supervised by Machine Manager.

The active job is a continuous local GPU search using a KeyHunt-based worker. Machine Manager is being developed around this workload to prove persistent supervision, multi-signal health checks, failure detection, autonomous recovery, telemetry, and manager-vs-reference evaluation under real operating conditions.

The Bitcoin workload is the current proving ground, not the long-term limit of the project. The goal of Machine Manager is to become a general local runtime capable of supervising many different specialist workers and workloads.

**Current reward plan:** if the active Puzzle #71 workload succeeds, **2 BTC** is allocated to the designated service/developer payout wallet and the remaining recovered balance is directed to the configured public payout address below.

> The live dashboard is the source of truth for current machine state. This section documents the active mission and intended workload, not real-time process health.

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

## Current status (2026-08-19)

- Repository initialized
- Current mission documented as Bitcoin Puzzle #71
- Reference Scenario Suite started (healthy-operation + worker-death)
- Local worker supervision under multi-signal evaluation
- Secrets layout prepared (never committed)
- GitHub Pages control center deployed from `dashboard/`

## Payout address (public only)

`bc1qszpdhrmupcw9ncnjfy2v0v3k3t6t63g54yva9h`

This is a public receiving address only. Never store private keys or seed phrases in this repository.

## License

MIT (or as decided by owner)
