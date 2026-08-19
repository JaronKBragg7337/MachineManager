# Reference Scenario: Healthy Operation (healthy-operation-001)

**Version:** 1.0  
**Pass:** true  
**Score:** 1.0  
**Timestamp:** 2026-08-18T19:26:59

## Description
Baseline healthy continuous KeyHunt operation on Puzzle #71.

## Initial State
- KeyHunt PID 22756 running since 19:05:37
- GPU util 87%, 6202 MiB, 60°C, ~74 W
- Uptime ~21.4 minutes of continuous work

## Observations
Multi-signal confirmation: process present + high sustained GPU utilization + elevated power draw.

## Interpretation
This is the gold-standard healthy baseline. Real productive search is occurring.

## Action
Observe only (no intervention).

## Resulting State
Unchanged – continued healthy operation.

## Verification
GPU util >70%, KeyHunt process alive, manager reports active=True.

## Metrics
- Detection time: 0 s
- Recovery time: 0 s

## Artifacts
- C:\Users\lilli\MachineManager\traces\healthy-operation-001.json
- C:\Users\lilli\Puzzle71_Experiment\logs\manager.log
