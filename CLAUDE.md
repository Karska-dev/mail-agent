# Working rules for this repo

## Review locally before anything is committed or pushed

- Claude never commits, pushes, opens or updates a pull request, merges, or starts a release until the owner has
  reviewed the change on their own computer and said so in the conversation ("commit", "push", "go ahead").
- Until then the change stays as uncommitted edits. If the owner's folder (`~/Repo/ReadMyMail/email-agent`) is
  connected, Claude writes the edits there; otherwise Claude sends a patch file. Nothing is pushed to GitHub to "show" it.
- When a change is ready for review, Claude says exactly how to check it: `git status`, `git diff`, run the app
  (`python3 -m email_agent app`) and open the screens that changed, and run `python3 -m pytest -q` (tests must pass first).
- An approval covers only the diff that was reviewed. If Claude changes anything afterwards, it asks again.
- After approval: commit to a branch (never straight to `main`), push the branch, open a pull request. The owner merges
  and runs the release workflow.

## Other rules

- Python standard library only; nothing is loaded from the internet at run time (fonts are bundled in `email_agent/web/index.html`).
- Add a line to `CHANGELOG.md` under `[Unreleased]` for anything the user can see.
