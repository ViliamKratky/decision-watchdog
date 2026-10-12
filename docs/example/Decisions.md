# Decision Watchdog - Engineering history

> Generated from an append-only local evidence log. Review before external publication.

## Decision

### Hooks only enqueue; analysis runs in a separate worker

- Date: 2026-09-19T20:12:00+02:00
- Source: claude
- Commit: `8761c50`
- Confidence: 0.93

Agent hooks write a small JSON file to a queue and exit. A LaunchAgent worker drains the queue and calls the local model.

Context: Codex and Claude Code give SessionEnd hooks a short timeout, and a local 30B model can take minutes per analysis.

Alternatives:
- Call Ollama synchronously from the hook
- Run analysis on demand from a CLI command only

Rationale:
- Hooks stay well inside the agent timeout
- A slow or stopped model can never stall an agent session

Trade-offs:
- Requires a background process and a durable queue
- Entries appear minutes after the turn, not instantly

Evidence:
- Commit 8761c50 adds queue_record() and the worker loop

## Problem

### Failed records were parked forever while Ollama was down

- Date: 2026-10-08T14:10:00+02:00
- Source: claude
- Commit: (uncommitted)
- Confidence: 0.88

Nine queue records failed with 'connection refused' while Ollama was stopped and were never retried, so their decisions were lost.

Context: The worker renamed a failing record to .failed and never looked at it again.

Alternatives:
- Delete failed records
- Retry on every poll with no limit

Rationale:
- Most failures are transient, so exponential backoff with an attempt cap recovers them without hammering the model

Trade-offs:
- A permanently broken record still needs a manual 'decision-watchdog retry' after 8 attempts

Evidence:
- worker-errors.log: Ollama analysis failed: Connection refused

## Constraint

### Nothing may be written inside a registered repository

- Date: 2026-09-19T22:15:00+02:00
- Source: claude
- Commit: `4ded1c5`
- Confidence: 0.9

Registration, state and history moved to an external registry under ~/Library/Application Support.

Context: Some registered repositories are shared with other contributors; a .portfolio/ directory or changed git status would leak into their work.

Alternatives:
- Keep a .portfolio/ directory in each repository and gitignore it

Rationale:
- git status stays clean
- No risk of committing captured prompts by accident

Trade-offs:
- History is not versioned together with the code

Evidence:
- RepositoryIsolationTests assert the repository is byte-for-byte unchanged
