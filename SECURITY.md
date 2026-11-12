# Security Policy

## Reporting a vulnerability

Do not open a public issue for a security vulnerability: a public issue
discloses the problem before a fix exists.

Use GitHub private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Describe the impact and the steps to reproduce it.

You will get an acknowledgement, a fix, and a disclosure timeline agreed with you.

## Scope

fleetwatch is a local terminal tool with no network service. It reads Claude
session metadata and transcripts under your home directory, indexes them
locally, and can open a terminal window to resume a session. The realistic
surface is path handling, parsing of untrusted transcript or document content,
the privacy scanner that scrubs indexed text, and the commands it launches.
Reports in any of those areas are welcome.

## Supported versions

Fixes go to the latest release on the default branch.

## Installing with verified hashes

`pyproject.toml` pins every dependency by version, and `uv.lock` records each
artifact's hash. A plain `pip install` checks neither the hashes nor the spaCy
model wheel, which comes from a GitHub release URL. To install exactly the
locked artifacts, use `uv sync --locked --extra search --extra memory` from a
checkout.
