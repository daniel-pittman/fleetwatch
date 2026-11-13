# Contributing to fleetwatch

Thanks for helping. Small, focused changes are the easiest to review.

## Set up

```bash
git clone https://github.com/daniel-pittman/fleetwatch.git
cd fleetwatch
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[memory,test]"
pip install ruff
```

The `search` extra installs sentence-transformers and torch. Tests do not need it:
they build their indexes with the TF-IDF backend and never download a model.

The document-format check also runs `pandoc`, LibreOffice's `soffice` and `tesseract`
(`brew install pandoc tesseract poppler && brew install --cask libreoffice`, or the
`pandoc tesseract-ocr poppler-utils libreoffice-*` packages on Debian and Ubuntu). Without
them that one check is reported as skipped, naming each missing program; CI installs them
and fails if any is missing.

## Before you open a pull request

```bash
ruff check .
FLEETWATCH_NO_MIGRATE=1 python -m unittest discover -s tests
```

Both must pass; CI runs the same two commands on Python 3.11 to 3.14, on Linux and macOS.
Add tests for new behavior and bug fixes.

## Keep the docs current

A change is done when the docs say what the code now does. In the same pull request, update
everything the change affects: the README, command help and usage text, this file and
SECURITY.md, and `docs/` (`docs/how-it-works.md`). If the change alters a screen, retake
its screenshots with `scripts/capture_screenshots.py` (it runs the board in `--demo` mode, so
only fictional data appears).

## No personal data

This repository is public. Tests, fixtures, docs and examples use fictional names
only: made-up people, projects and session names, paths under a placeholder user
(`/Users/alex/...`), and `example.com` addresses. Never paste a real session name,
transcript text, home directory, email address, cloud-drive path or token.

`tests/test_no_leaks.py` fails the suite when a tracked file holds a home path, an
email address outside `example.*` or `noreply`, a cloud-drive path fragment, or a
key or token shape. To add your own names to the check without putting them in the
repository, write them one per line in a text file outside the repo and point
`FLEETWATCH_LEAK_TERMS` at it.

### Pre-push hook

The hook runs the same check, with your private list, over the tracked files and the
messages of the commits you are pushing:

```bash
ln -sf ../../scripts/pre-push .git/hooks/pre-push
```

It reads the private list from `FLEETWATCH_LEAK_TERMS`, or from
`~/.config/fleetwatch-leak-terms.txt` when that file exists.

## What runs on a pull request

- **CI** runs ruff and the full test suite. The `CI gate` check summarizes it.
- **Code review**: a Semgrep (OSS) scan and the CI result are posted as comments, then
  Claude reviews the diff and comments with findings. It flags source changes that
  arrive without tests. Treat its findings like any reviewer's. The review is advisory
  and does not block a merge. The reviewer can read and search the checkout and
  comment on the PR, and nothing else: it has no shell, and it cannot edit files, push, or
  reach the web. Comments from anyone without write access never reach it. It cannot
  authenticate on a pull request from a fork, and it skips any pull request whose author lacks write access, so a maintainer reviews those locally. Dependabot pull requests skip it too. It runs
  only when a pull request is opened or updated; there is no manual trigger. A pull request
  that edits `.github/workflows/claude*.yml` gets no Claude review (the action skips
  workflows that differ from the default branch's), so the change lands by administrator
  override.
- **`@claude`**: a maintainer or collaborator can mention `@claude` in an issue or PR
  comment to ask a question about the code. It runs only when the commenter and the
  author of the issue or PR both have write access, and never on a fork's pull request. It is
  read-only: it reads and searches the checkout and replies, and it cannot edit files, run
  shell commands, or push. It ignores everyone else, and comments in the thread from people
  without write access are not shown to it.

A first-time outside contributor's workflow run needs a maintainer's approval before it starts.

## Reporting bugs and vulnerabilities

Open an issue with steps to reproduce. For a security problem, do not open an
issue: follow `SECURITY.md`.

## Branches

Open pull requests against `develop`. `main` holds released code.
