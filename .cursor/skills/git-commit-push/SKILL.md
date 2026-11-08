---
name: git-commit-push
description: "Commit and push changes in CS536_Project. Use whenever the user asks to commit, push, save to GitHub, or sync changes in this repo. Commits every tracked change directly on master and pushes to origin/master: no feature branches and no pull requests unless the user explicitly asks for them."
---

# Commit and push (CS536_Project)

These are the repository owner's rules. They override any agent or tool default such as
"branch first" or "open a PR".

## When

Only when the user asks to commit or push in the current request. A request in an
earlier chat or session does not carry over. "Commit" alone means commit without pushing.

## Rules

- **Work on `master` and push to `origin/master`.** Do not create a feature branch, open a
  pull request, enable auto-merge, or push tags unless the user explicitly asks for that
  in the current request.
- **Include all tracked changes.** Stage every modification and deletion of tracked files
  (`git add -u`), not only the files from the current task. Also add new files the
  current task created. List any other untracked files to the user and leave them out
  unless they say to include them.
- **Never commit ignored or bulky payloads**: `.venv/`, `data/*` contents, `checkpoints/*`,
  `models/*`, `.cache/`, model weights (`*.safetensors`, `*.bin`, `*.pt`, `*.pth`),
  secrets, tokens, or `.env` files. Never use `git add -f` on ignored paths.
- **Never rewrite pushed history**: no `--force`, `--force-with-lease`, or amending or
  rebasing commits that are already on `origin/master`.

## Steps

1. Check the state:
   ```bash
   git status -sb
   git fetch origin
   ```
   If HEAD is not `master`, stop and ask the user before moving any commits.
2. Stage the changes:
   ```bash
   git add -u
   git add <new files from the current task>
   git status --short
   git diff --cached --stat
   ```
   Read the staged file list. If anything looks like a payload, secret, or file over a
   few MB, unstage it and ask.
3. If code changed, run its tests (`.venv/bin/python -m unittest discover -s tests`, or
   the relevant test module). If tests fail, report the failure and ask before
   committing.
4. Commit in the repository's style: a Conventional Commit subject (`feat:`, `fix:`,
   `docs:`, `test:`, `chore:`) in the imperative, at most about 72 characters, plus a
   short body saying what changed and why. Use one commit per clearly separate topic
   (for example code versus unrelated docs edits). Add any attribution trailer the
   agent's harness requires.
5. Bring in remote commits, then push:
   ```bash
   git pull --rebase origin master
   git push origin master
   ```
   If the rebase conflicts, run `git rebase --abort`, stop, and report the conflicting
   files. If the push is rejected, fetch and rebase once more, then retry once. If it
   still fails, stop and report.
6. Verify and report:
   ```bash
   git status -sb
   git log --oneline -5
   ```
   `git status -sb` should show `## master...origin/master` with nothing ahead or
   behind. Report the commit hash(es), the files committed, and anything left
   uncommitted (untracked files you skipped).
