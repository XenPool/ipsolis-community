# Copilot / AI assistant instructions for this repo

These hints apply to GitHub Copilot, Copilot Chat, Cursor, and any other
AI integration that respects this convention. The aim is to keep
output that downstream tooling (CHANGELOG generation, release scripts,
PR templates) can consume without re-formatting.

## Commit messages

Use **[Conventional Commits](https://www.conventionalcommits.org/)** so
each entry flows into `CHANGELOG.md` via `tools/release/release.sh`:

```
type(scope): short imperative subject
```

### Types (and their CHANGELOG section)

| Type        | CHANGELOG bucket          | When to use |
|-------------|---------------------------|-------------|
| `feat`      | **Added**                 | A new user-visible feature, endpoint, config key, UI affordance |
| `fix`       | **Fixed**                 | A bug or regression repair |
| `refactor`  | **Changed**               | Internal cleanup with no behaviour change |
| `perf`      | **Changed**               | Performance improvement (no API change) |
| `style`     | **Changed**               | Pure formatting / whitespace |
| `docs`      | **Documentation**         | README, CHANGELOG prose, inline doc strings |
| `chore`     | **Other**                 | Build, deps, tooling, repo hygiene |
| `ci`        | **Other**                 | GitHub Actions, pipeline config |
| `build`     | **Other**                 | Dockerfile, requirements.txt, packaging |
| `test`      | **Other**                 | Test suite changes only |
| `revert`    | **Other**                 | A `git revert` of a prior commit |

For breaking changes, append `!` to the type/scope (e.g. `feat(api)!:`)
and call out the migration impact in the body.

### Subject line rules

- **Imperative mood** — "add", not "added" / "adds" / "adding".
- **≤ 72 characters** so `git log --oneline` stays readable.
- **No trailing period.**
- Lowercase first letter (after the prefix).
- Be specific: `fix(rbac): clear locked_at on superadmin password reset`
  beats `fix: bug in admin users page`.

### Body (optional but valued)

Leave a blank line after the subject, then prose. The body matters when:

- the *why* is non-obvious (a constraint, deadline, incident, or
  surprising tradeoff drove the change)
- there is **migration impact** — schema change, breaking config key,
  flag rename, removed endpoint
- the user-visible effect deserves more than one line

The body becomes CHANGELOG-quality material when curated for a release,
so explain **intent**, not the diff. Bad: "added a new column to
admin_users". Good: "track last password change so the rotation
policy can flag expired creds".

### Examples

```
feat(rbac): add per-rule SoD opt-out via approval_rules.sod_exempt

Lets a static compliance officer who is also an admin sign off on
orders for asset types they configured. The flag is captured at
order-creation time on each OrderApproval row so subsequent rule
edits don't shift past orders' SoD logic.

Migration: 0073 adds order_approvals.sod_exempt (default false).
```

```
fix(auth): tolerate UTF-16 BOM in /app/VERSION

Windows PowerShell 5.1's `>` redirection writes UTF-16 LE with BOM
by default; the version resolver crashed module import on the bad
encoding, taking portal + ui offline. Now reads as bytes and tries
utf-8-sig → utf-16 → utf-8 in order.
```

```
chore(release): bump VERSION to 0.5.0
```

## Other AI-assisted output

- **Code comments**: only when the *why* is non-obvious. Don't restate
  the diff. Don't reference issue numbers — they belong in commits.
- **Code style**: follow the surrounding patterns; don't introduce a
  new abstraction layer for a one-shot change.
- **No emojis** in code, commits, or docs unless the user asked for them.
