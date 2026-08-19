# Machine Manager

**Persistent autonomous system for teaching a computer how to operate itself.**

Hierarchy:

```
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
- Local KeyHunt worker running under multi-signal supervision
- Secrets layout prepared (never committed)
- Dashboard foundation in progress

## Payout address (public only)

`bc1qszpdhrmupcw9ncnjfy2v0v3k3t6t63g54yva9h`

Never store private keys or seed phrases in this repository.

## License

MIT (or as decided by owner)
