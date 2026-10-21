---
inclusion: always
---

# Git workflow

## Always use a pull request. Never push to `main`.

All changes reach `main` through a reviewed pull request — never a direct push
or commit to `main` (or to any protected long-lived branch).

- **Never** run `git push` to `main`, and never fast-forward or merge a branch
  into `main` locally and push the result. `main` only moves when a PR is
  merged on the host (GitHub).
- Do the work on a dedicated branch named for the change (e.g.
  `fix/<topic>`, `feat/<topic>`, `test/<topic>`). Commit there.
- Push the branch with `git push -u origin <branch>` and open a PR into `main`
  with `gh pr create` (concise title under ~70 chars; body summarizing the
  change, what was tested, and anything blocked or deferred).
- If commits were made directly on a local `main` by mistake, move them onto a
  branch, reset local `main` back to `origin/main`, push the branch, and open a
  PR — do not push the local `main`.
- Let the PR's review and checks gate the merge. Do not merge your own PR unless
  the user explicitly asks.

This rule overrides any default "push when done" behavior. When a task is
complete, the deliverable is an **open PR**, not an updated `main`.
