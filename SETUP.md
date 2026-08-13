# Setup guide

How to get plain-English rewrites working on your machine.

## What this does

Two separate things. You can have either, or both.

**1. It rewrites GitHub issue and PR text before it posts.** Your teammates read
plain English in the issue instead of dense engineering prose. This is the one
that matters for the team, because it changes what other people read.

**2. It shows a plain-English version of Claude's replies in your terminal.**
This is display-only. It never changes what Claude does, what goes in the
transcript, or what Claude remembers. Only your screen changes.

Both use a small language model running on **base-mini**. Nothing is sent to a
third party.

## Before you start

You need:

- **Tailscale**, connected to the tailnet. Everything runs on base-mini and is
  reached over Tailscale. It is not exposed to the internet.
- **jq 1.6 or newer** — `jq --version`
- **curl**
- **Python 3** — needed on Windows, and for the fallback hook anywhere

If any are missing, install them first. On macOS: `brew install jq`.

## How it fits together

```
your machine                              base-mini
────────────                              ─────────
Claude Code
  │
  ├─ terminal reply ──────┐
  │                       │
  └─ gh issue comment ──┐ │
                        │ │
                        ▼ ▼
              https://base-mini.tailef92b4.ts.net:8444
                        │
                        ▼
                    claudish-shim ──► LM Studio
                                      (claudish-rewriter)
```

If base-mini is asleep or LM Studio is closed, nothing breaks. You just see the
original text, unchanged.

---

## Part 1 — Install the plugin

In Claude Code:

```
/plugin marketplace add expa-ai/claudish-to-english
/plugin install claudish-to-english@expa-plugins
```

The marketplace is called `expa-plugins`, not `gvzdv-plugins`. This is our fork.

## Part 2 — Install the gh wrapper

