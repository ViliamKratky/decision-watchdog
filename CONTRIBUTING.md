# Contributing

Thanks for your interest. Decision Watchdog is a small, dependency-free tool,
and contributions that keep it that way are the easiest to accept.

## Development setup

```bash
git clone https://github.com/ViliamKratky/decision-watchdog.git
cd decision-watchdog
python3 -m unittest -v
```

The tests never touch your real installation: each one points the app at a
throwaway home directory and a throwaway Git repository. You do not need
Ollama to run them; the model is mocked.

To try a change against your real setup, re-run `python3 install.py`. Note that
reinstalling changes the hook command text, so Codex asks you to trust the hooks
again (`/hooks` in the Codex TUI).

## Changing the extraction prompt

Unit tests cannot tell whether the model records the right things. That is
what the benchmark in `eval/` is for: 29 labelled synthetic scenarios (real
decisions, constraints and bugs, plus negatives such as renames, status
reports and informational answers, some in Slovak). With Ollama running:

```bash
python3 eval/run_eval.py --model qwen3-coder:30b-64k            # the current prompt
python3 eval/run_eval.py --model qwen3-coder:30b-64k --variant legacy
python3 eval/compare.py eval/results/<run-a> eval/results/<run-b>
```

Add a prompt idea as a variant in `eval/variants.py` and compare it with
`current` before changing `SYSTEM_PROMPT`. Report the held-out scenarios
separately; they guard against tuning the prompt to the benchmark.

## Ground rules

- Standard library only. No runtime dependencies.
- Never write inside a registered repository, and never add a network call to
  anything other than the local Ollama endpoint. Both are enforced by tests.
- Hooks must stay fast: they only write a queue file and never call a model.
- Add a test for every behaviour change, and a line to `CHANGELOG.md`.
