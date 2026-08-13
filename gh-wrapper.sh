#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# claudish gh wrapper -- installed as ~/.local/bin/gh, ahead of the real gh.
#
# WHY THIS EXISTS
#
# The PreToolUse hook sees the command BEFORE the shell runs it, so it has to
# cope with heredocs, $(...) and quoting -- and it refuses anything it cannot
# rebuild safely, which is most real multi-line issue bodies.
#
# This wrapper runs AFTER the shell has finished. Whatever Claude wrote, by the
# time we are called the shell has expanded the heredoc, stripped the quotes
# and handed us the finished text as ordinary arguments. There is nothing left
# to parse, so nothing left to get wrong.
#
# WHAT IT DOES
#
# For `gh issue|pr create|comment|edit` carrying a body, the body is rewritten
# into plain English and passed to the real gh via --body-file. Every other gh
# command is exec'd straight through.
#
# The caller's own --body-file is never modified: the rewrite goes to a temp
# file. Your file on disk stays exactly as written.
#
# FAIL-OPEN: on ANY problem -- rewriter down, timeout, bad response, missing
# jq -- the ORIGINAL command runs unchanged. This wrapper must never be able to
# stop you posting, and must never post something it did not fully produce.
#
# Config (env):
#   CLAUDISH_GH_WRAPPER   1|0  master switch (default 1)
#   CLAUDISH_GH_ONLY_CC   1|0  only rewrite inside Claude Code (default 1).
#                              Set 0 to also rewrite gh commands you type.
#   CLAUDISH_OLLAMA       url  rewriter endpoint
#   CLAUDISH_MODEL        name model identifier
#   CLAUDISH_GH_TIMEOUT   s    rewrite timeout (default 45)
#   CLAUDISH_GH_MIN_CHARS n    skip bodies shorter than this (default 200)
#   CLAUDISH_GH_MIN_PCT   n    reject rewrites below n% of the original (default 50)
#   CLAUDISH_GH_DRYRUN    1|0  log what would change, post the original (default 0)
#   CLAUDISH_DEBUG        1|0  log to $TMPDIR/claudish-to-english/debug-ghwrap.log
# ---------------------------------------------------------------------------
set -uo pipefail

SELF="${BASH_SOURCE[0]}"
LOG_DIR="${TMPDIR:-/tmp}/claudish-to-english"

dbg() {
  [ "${CLAUDISH_DEBUG:-0}" = "1" ] || return 0
  mkdir -p "$LOG_DIR" 2>/dev/null || return 0
  printf '%s [%s] %s\n' "$(date '+%H:%M:%S')" "$$" "$*" >> "$LOG_DIR/debug-ghwrap.log" 2>/dev/null
  return 0
}

# Locate the real gh without ever re-entering this script.
find_real_gh() {
  local self_dir cand
  # Explicit override: for testing, and for a gh installed somewhere unusual.
  if [ -n "${CLAUDISH_GH_REAL:-}" ] && [ -x "${CLAUDISH_GH_REAL}" ]; then
    printf '%s' "$CLAUDISH_GH_REAL"; return 0
  fi
  self_dir="$(cd "$(dirname "$SELF")" 2>/dev/null && pwd -P)" || self_dir=""
  for cand in /opt/homebrew/bin/gh /usr/local/bin/gh /usr/bin/gh; do
    [ -x "$cand" ] && { printf '%s' "$cand"; return 0; }
  done
  while IFS= read -r cand; do
    [ -x "$cand" ] || continue
    [ "$(cd "$(dirname "$cand")" 2>/dev/null && pwd -P)" = "$self_dir" ] && continue
    printf '%s' "$cand"; return 0
  done < <(command -v -a gh 2>/dev/null)
  return 1
}

REAL_GH="$(find_real_gh)" || {
  printf 'claudish gh wrapper: cannot find the real gh on PATH\n' >&2
  exit 127
}

ORIG=("$@")

# Fail-open exit: run exactly what the caller asked for.
# ${ORIG[@]+...} guards empty-array expansion, which errors under set -u on
# the bash 3.2 that ships with macOS.
passthrough() {
  dbg "passthrough: ${1:-}"
  exec "$REAL_GH" ${ORIG[@]+"${ORIG[@]}"}
}

[ "${CLAUDISH_GH_WRAPPER:-1}" = "1" ]              || passthrough "wrapper disabled"
[ -f "${CLAUDISH_OFF_FILE:-$HOME/.claude/claudish-off}" ] && passthrough "kill switch"
if [ "${CLAUDISH_GH_ONLY_CC:-1}" = "1" ] && [ -z "${CLAUDECODE:-}" ]; then
  passthrough "not running under Claude Code"
fi
command -v jq   >/dev/null 2>&1 || passthrough "no jq"
command -v curl >/dev/null 2>&1 || passthrough "no curl"

# Only issue/pr writes carry a body worth rewriting.
case "${1:-}" in issue|pr) ;; *) passthrough "not issue/pr" ;; esac
case "${2:-}" in create|comment|edit) ;; *) passthrough "not create/comment/edit" ;; esac

