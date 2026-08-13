#!/usr/bin/env python3
"""
PreToolUse hook -- rewrite GitHub issue/PR bodies into plain English BEFORE posting.

Unlike the MessageDisplay hook, this one is NOT display-only: it changes what
teammates actually read in the issue. That is the point, and it is also why
every uncertain path here fails open and leaves the command untouched.

Two shapes are handled, and they are deliberately not equally trusted:

  --body-file PATH   the file is rewritten in place and the command is left
                     completely alone. No shell string is rebuilt, so there is
                     nothing to mis-quote. THIS IS THE SAFE PATH -- prefer it.

  --body TEXT        the command must be rebuilt. Parsing uses shlex (never
                     regex), and any command containing shell syntax that
                     shlex cannot faithfully round-trip is skipped entirely.

FAIL-OPEN CONTRACT: on ANY doubt -- unparseable command, shell metacharacters,
LLM down, timeout, suspiciously short rewrite -- emit nothing and exit 0, which
runs the agent's original command unchanged. A hook that posts to GitHub must
never be able to garble what it did not fully understand.

Config (env):
  CLAUDISH_GH_ENABLED    1|0   master switch (default 1)
  CLAUDISH_OFF_FILE      path  shared kill switch (default ~/.claude/claudish-off)
  CLAUDISH_OLLAMA        url   rewrite endpoint (default http://localhost:11434)
  CLAUDISH_MODEL         name  model identifier
  CLAUDISH_GH_TIMEOUT    secs  LLM timeout (default 45; keep under the hook timeout)
  CLAUDISH_GH_MIN_CHARS  n     skip bodies shorter than this (default 200)
  CLAUDISH_GH_MIN_RATIO  f     reject rewrites shorter than f x original (default 0.5)
  CLAUDISH_GH_DRYRUN     1|0   log the rewrite but post the original (default 0)
  CLAUDISH_GH_COMMANDS   re    which commands qualify (default issue/pr create|comment|edit)
  CLAUDISH_DEBUG         1|0   append to $TMPDIR/claudish-to-english/debug-gh.log
"""

import json
import os
import re
import shlex
import sys
import urllib.request
from datetime import datetime

ENABLED = os.environ.get("CLAUDISH_GH_ENABLED", "1") == "1"
OFF_FILE = os.environ.get("CLAUDISH_OFF_FILE", os.path.expanduser("~/.claude/claudish-off"))
OLLAMA = os.environ.get("CLAUDISH_OLLAMA", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("CLAUDISH_MODEL", "claudish-rewriter")
TIMEOUT = float(os.environ.get("CLAUDISH_GH_TIMEOUT", "45"))
MIN_CHARS = int(os.environ.get("CLAUDISH_GH_MIN_CHARS", "200"))
MIN_RATIO = float(os.environ.get("CLAUDISH_GH_MIN_RATIO", "0.5"))
DRYRUN = os.environ.get("CLAUDISH_GH_DRYRUN", "0") == "1"
DEBUG = os.environ.get("CLAUDISH_DEBUG", "0") == "1"

GH_RE = re.compile(
    os.environ.get("CLAUDISH_GH_COMMANDS", r"\bgh\s+(issue|pr)\s+(create|comment|edit)\b"))

# Post-tokenisation shell-operator check; see main().
# Post-tokenisation: real shell operators appear as standalone tokens. Checking
# here rather than in the raw string means an issue body may freely contain
# '&', '>' or '->' -- inside quotes those are just prose, and scanning the raw
# command would refuse most normal writing.
SHELL_OPS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "2>", "2>&1"}

SYSTEM_PROMPT = (
    "You rewrite GitHub issue and pull request text into much simpler, plain English "
    "for teammates who did not write the code. Keep every fact, name, number, file path, "
    "URL, and issue reference. Keep all Markdown structure - headings, lists, tables, "
    "links, and checkboxes. Leave fenced code blocks completely unchanged. "
    "Use short sentences and everyday words. "
    "Output ONLY the rewritten text with no preamble, labels, or commentary."
)

LOG_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "claudish-to-english")


def has_unsafe_shell(cmd):
    """True if $(...), a backtick, or a heredoc appears where the SHELL would act on it.

    The distinction matters enormously here: Markdown issue bodies are full of
    backticks for inline code, but inside single quotes a backtick is literal
    text that shlex round-trips perfectly. Scanning the raw string without
    tracking quote state would refuse nearly every real issue body.

    Inside double quotes or unquoted, the same characters are substitution --
    re-quoting them would silently change what the command does, so we bail.
    """
    i, n = 0, len(cmd)
    in_single = in_double = False
    while i < n:
        c = cmd[i]
        if in_single:
            if c == "'":
                in_single = False
        elif in_double:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_double = False
            elif c == "`" or cmd.startswith("$(", i):
                return True
        else:
            if c == "'":
                in_single = True
            elif c == '"':
                in_double = True
            elif c == "`" or cmd.startswith("$(", i) or cmd.startswith("<<", i):
                return True
        i += 1
    return False


