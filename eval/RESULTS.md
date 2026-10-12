# Extraction benchmark results

29 labelled synthetic scenarios: 15 should be recorded, 14 should not. 7 are
held out: they were not looked at while the prompt was designed. Model:
`qwen3-coder:30b-64k` at temperature 0.1, run with `eval/run_eval.py`.

| Prompt | Scenario precision | Entry precision | Recall | Negatives recorded | Type accuracy | Entries |
| --- | --- | --- | --- | --- | --- | --- |
| `legacy` (up to 0.4.0) | 0.52 | 0.29 | 1.00 | 14 of 14 | 0.94 | 62 |
| `current` (0.4.0) | 1.00 | 0.90 | 1.00 | 0 of 14 | 1.00 | 20 |

Held-out scenarios alone: `legacy` recorded 5 of 5 negatives with an entry
precision of 0.21; `current` recorded 0 of 5 and kept both positives.

## What changed

The legacy prompt recorded nearly everything, and gave each entry a confidence
of about 1.0. Its typical false positives were:

- answers to informational questions ("where is the data stored")
  recorded as decisions;
- status reports and review findings nobody had acted on yet;
- renames, formatting, dependency bumps and routine implementation;
- one idea split into several `decision` entries;
- rationale and alternatives taken from general knowledge rather than from
  the evidence. A manual review found invented reasons in 26 of 62 entries.

The current prompt lists what does *not* count, requires that a decision was
actually made, restricts rationale and alternatives to what the evidence
states, and asks for zero or one entry in most cases. In code, a `decision`
with neither a reason nor an alternative is dropped.

## Known limits

- Confidence is still not calibrated. Every threshold from 0.5 to 0.9 gives
  the same result, so `min_confidence` is a safety net rather than a filter.
- Cosmetic details inside a long answer can still leak through
  (`hard-buried-decision`), and the model occasionally adds a reason from
  general knowledge. In a manual review of an earlier run of this prompt, 6 of
  20 entries had such an addition.
- Deduplication of reworded entries relies on the prompt and the 25 most
  recent entries; the fingerprint only catches exact repeats.
- One model, one small corpus. Rerun with another model before switching:
  `python3 eval/run_eval.py --model <tag>`.