This is what makes the GitHub rewrite reliable. Without it, the rewrite only
works some of the time — see [Why the wrapper](#why-the-wrapper) at the end.

### macOS and Linux

```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/expa-ai/claudish-to-english/main/gh-wrapper.sh \
  -o ~/.local/bin/gh
chmod +x ~/.local/bin/gh
```

Check it:

```sh
which gh        # must print ~/.local/bin/gh
gh --version    # must still print the real gh version
```

If `which gh` shows the old path, add this to `~/.zshrc` (or `~/.bashrc`) and
open a new terminal:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

### Windows

Download **both** files and keep them in the same folder — `gh.cmd` looks for
`gh-wrapper.py` next to itself.

- `gh-wrapper.py`
- `gh.cmd`

```bat
mkdir "%USERPROFILE%\.local\bin"
```

Put both files in `%USERPROFILE%\.local\bin`, then add that folder to your PATH
**above** the entry for the real GitHub CLI.

Check it:

```bat
where gh          :: gh.cmd must be listed FIRST
gh --version      :: must still print the real gh version
```

If you use **Git Bash**, also install the macOS version above. Git Bash looks
for `gh`, not `gh.cmd`. The two do not conflict.

## Part 3 — Settings

Add this to `~/.claude/settings.json`. If you already have an `env` block, add
the keys to it rather than replacing it.

```json
{
  "env": {
    "CLAUDISH_OLLAMA": "https://base-mini.tailef92b4.ts.net:8444",
    "CLAUDISH_MODEL": "claudish-rewriter",
    "CLAUDISH_MODE": "append",
    "CLAUDISH_GH_ENABLED": "0"
  }
}
```

What each one does:

| Key | Meaning |
|---|---|
| `CLAUDISH_OLLAMA` | Where the rewriter lives. **On base-mini itself use `http://127.0.0.1:8444`** — faster, and works without Tailscale. |
| `CLAUDISH_MODEL` | Which model. Leave as is. |
| `CLAUDISH_MODE` | `append` shows the original *and* the rewrite. `replace` shows only the rewrite. Stick with `append`. |
| `CLAUDISH_GH_ENABLED` | `0` turns off the older hook. **Required if you installed the gh wrapper**, or your text gets rewritten twice. |

Don't want the terminal rewrite, only the GitHub one? Add `"CLAUDISH_ENABLED": "0"`.

## Part 4 — Restart

Close Claude Code and open it again. These settings are read at startup, so a
running session keeps the old ones.

---

## Check it worked

**1. Can you reach the server?**

```sh
curl -s https://base-mini.tailef92b4.ts.net:8444/healthz
```

Expected:

```json
{"ok": true, "model": "claudish-rewriter", "detail": null}
```

If this fails, check Tailscale first.

**2. Is the terminal rewrite running?**

Ask Claude something that produces a long, technical answer. Below it you should
see a divider and `💬 In plain English:` with a simpler version.

It takes about **13 seconds** after the reply finishes. Replies under 200
characters are skipped on purpose.

**3. Is the GitHub rewrite running?**

Try it in dry-run first — it logs what it *would* post, and posts your original:

```sh
export CLAUDISH_GH_DRYRUN=1
export CLAUDISH_DEBUG=1
```

Ask Claude to comment on a test issue, then read the log:

```sh
cat "$TMPDIR/claudish-to-english/debug-ghwrap.log"
```

Remove both variables when you're happy.

---

## Turning it off

**Pause right now**, no restart:

```sh
touch ~/.claude/claudish-off
```

Turn it back on:

```sh
rm ~/.claude/claudish-off
```

This stops everything — terminal rewrites and GitHub rewrites.

**Turn off permanently**: remove the keys from `settings.json` and restart.

---

## When something goes wrong

Everything here **fails open**. If the rewriter is down, slow, or returns
nonsense, you get your original text. It can never block you from posting and
never posts anything it did not fully produce.

| What you see | What it means | What to do |
|---|---|---|
| `Cannot reach LM Studio` | base-mini is asleep, or LM Studio was closed | Wake base-mini, open LM Studio |
| `not loaded (state: not-loaded)` | The model was unloaded | Ask the admin to reload it |
| `busy with another rewrite` | Three rewrites already running | Nothing. It skipped one on purpose |
| `rewrite lost too much content` | The model truncated its answer | Nothing. Your original was posted |
| No rewrite, no message at all | Usually the plugin isn't loading | See below |
| `gh` stopped working entirely | The wrapper is broken | `rm ~/.local/bin/gh` restores normal gh |

**Nothing happens at all:**

```sh
jq --version                       # must be 1.6 or newer
which gh                           # must be your ~/.local/bin/gh
curl -s https://base-mini.tailef92b4.ts.net:8444/healthz
```

Then turn on logging, restart Claude Code, and read:

```sh
export CLAUDISH_DEBUG=1
```

- `$TMPDIR/claudish-to-english/debug.log` — terminal rewrites
- `$TMPDIR/claudish-to-english/debug-ghwrap.log` — GitHub rewrites

---

## Why the wrapper

Claude writes issue text inside a shell command, and it can do that several ways:

```sh
gh issue comment 42 --body "$(cat <<EOF
long text
EOF
)"
```

That is a heredoc, and it is what Claude reaches for by default. Reading the text
back out of it safely is not possible — rebuilding the command wrong could post
broken text or run something unintended.

The wrapper sidesteps this. It sits in front of the real `gh`, so it runs **after**
the shell has finished its work. By then the heredoc is expanded and the quotes
are gone, and the finished text arrives as ordinary arguments. There is nothing
left to parse, so nothing left to get wrong.

That is why the wrapper is reliable and a `CLAUDE.md` instruction is not.

---

## For the admin (base-mini only)

The server side runs at `~/projects/deploy/claudish-shim/`.

```sh
launchctl list | grep claudish            # is the shim running?
curl -s localhost:8444/healthz            # is the model loaded?
tail -f ~/projects/deploy/claudish-shim/logs/shim-stderr.log
```

Restart the shim:

```sh
launchctl unload ~/Library/LaunchAgents/com.baseadmin.claudish-shim.plist
launchctl load   ~/Library/LaunchAgents/com.baseadmin.claudish-shim.plist
```

Reload the model:

```sh
lms load qwen/qwen3-8b --identifier claudish-rewriter --context-length 16384
```

**Use a non-reasoning model.** A reasoning model spends thousands of tokens
thinking first. Measured on one real message, a 35B reasoning model took 61.8
seconds against Claude Code's 60-second hook limit — every rewrite was killed,
silently. The current model does the same job in about 13 seconds.

**Known trap: LM Studio must be restarted after it updates itself.** Its process
ran for three months across two in-place updates, and in that state every MLX
model failed to load with a bare `FileNotFoundError` while GGUF models loaded
fine. A restart fixed it. Note that `pkill` does not kill it — it catches the
signal. Kill the main PID from `pgrep -f "MacOS/LM Studio"` and check the new
process start time.
