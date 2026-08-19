# Reference Scenario: Worker Death & Recovery (worker-death-001)

**Version:** 1.0  
**Pass:** true  
**Score:** 1.0  
**Timestamp:** 2026-08-18T19:27:18

## Description
Controlled termination of KeyHunt followed by detection and clean restart.

## Initial State
Healthy KeyHunt (PID 22756), GPU 86%, 59°C, 83 W.

## Observations
- After Stop-Process: GPU util collapsed to 0%, power dropped, process gone.
- Clear death signal (process absence + util collapse).

## Interpretation
Worker is dead. Competent manager must restart with the same proven command line.

## Action
1. Kill KeyHunt
2. Relaunch with gold-standard arguments (range, -r 2000, address, etc.)

## Resulting State
New PID 28004, GPU recovered to 82%, 62°C within 15.3 seconds.

## Verification
New process present AND GPU util >50%.

## Metrics
- Detection time: ~0.1 s (immediate)
- Full recovery time: 15.3 s
- GPU util after: 82%

## Artifacts
- C:\Users\lilli\MachineManager\traces\worker-death-001.json
