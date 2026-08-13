#!/usr/bin/env python3
"""
claudish gh wrapper (Python) -- the Windows-capable twin of gh-wrapper.sh.

Same idea, same config, same guarantees. It runs AFTER the shell has finished,
so heredocs are already expanded and quotes already stripped: the finished issue
text arrives as ordinary argv and there is nothing left to parse.

Use this one where the bash wrapper cannot go -- Windows, or anywhere without
jq. It needs only the Python standard library.

INSTALL (Windows)
  Copy gh-wrapper.py and gh.cmd into a directory that comes BEFORE the real gh
  on PATH, e.g. %USERPROFILE%\\.local\\bin. `where gh` must list gh.cmd first.
  In Git Bash the bash wrapper (`gh`) is used instead, so both can coexist.

INSTALL (macOS/Linux)
  Prefer gh-wrapper.sh. If you want this one instead, copy it as `gh`.

FAIL-OPEN: on ANY problem -- rewriter down, timeout, empty or truncated answer,
unreadable file -- the ORIGINAL command runs unchanged. This wrapper must never
stop you posting, and must never post anything it did not fully produce.

Config: identical to gh-wrapper.sh. See the README.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

BODY_INLINE = ("--body", "-b")
BODY_FILE = ("--body-file", "-F")
SUBJECTS = ("issue", "pr")
ACTIONS = ("create", "comment", "edit")

SYSTEM_PROMPT = (
    "You rewrite GitHub issue and pull request text into much simpler, plain English "
    "for teammates who did not write the code. Keep every fact, name, number, file path, "
    "URL, and issue reference. Keep all Markdown structure - headings, lists, tables, "
    "links, and checkboxes. Leave fenced code blocks completely unchanged. "
    "Use short sentences and everyday words. "
    "Output ONLY the rewritten text with no preamble, labels, or commentary."
)

LOG_DIR = os.path.join(tempfile.gettempdir(), "claudish-to-english")


def env(name, default=""):
    return os.environ.get(name, default)


def dbg(msg):
    if env("CLAUDISH_DEBUG", "0") != "1":
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "debug-ghwrap.log"), "a") as fh:
            fh.write("[%d] %s\n" % (os.getpid(), msg))
    except Exception:
        pass


def find_real_gh():
    """Locate the real gh, never re-entering this wrapper."""
    override = env("CLAUDISH_GH_REAL")
    if override and os.path.isfile(override):
        return override

    try:
        self_dir = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        self_dir = ""

    names = ["gh.exe", "gh.cmd", "gh.bat", "gh"] if os.name == "nt" else ["gh"]
    for entry in env("PATH").split(os.pathsep):
        if not entry:
            continue
        try:
            if os.path.realpath(entry) == self_dir:
                continue  # our own directory: skip, or we recurse
        except Exception:
            pass
        for name in names:
            cand = os.path.join(entry, name)
            if os.path.isfile(cand) and (os.name == "nt" or os.access(cand, os.X_OK)):
                return cand
    return None


REAL_GH = find_real_gh()


def passthrough(why, args):
    """Fail open: run exactly what the caller asked for."""
    dbg("passthrough: %s" % why)
    if not REAL_GH:
        sys.stderr.write("claudish gh wrapper: cannot find the real gh on PATH\n")
        sys.exit(127)
    sys.exit(subprocess.call([REAL_GH] + args))


def prose_len(text):
    """Length ignoring fenced code and whitespace."""
    out, fenced = [], False
    for line in (text or "").splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return len("".join("".join(out).split()))


def find_body(args):
    """Return (kind, index, prefix) for the body argument, or (None, -1, None).

    prefix is set for the --flag=value form, where value shares the token.
    """
    for i, tok in enumerate(args):
        if tok in BODY_INLINE and i + 1 < len(args):
            return "inline", i + 1, None
        if tok in BODY_FILE and i + 1 < len(args):
            return "file", i + 1, None
        for name in BODY_INLINE + BODY_FILE:
            if name.startswith("--") and tok.startswith(name + "="):
                kind = "inline" if name in BODY_INLINE else "file"
                return kind, i, name + "="
    return None, -1, None


def rewrite(text):
    """Return rewritten text, or None to fail open."""
    payload = json.dumps({
        "model": env("CLAUDISH_MODEL", "claudish-rewriter"),
        "stream": False,
        "think": False,
        "options": {"temperature": 0.3},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode()
    url = env("CLAUDISH_OLLAMA", "http://localhost:11434").rstrip("/") + "/api/chat"
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        timeout = float(env("CLAUDISH_GH_TIMEOUT", "45"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        dbg("rewrite call failed: %r" % (e,))
        sys.stderr.write("claudish: rewrite unavailable, posting the original.\n")
        return None

    if parsed.get("error"):
        dbg("rewriter error: %s" % parsed["error"])
        sys.stderr.write("claudish: rewrite skipped (%s). Posting the original.\n"
                         % str(parsed["error"])[:200])
        return None

    new = (parsed.get("message") or {}).get("content") or ""
    new = re.sub(r"<think>.*?</think>", "", new, flags=re.DOTALL | re.I).strip()
    return new or None


def main():
    args = sys.argv[1:]

    if env("CLAUDISH_GH_WRAPPER", "1") != "1":
        passthrough("wrapper disabled", args)
    off = env("CLAUDISH_OFF_FILE") or os.path.join(os.path.expanduser("~"), ".claude", "claudish-off")
    if os.path.exists(off):
        passthrough("kill switch", args)
    if env("CLAUDISH_GH_ONLY_CC", "1") == "1" and not env("CLAUDECODE"):
        passthrough("not running under Claude Code", args)

    if len(args) < 2 or args[0] not in SUBJECTS or args[1] not in ACTIONS:
        passthrough("not an issue/pr write", args)

    kind, idx, prefix = find_body(args)
    if not kind:
        passthrough("no body argument", args)

    raw = args[idx][len(prefix):] if prefix else args[idx]
    if kind == "file":
        if raw == "-":
            passthrough("--body-file reads stdin", args)
        try:
            with open(raw, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except Exception as e:
            passthrough("body file unreadable: %r" % (e,), args)
    else:
        body = raw
    if not body.strip():
        passthrough("empty body", args)

    before = prose_len(body)
    if before < int(env("CLAUDISH_GH_MIN_CHARS", "200")):
        passthrough("below min_chars (%d)" % before, args)

    new = rewrite(body)
    if new is None:
        passthrough("no usable rewrite", args)

    # Never let a truncated rewrite reach a real issue.
    after = prose_len(new)
    floor = before * int(env("CLAUDISH_GH_MIN_PCT", "50")) / 100.0
    if after < floor:
        dbg("rejected: prose %d -> %d (floor %.0f)" % (before, after, floor))
        sys.stderr.write("claudish: rewrite lost too much content, posting the original.\n")
        passthrough("rewrite too short", args)

    if env("CLAUDISH_GH_DRYRUN", "0") == "1":
        sys.stderr.write("claudish: DRYRUN, posting the original (%d -> %d prose chars).\n"
                         % (before, after))
        passthrough("dryrun", args)

    # Hand the rewrite to the real gh via a temp file -- never the caller's own
    # --body-file, which stays exactly as they wrote it.
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="claudish-gh-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new if new.endswith("\n") else new + "\n")
    except Exception as e:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        passthrough("temp write failed: %r" % (e,), args)

    out = list(args)
    if prefix:
        out[idx] = "--body-file=" + tmp
    else:
        out[idx - 1] = "--body-file"
        out[idx] = tmp

    dbg("rewrote body (%d -> %d prose chars) via %s" % (before, after, tmp))
    try:
        rc = subprocess.call([REAL_GH] + out)
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    sys.exit(rc)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dbg("unhandled: %r" % (e,))
        passthrough("unhandled error", sys.argv[1:])