# ---- locate the body argument ---------------------------------------------
# BODY_KIND: inline (text is in argv) | file (text is in a file)
# BODY_IDX : index of the value; VAL_INLINE means --flag=value in one token
args=("$@")
BODY_KIND=""; BODY_IDX=-1; JOINED=0
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
    --body|-b)        BODY_KIND="inline"; BODY_IDX=$((i + 1)); break ;;
    --body-file|-F)   BODY_KIND="file";   BODY_IDX=$((i + 1)); break ;;
    --body=*)         BODY_KIND="inline"; BODY_IDX=$i; JOINED=1; break ;;
    --body-file=*)    BODY_KIND="file";   BODY_IDX=$i; JOINED=1; break ;;
  esac
done
[ -n "$BODY_KIND" ] || passthrough "no body argument"
[ "$BODY_IDX" -lt "${#args[@]}" ] || passthrough "body flag has no value"

if [ "$JOINED" = "1" ]; then
  RAW="${args[BODY_IDX]#*=}"
else
  RAW="${args[BODY_IDX]}"
fi

if [ "$BODY_KIND" = "file" ]; then
  [ "$RAW" = "-" ] && passthrough "--body-file reads stdin"
  [ -r "$RAW" ]    || passthrough "body file unreadable"
  BODY="$(cat -- "$RAW" 2>/dev/null)" || passthrough "body file unreadable"
else
  BODY="$RAW"
fi
[ -n "$BODY" ] || passthrough "empty body"

# ---- length gate ----------------------------------------------------------
prose_len_of() {
  printf '%s' "$1" \
    | awk 'BEGIN{f=0} /^```/{f=!f; next} f==0{print}' \
    | tr -d '[:space:]' | wc -c | tr -d ' '
}
BEFORE_LEN="$(prose_len_of "$BODY")"
[ "${BEFORE_LEN:-0}" -ge "${CLAUDISH_GH_MIN_CHARS:-200}" ] || passthrough "below min_chars ($BEFORE_LEN)"

# ---- rewrite --------------------------------------------------------------
SYS="You rewrite GitHub issue and pull request text into much simpler, plain English for teammates who did not write the code. Keep every fact, name, number, file path, URL, and issue reference. Keep all Markdown structure - headings, lists, tables, links, and checkboxes. Leave fenced code blocks completely unchanged. Use short sentences and everyday words. Output ONLY the rewritten text with no preamble, labels, or commentary."

REQ="$(jq -n --arg m "${CLAUDISH_MODEL:-claudish-rewriter}" --arg s "$SYS" --arg u "$BODY" \
      '{model:$m,stream:false,think:false,options:{temperature:0.3},messages:[{role:"system",content:$s},{role:"user",content:$u}]}' 2>/dev/null)"
[ -n "$REQ" ] || passthrough "could not build request"

RESP="$(printf '%s' "$REQ" | curl -sS --max-time "${CLAUDISH_GH_TIMEOUT:-45}" \
        -H 'Content-Type: application/json' -X POST \
        "${CLAUDISH_OLLAMA:-http://localhost:11434}/api/chat" -d @- 2>/dev/null)"
CURL_RC=$?
NEW="$(printf '%s' "$RESP" | jq -j '.message.content // empty' 2>/dev/null)"
ERR="$(printf '%s' "$RESP" | jq -r '.error // empty' 2>/dev/null)"
dbg "rewrite curl_rc=$CURL_RC before=$BEFORE_LEN new_bytes=${#NEW} err=${ERR:-none}"

if [ -z "$NEW" ]; then
  [ -n "$ERR" ] && printf 'claudish: rewrite skipped (%s). Posting the original.\n' "$ERR" >&2
  passthrough "no rewrite"
fi

# Never let a truncated rewrite reach a real issue.
AFTER_LEN="$(prose_len_of "$NEW")"
MIN_KEEP=$(( BEFORE_LEN * ${CLAUDISH_GH_MIN_PCT:-50} / 100 ))
if [ "${AFTER_LEN:-0}" -lt "$MIN_KEEP" ]; then
  dbg "rejected: prose $BEFORE_LEN -> $AFTER_LEN (floor $MIN_KEEP)"
  printf 'claudish: rewrite lost too much content, posting the original.\n' >&2
  passthrough "rewrite too short"
fi

if [ "${CLAUDISH_GH_DRYRUN:-0}" = "1" ]; then
  dbg "DRYRUN: would rewrite ($BEFORE_LEN -> $AFTER_LEN prose chars)"
  printf 'claudish: DRYRUN, posting the original (%s -> %s prose chars).\n' "$BEFORE_LEN" "$AFTER_LEN" >&2
  passthrough "dryrun"
fi

# ---- hand the rewrite to the real gh via a temp file ----------------------
# A temp file, never the caller's own --body-file: their file stays untouched.
TMP="$(mktemp "${TMPDIR:-/tmp}/claudish-gh.XXXXXX")" || passthrough "mktemp failed"
trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$NEW" > "$TMP" 2>/dev/null || passthrough "temp write failed"

if [ "$JOINED" = "1" ]; then
  args[BODY_IDX]="--body-file=$TMP"
else
  args[BODY_IDX - 1]="--body-file"
  args[BODY_IDX]="$TMP"
fi

dbg "rewrote body ($BEFORE_LEN -> $AFTER_LEN prose chars) via $TMP"
"$REAL_GH" "${args[@]}"
exit $?
