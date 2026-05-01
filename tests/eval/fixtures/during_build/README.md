# during_build fixtures

Pulse-tier suites for S14 (`gecko_pulse`). Empty in S13 — the phase
primitive seam ships before the first consumer.

When S14 lands the first `pulse_holdout.json`, drop it here. The loader
in `tests/eval/runner.py` already routes `phase='during_build'` to this
directory.

Expected fixture shape (per `docs/strategy/sprint-13+-ai-ml-engineer-memo-2026-04-30.md`):

```json
{
  "id": "pulse-001",
  "weeks_of_context": 2,
  "expected_verdict": "stay-the-course",
  "..."
}
```