def dbg(msg):
    if not DEBUG:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "debug-gh.log"), "a") as fh:
            fh.write("%s [%d] %s\n" % (datetime.now().strftime("%H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def passthrough(why=""):
    """Fail open: emit nothing, run the agent's command unchanged."""
    dbg("passthrough: %s" % why)
    sys.exit(0)


def prose_len(text):
    """Length ignoring fenced code and whitespace -- the part worth rewriting."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return len("".join("".join(out).split()))


def rewrite(text):
    """Return the rewritten text, or None to fail open."""
    body = json.dumps({
        "model": MODEL,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode()
    req = urllib.request.Request(
        OLLAMA + "/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            parsed = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        dbg("llm call failed: %r" % (e,))
        return None

    if parsed.get("error"):
        dbg("llm error: %s" % parsed["error"])
        return None

    new = (parsed.get("message") or {}).get("content") or ""
    new = re.sub(r"<think>.*?</think>", "", new, flags=re.DOTALL | re.I).strip()
    if not new:
        dbg("empty rewrite")
        return None

    # The upstream Markdown hook's worst bug is having no guard here: a model
    # that truncates silently replaces real content. Never accept a rewrite
    # that lost most of the document.
    before, after = prose_len(text), prose_len(new)
    if before and after < before * MIN_RATIO:
        dbg("rejected: prose shrank %d -> %d (ratio %.2f < %.2f)"
            % (before, after, after / float(before), MIN_RATIO))
        return None
    return new


def find_flag(tokens, names):
    """Index of a flag's VALUE, or None. Handles --flag=value too."""
    for i, tok in enumerate(tokens):
        if tok in names and i + 1 < len(tokens):
            return i + 1, None
        for name in names:
            if name.startswith("--") and tok.startswith(name + "="):
                return i, name + "="
    return None, None


def handle_body_file(tokens, cwd):
    """Rewrite the referenced file in place. The command is never touched."""
    idx, prefix = find_flag(tokens, ("--body-file", "-F"))
    if idx is None:
        return False
    raw = tokens[idx][len(prefix):] if prefix else tokens[idx]
    if raw == "-":
        return passthrough("--body-file reads stdin")

    path = raw if os.path.isabs(raw) else os.path.join(cwd or ".", raw)
    try:
        with open(path, "r") as fh:
            original = fh.read()
    except Exception as e:
        return passthrough("cannot read body file: %r" % (e,))

    if prose_len(original) < MIN_CHARS:
        return passthrough("body file below min_chars")

    new = rewrite(original)
    if new is None:
        return passthrough("no usable rewrite for body file")
    if DRYRUN:
        dbg("DRYRUN: would rewrite %s (%d -> %d chars)" % (path, len(original), len(new)))
        return passthrough("dryrun")

    # Write atomically, and only within the file's own directory.
    tmp = "%s.claudish.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            fh.write(new if new.endswith("\n") else new + "\n")
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return passthrough("atomic write failed: %r" % (e,))

    dbg("rewrote body file %s (%d -> %d chars)" % (path, len(original), len(new)))
    sys.exit(0)  # command unchanged; nothing to emit


def handle_inline_body(tokens, tool_input):
    """Rewrite --body TEXT and rebuild the command with correct quoting."""
    idx, prefix = find_flag(tokens, ("--body", "-b"))
    if idx is None:
        return passthrough("no --body or --body-file found")

    original = tokens[idx][len(prefix):] if prefix else tokens[idx]
    if prose_len(original) < MIN_CHARS:
        return passthrough("inline body below min_chars")

    new = rewrite(original)
    if new is None:
        return passthrough("no usable rewrite for inline body")
    if DRYRUN:
        dbg("DRYRUN: would rewrite inline body (%d -> %d chars)" % (len(original), len(new)))
        return passthrough("dryrun")

    tokens[idx] = (prefix + new) if prefix else new
    try:
        rebuilt = " ".join(shlex.quote(t) for t in tokens)
    except Exception as e:
        return passthrough("could not rebuild command: %r" % (e,))

    updated = dict(tool_input)
    updated["command"] = rebuilt
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "updatedInput": updated}}, sys.stdout)
    dbg("rewrote inline body (%d -> %d chars)" % (len(original), len(new)))
    sys.exit(0)


def main():
    if not ENABLED:
        passthrough("disabled")
    if os.path.exists(OFF_FILE):
        passthrough("kill switch present")

    try:
        payload = json.load(sys.stdin)
    except Exception:
        passthrough("unparseable hook payload")

    if payload.get("tool_name") != "Bash":
        passthrough("not a Bash call")

    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") or ""
    if not GH_RE.search(cmd):
        passthrough("not a gh issue/pr write")
    if has_unsafe_shell(cmd):
        passthrough("heredoc or unquoted command substitution; left untouched")

    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        passthrough("shlex could not parse: %r" % (e,))
    if not tokens:
        passthrough("empty command")
    if any(t in SHELL_OPS for t in tokens):
        passthrough("command chains or redirects; left untouched")

    # Belt and braces: if re-quoting the untouched tokens does not reproduce a
    # command that re-parses identically, this command is not one we understand
    # well enough to rewrite.
    try:
        if shlex.split(" ".join(shlex.quote(t) for t in tokens)) != tokens:
            passthrough("command does not survive a quote round-trip")
    except ValueError:
        passthrough("command does not survive a quote round-trip")

    cwd = payload.get("cwd") or os.getcwd()
    handle_body_file(tokens, cwd)      # exits if it handled the command
    handle_inline_body(tokens, tool_input)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dbg("unhandled: %r" % (e,))
        sys.exit(0)  # fail open, always
