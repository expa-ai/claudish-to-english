# claudish-to-english

<p align="center">
  <img
    src="https://github.com/gvzdv/claudish-to-english/releases/download/assets/comparison.png"
    width="820"
    alt="Side-by-side comparison: a dense, jargon-heavy Claude message labeled 'Claudish' on the left, and its plain-English rewrite on the right">
</p>

A Claude Code plugin that shows a **plain-English rewrite** of each assistant
message, produced by a **local LLM via ollama**. It is **display-only**: Claude's
own reasoning and the saved transcript keep the original text — only what you
read on screen changes.

An optional second hook rewrites **Markdown files** into plain English when they
are written or edited (opt-in, off by default).

> Status: working prototype. Every hook fails **open** — if anything goes wrong
> (ollama down, timeout, missing dependency), you simply see Claude's original
> text. The plugin can never swallow or corrupt an answer.

---

> **Setting this up on your machine? Read [SETUP.md](SETUP.md) instead.**
> This README explains how the plugin works and what we changed. SETUP.md is the
> step-by-step guide for the team.

## This is a fork

Forked from [gvzdv/claudish-to-english](https://github.com/gvzdv/claudish-to-english)
(MIT, © Mike Gvozdev). It does not track upstream automatically — changes are
reviewed before they land. What differs:

| Change | Why |
|---|---|
| **New `rewrite-gh.py`** (`PreToolUse`) rewrites GitHub issue/PR bodies before they post | Our team communicates with humans through GitHub issues; the text that needs to be readable is what lands in the issue, not what scrolls past in the terminal |
| **`CLAUDISH_DISPLAY_MODE`** can gate the display hook to GitHub work | Kept as an option, but we run the default (`always`). See the note below on why gating turned out to be the wrong idea |
| **`gh` wrapper** in bash and Python, so the rewrite no longer depends on how Claude wrote the command | A `CLAUDE.md` rule is a request, not a guarantee; the wrapper runs after the shell, where there is nothing left to parse |
| Works against **LM Studio**, not just ollama | Via a translating shim; see [Backends](#backends) |
| Truncation guard on Markdown rewrites | A model that stops early returns plenty of bytes; without a length check, `overwrite` mode replaces real content with a partial document |
| Symlink guard on `CLAUDISH_MD_DIR` | The containment check resolves the parent directory but not the basename, so a symlink inside the directory could point anywhere on disk |
| `session_id` / `message_id` sanitised before use as paths | They reach `rm -rf` as path components; `index` was already validated, these were not |

The `rewrite-gh.py` hook is the one thing here that is **not** display-only — it
changes what your teammates read. It fails open on any doubt, but treat it with
more care than the rest.

---

## Requirements (read this first)

This plugin shells out to a **local** model. Nothing works until these are in place:

| Requirement | Why | Install |
|---|---|---|
| **ollama**, running | Does the rewriting, locally | `brew install ollama` then `ollama serve` |
| A pulled model | The actual rewriter | `ollama pull gemma4:26b-mlx` (~17 GB; choose the model that fits into your memory) |
| `jq` | Parses hook JSON | ships with macOS; else `brew install jq` |
| `curl` | Talks to ollama | ships with macOS |

Warm the model once after `ollama serve` (the first call is a slow cold load):

```bash
ollama run gemma4:26b-mlx "hi"
```

**If the local model isn't ready, the plugin does nothing to your text** —
Claude's output shows normally, unchanged. That is by design, not a bug. It skips
(fails open) when ollama is down, the request times out, or the model isn't
pulled. The first time that happens in a session it tells you why: the display
hook appends a one-line notice on screen, and the Markdown hook shows a
`systemMessage`. So a silent skip is never a mystery (once per session; set
`CLAUDISH_NOTICE=0` to silence it).

**Pick a model you actually have.** The default is `gemma4:26b-mlx`. Pull it (as
above), or pull a smaller/faster model and point the plugin at it by setting
`CLAUDISH_MODEL` to that model's exact ollama tag in your `env` (see
[Configuring the plugin](#configuring-the-plugin)). If `CLAUDISH_MODEL` names a
model you have not pulled, every rewrite is skipped — with the one-time notice
above.

---

## Install

Directly from this repository (also serves its own marketplace):

```shell
/plugin marketplace add gvzdv/claudish-to-english
/plugin install claudish-to-english@gvzdv-plugins
```

After review by the Anthropic team, the plugin will be available to install from the community marketplace:

```shell
/plugin marketplace add anthropics/claude-plugins-community
/plugin install claudish-to-english@claude-community
```

If the install summary says `Run /reload-plugins to activate.`, run that command.

**Try before installing** (loads it for one session, no install):

```bash
claude --plugin-dir /path/to/claudish-to-english
```

Run `/reload-plugins` after edits; if it doesn't load, check the `/plugin`
**Errors** tab.

---

## Configuring the plugin

All behavior is controlled by `CLAUDISH_*` environment variables (full list in
[Configuration](#configuration-env-vars) below). When you install from a
marketplace, set them in Claude Code's **`env` block in `settings.json`** — do
**not** edit the plugin's own `hooks/hooks.json`, which lives in the read-only
plugin cache (`~/.claude/plugins/cache/…`) and is overwritten on every update.

For a personal, all-projects setup, use `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDISH_MODEL": "gemma4:26b-mlx",
    "CLAUDISH_MODE": "append"
  }
}
```

The hooks are subprocesses Claude Code spawns, so they inherit these. A few
things to know:

- **Restart Claude Code after editing `env`.** The value is captured at launch,
  so a running session keeps the old one.
- **`env` does not merge across scopes.** The highest-precedence settings file
  that defines `env` supplies the *entire* block — it isn't combined with lower
  scopes. Precedence: managed → local → project → user. Keep all your
  `CLAUDISH_*` vars in whichever file wins.
- **Scopes:** `~/.claude/settings.json` (all your projects) ·
  `.claude/settings.json` (shared with a repo, checked in) ·
  `.claude/settings.local.json` (just you, just this repo).

Quick one-off without editing a file — hooks inherit the launching shell:

```bash
CLAUDISH_MODEL=llama3.2:3b claude
```

To confirm the hook is firing, set `CLAUDISH_DEBUG=1` and watch
`"$TMPDIR"/claudish-to-english/debug.log`.

---

## How the display hook works

Claude Code fires the `MessageDisplay` event **once per streamed chunk**, not
once per message. Each fire is a separate process carrying `message_id`,
`index`, a `final` flag, and this chunk's `delta` (a text fragment, not the
whole message). So the hook **buffers every delta** to a temp file (keyed by
`message_id`) and only calls the model on the **final** chunk, once the whole
message is known:

```
chunk 0 (final:false) ─┐
chunk 1 (final:false) ─┤ append each delta to $TMPDIR/claudish-to-english/<session>/<message>/<index>.part
chunk 2 (final:false) ─┘  → emit nothing (append) or "" (replace)
chunk 3 (final:true)  ──► reconstruct full message → call ollama once → show the rewrite
                          → delete the buffer
```

On that final chunk it also reads the **original user question** from the
transcript and passes it to the model as **context only** — to keep the rewrite
on-topic. The model is told never to answer or repeat the question; it only
rewrites the assistant's message.

### Display modes

| `CLAUDISH_MODE` | On screen | Notes |
|---|---|---|
| `append` (default) | Original streams normally, then a `💬 In plain English:` block is appended. | Safest. No streaming loss; if the LLM fails you just don't get the extra block. |
| `replace` | Only the simplified version (original chunks suppressed while streaming). | Experimental. Appears all at once after LLM latency; on failure it re-shows the full original. |

---

## Markdown file rewrite (optional second hook)

A `PostToolUse` hook (`rewrite-md.sh`) rewrites Markdown **files** into plain
English when they are written or edited. Unlike the display hook, this changes
bytes on disk.

**Opt-in by directory.** It does nothing unless `CLAUDISH_MD_DIR` is set, and it
only touches `*.md` files whose resolved path is inside that directory. Every
other `README`, `CLAUDE.md`, or doc you edit is left alone.

| `CLAUDISH_MD_MODE` | Result | Notes |
|---|---|---|
| `sibling` (default) | Writes `NAME.plain.md` next to `NAME.md`. | Non-destructive; the original is never touched. |
| `overwrite` | Replaces `NAME.md` in place. | Adds a `<!-- claudish-to-english:rewritten -->` marker so a re-write is skipped (idempotent). A weak model can degrade real docs — use with care. |

In both modes: YAML frontmatter is split off and re-attached **verbatim**, fenced
code is left to the model instruction, short files are skipped, and the write is
atomic. Fail-open here means the file is left **exactly as the agent wrote it**.

**Large files are slow.** `gemma4:26b-mlx` (the default) rewrites at roughly 60
tokens/s, so a long plan or spec can take 30–120s. This hook allows up to
`CLAUDISH_MD_TIMEOUT` (150s) inside a 180s `PostToolUse` hook budget; if a rewrite
still times out you get the one-time notice above — raise those limits, or set
`CLAUDISH_MODEL` to a smaller model.

Enable it for one directory, in sibling mode (the safe default), the same way
as every other setting — the `env` block of your `settings.json`:

```json
{
  "env": {
    "CLAUDISH_MD_DIR": "/ABS/PATH/docs/plain",
    "CLAUDISH_MD_MODE": "sibling"
  }
}
```

In `overwrite` mode the marker comment is written **after** any YAML
frontmatter, so the frontmatter stays on line 1 where parsers expect it.

---

## GitHub issue rewrite (`rewrite-gh.py`)

Fires on `PreToolUse` for `Bash`. When Claude runs a `gh issue` or `gh pr`
write, the body is rewritten into plain English **before it posts**.

This hook is written in Python rather than shell on purpose: it has to parse and
rebuild a command that posts to GitHub, and `shlex` gets quoting right where a
regex in bash is exactly where injection bugs live.

### Two shapes, not equally trusted

**`--body-file PATH`** — the file is rewritten in place and the command is left
completely alone. No shell string is rebuilt, so there is nothing to mis-quote.
**Prefer this.**

**`--body TEXT`** — the command must be rebuilt. Tokenised with `shlex`, the body
token replaced, then re-quoted with `shlex.quote`. The rebuilt command is checked
to re-parse into identical tokens before it is accepted.

### What passes through untouched

Anything the hook cannot fully understand, because a hook that posts to GitHub
must never garble what it only half-parsed:

- **heredocs** — `shlex` cannot round-trip them
- **`$(...)` or backticks where the shell would act on them.** Inside single
  quotes these are literal text, and Markdown bodies are full of backticks, so
  the check tracks quote state rather than scanning the raw string
- pipes, redirects, and `&&`/`;` chains
- bodies shorter than `CLAUDISH_GH_MIN_CHARS`
- rewrites whose prose falls below `CLAUDISH_GH_MIN_RATIO` of the original

### Making it actually fire

Heredocs are skipped, and a heredoc is how Claude usually writes a multi-line
issue body — so on its own this hook quietly does nothing most of the time.

A `CLAUDE.md` rule can push Claude toward `--body-file`:

> When posting GitHub issue or PR bodies, always write the body to a file and use
> `--body-file`. Never use a heredoc or an inline `--body` for multi-line text.

But that is a request, not a guarantee — Claude can ignore it. **For a reliable
setup, use the [gh wrapper](#the-gh-wrapper) instead and turn this hook off with
`CLAUDISH_GH_ENABLED=0`.** Keep the hook only where you cannot put a wrapper on
PATH.

### Try it safely first

```sh
CLAUDISH_GH_DRYRUN=1
```

Logs what it *would* have posted to `$TMPDIR/claudish-to-english/debug-gh.log`
and posts the original unchanged. Worth running for a day before letting it write
to real issues.

### Config

| Var | Default | Meaning |
|---|---|---|
| `CLAUDISH_GH_ENABLED` | `1` | master switch for this hook |
| `CLAUDISH_GH_TIMEOUT` | `45` | LLM timeout; keep below the hook timeout in `hooks.json` |
| `CLAUDISH_GH_MIN_CHARS` | `200` | skip bodies shorter than this |
| `CLAUDISH_GH_MIN_RATIO` | `0.5` | reject rewrites that lose more prose than this |
| `CLAUDISH_GH_DRYRUN` | `0` | log the rewrite, post the original |
| `CLAUDISH_GH_COMMANDS` | `\bgh\s+(issue\|pr)\s+(create\|comment\|edit)\b` | which commands qualify |

Requires `python3` on PATH. On Windows that is often `python`, so the hook
command in `hooks/hooks.json` may need adjusting.

---

## The gh wrapper

`gh-wrapper.sh` is the reliable way to rewrite issue text. Install it as `gh`
somewhere earlier on your PATH than the real `gh`.

**Why it beats the hook.** The hook inspects the command *before* the shell runs
it, so it has to cope with heredocs, `$(...)` and quoting — and refuses whatever
it cannot rebuild safely. The wrapper runs *after* the shell has finished. By
then the heredoc is expanded, the quotes are gone, and the finished text arrives
as ordinary arguments. There is nothing left to parse, so nothing left to get
wrong. It does not matter how Claude wrote the command.

It rewrites the body and passes it to the real `gh` via `--body-file`, using a
temp file. If you supplied your own `--body-file`, **your file is never
modified**.

There are two implementations with identical behaviour and identical config.
Pick one per machine:

| File | For | Needs |
|---|---|---|
| `gh-wrapper.sh` | macOS, Linux, Git Bash | `jq`, `curl` |
| `gh-wrapper.py` + `gh.cmd` | Windows (cmd, PowerShell) | Python only |

### Install — macOS / Linux

```sh
mkdir -p ~/.local/bin
cp gh-wrapper.sh ~/.local/bin/gh
chmod +x ~/.local/bin/gh
which gh          # must print ~/.local/bin/gh, not the real one
gh --version      # must still work
```

If `which gh` still shows the real one, put `~/.local/bin` earlier on your PATH.

### Install — Windows

Copy **both** files into a directory that comes before the real `gh` on PATH,
keeping them together — `gh.cmd` looks for `gh-wrapper.py` beside itself.

```bat
mkdir "%USERPROFILE%\.local\bin"
copy gh-wrapper.py "%USERPROFILE%\.local\bin\"
copy gh.cmd        "%USERPROFILE%\.local\bin\"
```

Add that directory to PATH ahead of the real `gh`, then check:

```bat
where gh          :: gh.cmd must be listed FIRST
gh --version      :: must still work
```

`gh.cmd` finds Python via `py`, `python`, or `python3`. If none is present it
falls back to the real `gh`, so your commands keep working either way.

Git Bash resolves `gh` (the bash script) rather than `gh.cmd`, so on a Windows
box that uses Git Bash you can install `gh-wrapper.sh` as well — they coexist.

### Then turn off the hook

Otherwise the text is rewritten twice:

```json
{ "env": { "CLAUDISH_GH_ENABLED": "0" } }
```

### Config

| Var | Default | Meaning |
|---|---|---|
| `CLAUDISH_GH_WRAPPER` | `1` | master switch |
| `CLAUDISH_GH_ONLY_CC` | `1` | only rewrite inside Claude Code. Set `0` to also rewrite `gh` commands you type yourself |
| `CLAUDISH_GH_REAL` | auto | path to the real `gh`, if it is somewhere unusual |
| `CLAUDISH_GH_TIMEOUT` | `45` | rewrite timeout |
| `CLAUDISH_GH_MIN_CHARS` | `200` | skip bodies shorter than this |
| `CLAUDISH_GH_MIN_PCT` | `50` | reject rewrites below this percentage of the original |
| `CLAUDISH_GH_DRYRUN` | `0` | log what would change, post the original |

`touch ~/.claude/claudish-off` disables it instantly, same as the other hooks.

### It always fails open

Rewriter down, timed out, empty answer, truncated answer, no `jq` — the original
command runs unchanged. The wrapper can never stop you posting, and never posts
anything it did not fully produce. Failures print one line to stderr and post the
original.

Everything else — `gh pr list`, `gh repo view`, any command without a body — is
handed straight to the real `gh` untouched.

---

## Gating the display hook

`CLAUDISH_DISPLAY_MODE` controls **when** the display hook rewrites at all:

| Value | Behaviour |
|---|---|
| `always` (default) | upstream behaviour — every assistant message |
| `gh` | only while the session is writing to GitHub issues/PRs |

In `gh` mode the decision is made before the replace-mode branch, so a gated-off
message streams normally instead of being blanked and then restored. The verdict
is cached per message, so the transcript scan runs once rather than once per
streamed chunk.

> **We run `always`, and suggest you do too.** The two hooks serve different
> readers: `rewrite-gh.py` serves your teammates, who did not write the code,
> while the display hook serves you. Gating the display hook to GitHub work
> fires it exactly when `rewrite-gh.py` is already rewriting that same text —
> so it spends time explaining your own words back to you, and leaves every
> other message unrewritten. If the added latency bothers you, turn the display
> hook off entirely rather than switching to `gh`.
>
> To pause mid-session without restarting: `touch ~/.claude/claudish-off`.

| Var | Default | Meaning |
|---|---|---|
| `CLAUDISH_DISPLAY_MODE` | `always` | `always` or `gh` |
| `CLAUDISH_GH_LOOKBACK` | `60` | transcript lines scanned |
| `CLAUDISH_GH_PATTERN` | `gh +(issue\|pr) +(create\|comment\|edit)` | what counts as GitHub work |

---

## Backends

Both hooks POST to `$CLAUDISH_OLLAMA/api/chat` and read `.message.content` —
ollama's shape.

**ollama** works out of the box.

**LM Studio** does not: it serves `/v1/chat/completions` and returns
`.choices[0].message.content`. Run a translating shim in front of it and point
`CLAUDISH_OLLAMA` at the shim. Ours lives at `deploy/claudish-shim/` on the host
that runs LM Studio and is reached over Tailscale, which also lets several
machines share one model.

**Use a non-reasoning model.** A reasoning model spends thousands of tokens
thinking before it answers; measured on one realistic message, a 35B reasoning
model took 61.8s against Claude Code's 60s hook ceiling — every rewrite is killed,
and because `curl` is killed rather than returning an error, you usually get no
diagnostic at all. A small instruct model does the same job in ~13s.

---

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `CLAUDISH_ENABLED` | `1` | Master switch. `0` = pass everything through. Read once at session start. |
| `CLAUDISH_OFF_FILE` | `~/.claude/claudish-off` | Runtime kill switch. While this file exists, rewrites pause — re-checked every message, so unlike env vars it works mid-session. See [Toggling mid-session](#toggling-mid-session). |
| `CLAUDISH_MODE` | `append` | `append` or `replace` (display hook). |
| `CLAUDISH_MODEL` | `gemma4:26b-mlx` | ollama model name. |
| `CLAUDISH_OLLAMA` | `http://localhost:11434` | ollama base URL. |
| `CLAUDISH_MIN_CHARS` | `200` | Skip messages/files whose prose (code stripped) is shorter than this. |
| `CLAUDISH_STUB` | `0` | `1` = deterministic stub instead of the model (for testing display mechanics). |
| `CLAUDISH_TIMEOUT` | `45` | LLM client timeout for the **display** hook (seconds). Keep it below that hook's `timeout` (60s). |
| `CLAUDISH_MD_TIMEOUT` | `150` | LLM client timeout for the **Markdown file** hook (seconds). Higher on purpose — a large model rewriting a long doc is slow. Keep it below the `PostToolUse` hook `timeout` (180s). |
| `CLAUDISH_DEBUG` | `0` | `1` = write a debug log to `$TMPDIR/claudish-to-english/`. |
| `CLAUDISH_NOTICE` | `1` | `1` = show a one-time, once-per-session notice when a rewrite is skipped because ollama is unreachable, the call timed out, or the model isn't pulled (display hook appends it on screen; Markdown hook uses a `systemMessage`). `0` = stay fully silent (pure fail-open). |
| `CLAUDISH_MD_DIR` | *(unset)* | **Markdown hook opt-in.** Only `*.md` under this directory is rewritten. Unset = the Markdown hook does nothing. |
| `CLAUDISH_MD_MODE` | `sibling` | `sibling` (`NAME.plain.md`) or `overwrite` (in place). |
| `CLAUDISH_MD_SUFFIX` | `plain` | Sibling infix: `NAME.<suffix>.md`. |

In `hooks/hooks.json` the display hook (`MessageDisplay`) has a 60s `timeout` and
the Markdown hook (`PostToolUse`) has a 180s `timeout` — the file hook is higher
because a large model rewriting a long document can take a couple of minutes.
`CLAUDISH_TIMEOUT` and `CLAUDISH_MD_TIMEOUT` keep the LLM call itself bounded
below those ceilings, so it fails open cleanly instead of being killed mid-write.

**Quick kill switch:** set `CLAUDISH_ENABLED=0` or disable the plugin (both apply
only from the next session start), or `touch ~/.claude/claudish-off` to pause a
session that's already running — see [Toggling mid-session](#toggling-mid-session)
below.

### Toggling mid-session

`CLAUDISH_ENABLED` and the other env vars are read once, when a session launches,
so they can't pause rewrites in a session that's already running. For that, both
hooks also check a **flag file** on every invocation — each fire is a fresh
process, so the check is always live:

```bash
touch ~/.claude/claudish-off   # pause rewrites, effective on the next message
rm    ~/.claude/claudish-off   # resume
```

You create and remove this file yourself; nothing creates it on install, and its
absence is the normal "on" state. While it exists, `ENABLED` is forced to `0` and
the fail-open path leaves Claude's original text untouched. Point a hotkey at a
two-line toggle script to flip rewrites from the keyboard across all running
sessions at once. Override the path with `CLAUDISH_OFF_FILE`.

### Reasoning models

The request sends `"think": false`. Models with a hidden reasoning phase
otherwise spend most of their time generating reasoning tokens you never see —
much slower for identical output quality on this simple task. Keep it off.

---

## Privacy / egress

The rewriter runs **entirely locally** against ollama, so **no conversation
content leaves your machine**. If you ever point `CLAUDISH_OLLAMA` at a
remote/hosted endpoint, that context (which can include file contents from tool
results) would be sent off-box — don't do that unless you understand and accept
it.

---

## Layout

```
claudish-to-english/
├── .claude-plugin/
│   ├── plugin.json         # plugin manifest
│   └── marketplace.json    # so the repo can be added as a marketplace directly
├── hooks/
│   └── hooks.json          # MessageDisplay -> rewrite.sh ; PostToolUse -> rewrite-md.sh
├── rewrite.sh              # display-rewrite hook
├── rewrite-md.sh           # markdown-file rewrite hook (opt-in)
├── LICENSE
└── README.md
```

## License

MIT — see [LICENSE](./LICENSE).
