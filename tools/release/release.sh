#!/usr/bin/env bash
# release.sh — pre-release helper for ipSolis.
#
# Reads commit history since the previous tag, groups conventional-commit
# entries by type, and prints a draft CHANGELOG section + a checklist of
# manual steps. **Does not modify any files** — the output is meant to be
# reviewed, polished, and pasted into CHANGELOG.md by hand.
#
# Usage:
#   tools/release/release.sh <new-version>
#   tools/release/release.sh 0.5.0
#
# Run from anywhere inside the repo (the script `cd`s to repo root).
# Works under Git Bash on Windows + native bash on macOS/Linux.

set -euo pipefail

# ── Repo root ────────────────────────────────────────────────────────────────
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
  echo "ERROR: not inside a git repository." >&2
  exit 1
fi
cd "$repo_root"

# ── Args ─────────────────────────────────────────────────────────────────────
new_version="${1:-}"
if [[ -z "$new_version" ]]; then
  cat >&2 <<'USAGE'
Usage: tools/release/release.sh <new-version>
Example:
  tools/release/release.sh 0.5.0

Prints a draft CHANGELOG section based on commits since the previous tag,
plus the manual checklist for cutting the release. No files are modified.
USAGE
  exit 1
fi

if ! [[ "$new_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: '$new_version' is not a MAJOR.MINOR.PATCH semver." >&2
  exit 1
fi

# ── Previous tag ─────────────────────────────────────────────────────────────
# ``git describe --tags --abbrev=0`` walks back from HEAD to the closest tag.
# When the repo has no tags yet (first release ever), fall back to the root
# commit so the diff covers the whole history.
prev_tag="$(git describe --tags --abbrev=0 2>/dev/null || true)"
if [[ -z "$prev_tag" ]]; then
  prev_ref="$(git rev-list --max-parents=0 HEAD | head -1)"
  range_label="(all commits — no prior tag)"
else
  prev_ref="$prev_tag"
  range_label="$prev_tag..HEAD"
fi

today="$(date -u +%Y-%m-%d)"

# ── Sanity checks ────────────────────────────────────────────────────────────
warn=()
if [[ -n "$(git status --porcelain)" ]]; then
  warn+=("⚠ Working tree has uncommitted changes — release commit will sweep them up.")
fi
if git rev-parse "v$new_version" >/dev/null 2>&1; then
  warn+=("⚠ Tag v$new_version already exists. Pick a different version, or delete the existing tag first.")
fi
if [[ -n "$prev_tag" ]]; then
  # Strip leading 'v' if present (we tag as v0.4.1 but VERSION is bare 0.4.1)
  prev_bare="${prev_tag#v}"
  if [[ "$prev_bare" == "$new_version" ]]; then
    warn+=("⚠ Previous tag '$prev_tag' already matches '$new_version'. Did you mean to bump?")
  fi
fi

# ── Header ───────────────────────────────────────────────────────────────────
echo
echo "── ipSolis release draft — v$new_version ─────────────────────────────────"
echo "  Range:  $range_label"
echo "  Date:   $today"
echo

# ── Body: grouped commit summary ─────────────────────────────────────────────
# Conventional Commits → Keep-a-Changelog mapping. ``[:(]`` matches both
# bare ``feat:`` and scoped ``feat(scope):`` without backslash gymnastics
# that trip awk's escape-sequence warnings.
print_group() {
  local heading="$1" prefix_alt="$2"
  local pattern="^(${prefix_alt})[:(]"
  local lines
  lines="$(git log "${prev_ref}..HEAD" --no-merges --reverse \
            --pretty=format:'%s|%h' \
          | grep -Ei "$pattern" \
          || true)"
  if [[ -z "$lines" ]]; then
    return 0
  fi
  echo "### $heading"
  echo
  while IFS='|' read -r subject sha; do
    [[ -z "$subject" ]] && continue
    # Strip the conventional-commit prefix so the bullet reads cleanly.
    cleaned="$(echo "$subject" | sed -E 's/^(feat|fix|docs|chore|refactor|perf|test|ci|build|style|revert)(\([^)]+\))?: //I')"
    echo "- $cleaned (\`$sha\`)"
  done <<< "$lines"
  echo
}

print_group "Added"          'feat'
print_group "Fixed"           'fix'
print_group "Changed"         'refactor|perf|style'
print_group "Documentation"   'docs'
print_group "Other"           'chore|ci|build|test|revert'

# Anything that didn't match a known prefix at all (someone wrote a commit
# without conventional formatting). List them so you can re-categorise.
non_conventional="$(git log "${prev_ref}..HEAD" --no-merges --reverse \
  --pretty=format:'%s|%h' \
  | grep -Eiv '^(feat|fix|docs|chore|refactor|perf|test|ci|build|style|revert)[:(]' \
  || true)"
if [[ -n "$non_conventional" ]]; then
  echo "### ⚠ Non-conventional commits (categorise manually)"
  echo
  while IFS='|' read -r subject sha; do
    [[ -z "$subject" ]] && continue
    echo "- $subject (\`$sha\`)"
  done <<< "$non_conventional"
  echo
fi

# ── Warnings (post-body so they're visible right above the checklist) ────────
if (( ${#warn[@]} > 0 )); then
  echo "── Warnings ──────────────────────────────────────────────────────────"
  for w in "${warn[@]}"; do echo "  $w"; done
  echo
fi

# ── Checklist ────────────────────────────────────────────────────────────────
cat <<EOF
── Release checklist ────────────────────────────────────────────────────
  1) Edit CHANGELOG.md:
       • rename the existing '## [Unreleased]' header to
         '## [$new_version] — $today'
       • paste the curated draft above, polishing as needed
       • add a fresh '## [Unreleased]' header at the top for next time
  2) Bump VERSION (mind the encoding on Windows PowerShell 5.1):
       PS 5.1:  '$new_version' | Out-File -Encoding ascii VERSION
       PS 7+ :  Set-Content -Path VERSION -Value '$new_version' -Encoding utf8NoBOM
       bash  :  echo '$new_version' > VERSION   (LF, UTF-8 — no BOM)
  3) Stage + commit + tag:
       git add VERSION CHANGELOG.md
       git commit -m 'release: v$new_version'
       git tag -a v$new_version -m 'Release v$new_version'
  4) Restart the api so /app/VERSION re-reads (no rebuild needed):
       docker compose restart api
       curl -s http://localhost:8000/health   # ← should report "$new_version"
  5) Push when ready:
       git push origin <branch> && git push origin v$new_version
EOF
