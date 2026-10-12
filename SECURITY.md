# Security and privacy

Decision Watchdog reads prompts, assistant replies and Git diffs from your
machine, so its privacy guarantees are part of its contract:

- The only network call is to Ollama on `127.0.0.1`. No cloud API is used.
- Full agent transcripts are never read; only the current prompt, the final
  assistant message, Git metadata and a capped diff reach the model.
- Untracked files that look like secrets (`.env*`, private keys, certificates,
  `secrets.*`) are never read, and private keys, common token formats and
  `password=` / `api_key:` style assignments are redacted from everything sent
  to the model.
- Nothing is written inside a registered repository.

These properties are covered by tests (`PrivacyTests`, `SensitivePathTests`,
`RepositoryIsolationTests`).

## Reporting a vulnerability

If you find a way for captured data to leave the machine, reach the model
unredacted, or be written into a repository, please do not open a public issue.
Use GitHub's private vulnerability reporting on this repository instead
(Security tab, "Report a vulnerability"). You can expect a reply within a week.
