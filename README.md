# Machine Manager

**Persistent autonomous system for teaching a computer how to operate itself.**

## Live control center

The normal GitHub repository page is the project documentation and source tree. The visual machine-status interface is the GitHub Pages dashboard:

**https://jaronkbragg7337.github.io/MachineManager/**

The dashboard is designed to mirror the useful operational view from the MSI machine: current objective/job state, workers, GPU/VRAM/temperature/power, event stream, manager status, and scenario/evaluation results. It only displays sanitized public telemetry. If the machine has not published a fresh snapshot recently, the dashboard marks the data **STALE** instead of presenting an old snapshot as live.

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

## Current status (2026-08-18)

- Repository initialized
- Reference Scenario Suite started (healthy-operation + worker-death)
- Local worker supervision under multi-signal evaluation
- Secrets layout prepared (never committed)
- GitHub Pages control center deployed from `dashboard/`

## Payout address (public only)

`bc1qszpdhrmupcw9ncnjfy2v0v3k3t6t63g54yva9h`

Never store private keys or seed phrases in this repository.

## License

MIT (or as decided by owner)
