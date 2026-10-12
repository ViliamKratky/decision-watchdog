# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-10-08

### Changed
- New extraction prompt, tuned on a labelled benchmark (`eval/`). The old
  prompt recorded informational answers, status reports and routine edits as
  decisions: 14 of 14 negative scenarios produced entries, versus 0 of 14
  now, with every real decision still captured. A `decision` with neither a
  reason nor an alternative is dropped. See `eval/RESULTS.md`.

### Fixed
- Records that failed analysis (for example because Ollama was not running)
  were parked as `.failed` and never retried, silently losing the decisions
  they carried. They are now retried with exponential backoff, from 2 minutes
  up to 6 hours, for up to 8 attempts.
- A record claimed by a worker that crashed mid-analysis stayed in
  `.processing` forever. Claims older than two hours are now returned to the
  queue. Replaying is safe because analysis is idempotent per record.

- Commits made during an agent turn were never analyzed: the turn's
  analysis advanced the commit scanner's baseline. The scanner now owns it,
  and queues only new commits by the configured Git user, so checking out an
  old branch or pulling teammates' work no longer counts as new work.
- A retried turn could be analyzed with a later prompt from the same session.
- Two analyzers (the worker and `mark`, or two workers) could drop each
  other's state and write duplicate entries. State updates are now locked
  and only one worker runs at a time.
- An Ollama outage no longer uses up retry attempts or blocks on every
  record; the queue pauses until Ollama is back.
- A failing archive or Obsidian export no longer re-runs the model.
- Malformed model entries are dropped one at a time instead of failing the
  whole record; bad `config.json` values fall back to defaults; torn JSONL
  lines, lone surrogates, payloads over 2 MB and non-ASCII file names are
  handled.

### Security
- Redaction covers many more formats (AWS, GCP, Stripe, GitHub fine-grained
  tokens, JWTs, PGP keys, credentials in URLs, `KEY=value` forms including
  diff lines) and runs before clipping, so a clip cannot cut a key in half.
- Credential files (`.env*`, `credentials*`, `.npmrc`, `kubeconfig`,
  `*.tfstate`, keys and more) are left out of diffs as well as untracked
  excerpts, and untracked symlinks are never followed.
- Prompts are redacted before they are queued, and model output is redacted
  before it is stored. Images and HTML in model output are rendered as text.
- Credentials in git remote URLs are never stored.
- The Ollama client ignores HTTP proxy settings and refuses a non-loopback
  `ollama_url`.
- Reading a repository no longer rewrites its `.git/index`.
- Retention also removes failed records and cached prompts.
- `mark`, `analyze` and `generate` refuse unregistered repositories instead
  of registering them silently.

### Added
- `decision-watchdog retry` requeues every failed record immediately,
  including ones past the attempt limit.
- `decision-watchdog status` reports waiting, in-progress and failed records.
- Extraction benchmark: 29 labelled scenarios and a harness in `eval/`.
- MIT license, CI on macOS for Python 3.10-3.13, setup tutorial, contribution
  and security notes.

## [0.3.0] - 2026-09-19

### Added
- External repository registry: nothing is written inside a registered
  repository.
- Export of distilled evidence to a central portfolio archive.
- Debounced working-tree monitoring and cross-source deduplication.

## [0.1.0] - 2026-09-19

### Added
- Initial MVP: Claude Code and Codex hooks, background LaunchAgent worker,
  local Ollama extraction, `history.jsonl`, generated `Decisions.md` and
  `Timeline.md`, Obsidian mirroring.
