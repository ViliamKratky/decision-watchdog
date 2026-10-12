# Setup tutorial

From zero to your first captured decision in about ten minutes. You need a Mac,
and at least one of Claude Code or Codex.

## 1. Install the prerequisites

```bash
python3 --version   # 3.10 or newer
git --version
```

Install [Ollama](https://ollama.com/download), start it, and pull a coding model.
Any model that follows instructions and supports structured JSON output works;
larger models extract noticeably better decisions.

```bash
ollama pull qwen3-coder:30b
ollama list
```

If your machine is short on memory, pick a smaller coding model instead. You can
switch later with `decision-watchdog config --model`.

## 2. Install Decision Watchdog

```bash
git clone https://github.com/ViliamKratky/decision-watchdog.git
cd decision-watchdog
python3 install.py
```

The installer copies the app to `~/Library/Application Support/Decision Watchdog/app`,
adds the `decision-watchdog` command to `~/.local/bin`, merges its hooks into
`~/.claude/settings.json` and `~/.codex/hooks.json` (with timestamped backups),
and starts a background LaunchAgent. It picks an installed coding model
automatically.

If the installer says `~/.local/bin` is not on your PATH, add the line it prints
to `~/.zshrc` and open a new terminal.

## 3. Check the installation

```bash
decision-watchdog doctor
```

Every line should say `OK`. The most common failures:

| Check fails | Fix |
| --- | --- |
| Ollama reachable | Start the Ollama app, or run `ollama serve` |
| Configured model installed | `decision-watchdog config --model "<name from ollama list>"` |
| LaunchAgent running | Re-run `python3 install.py` |

## 4. Trust the hooks in Codex (Codex users only)

Codex ignores new hooks until you approve them. Start `codex`, type `/hooks`,
review the three Decision Watchdog commands and trust them. Repeat this after
every reinstall. Claude Code needs no extra step.

## 5. Register a repository

Nothing is captured until you opt a repository in:

```bash
cd ~/code/my-project
decision-watchdog register --name "My Project"
decision-watchdog projects
```

`register` writes only to the external registry; `git status` in your
repository stays clean.

## 6. Work normally

Use Claude Code, Codex, your editor or plain Git as usual. Decision Watchdog
picks up:

- each finished agent turn (prompt plus final answer),
- every new commit,
- uncommitted changes, once you have stopped editing for five minutes.

To preserve something important explicitly:

```bash
decision-watchdog mark "Chose SQLite over Postgres: single-user app, no server to run"
```

## 7. Read the history

```bash
decision-watchdog status
```

`status` lists each project with its entry count and the queue state.
`decision-watchdog projects` shows where each project's data lives; open
`Decisions.md` and `Timeline.md` there. See [example output](example/Decisions.md).

When you have enough history, draft a case study:

```bash
cd ~/code/my-project
decision-watchdog generate
```

## 8. Optional: archive and Obsidian

```bash
decision-watchdog config --archive "$HOME/Documents/portfolio-archive"
decision-watchdog config --vault "$HOME/Obsidian/Developer OS"
```

The archive receives distilled evidence per project under
`projects/<slug>/private/watchdog/`; the vault receives the generated Markdown
under `10 Projects/<name>/`.

## Troubleshooting

**`status` shows failed records.** Usually Ollama was not running. While it is
down the queue simply pauses, and nothing is lost: records resume about two
minutes after Ollama is back. Records that failed for another reason retry
with growing delays (2 minutes up to 6 hours, 8 attempts). To skip the wait:

```bash
decision-watchdog retry
```

The reason for each failure is in
`~/Library/Application Support/Decision Watchdog/logs/worker-errors.log`.

**Nothing is captured from Codex.** Run `decision-watchdog self-test`; if it
reports the hooks as untrusted, repeat step 4.

**Too many trivial entries, or too few.** Raise or lower `min_confidence` in
`~/Library/Application Support/Decision Watchdog/config.json` (default `0.62`).

## Uninstall

```bash
decision-watchdog uninstall               # keeps captured history
decision-watchdog uninstall --remove-data # deletes it too
```
