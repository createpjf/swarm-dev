# HEARTBEAT.md — Alic

## Background Checks

Every evaluation cycle, Alic should verify:

### Evaluation Queue
- Unscored outputs count: if > 0, prioritize evaluation
- Time since last evaluation: if > 30min during active session, check pipeline

### Scoring Consistency
- Average score trend over last 10 evaluations
- Score variance: if too low (all 7-8), recalibrate
- Score distribution: ensure full range used appropriately

### Memory Write Health
- Last memory write success time
- Memory entry count growth rate
- Pattern extraction frequency

### Integration
- ContextBus publish success rate
- Leo reading evaluation results: verify consumption
