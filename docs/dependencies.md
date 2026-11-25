# What fleetwatch depends on

Generated from `fleetwatch/dependencies.py` by `fleetwatch doctor --inventory`; a test keeps them in step.

`fleetwatch install` installs and starts every scheduled job and creates the folders, and retires the older jobs each one replaces: it unloads them and moves their plist into `legacy-agents/` in the state folder, never deleting it. `fleetwatch uninstall` stops and removes the jobs. `fleetwatch doctor` checks every row below and exits 1 when something is wrong; `fleetwatch doctor --fix` installs what is missing and retires older jobs, and reports the rest.

## Scheduled jobs

| Job | What it does | Runs | launchd label (macOS) | systemd unit | Its own install | Replaces |
|---|---|---|---|---|---|---|
| index refresh | refreshes every registered session index (`fleetwatch index refresh --all`) | every 30 minutes | `local.fleetwatch.index-refresh` | `fleetwatch-index-refresh.timer` | `fleetwatch index schedule install` | `local.search-indexes.refresh` |
| watch run | checks GitHub for stopped sessions' watches (`fleetwatch watch run`) | every 15 minutes | `local.fleetwatch.watch-run` | `fleetwatch-watch-run.timer` | `fleetwatch watch schedule install` |  |
| notify | desktop notifications when something new needs you (`fleetwatch notify`) | kept running | `local.fleetwatch.notify` | `fleetwatch-notify.service` | `fleetwatch notify schedule install` |  |
| archive | copies Claude Code transcripts into the archive before the retention sweep removes them (`fleetwatch archive run`) | every 60 minutes | `local.fleetwatch.archive` | `fleetwatch-archive.timer` | `fleetwatch archive install` | `local.claude-session-archive` |

Doctor checks each job: installed, loaded (a kept-running job: running), the last run no older than 2 times its interval, no older job it replaces still installed or loaded, and no two loaded jobs running the same fleetwatch command.

## Folders

| Folder | What it holds | Where | Created by install |
|---|---|---|---|
| data folder | indexes and fleetwatch's own data | $XDG_DATA_HOME/fleetwatch, else ~/.local/share/fleetwatch | yes |
| state folder | job logs, notification state, the archive's last run, retired job backups | $XDG_STATE_HOME/fleetwatch, else ~/.local/state/fleetwatch | yes |
| session archive | transcript copies that outlive Claude Code's retention sweep | CLAUDE_SESSION_ARCHIVE_DEST, else ~/.local/share/claude-session-archive/archive | yes |
| Claude projects folder | the transcripts fleetwatch reads (Claude Code writes them) | FLEETWATCH_PROJECTS_DIR, else ~/.claude/projects | no (read only) |

Doctor checks that each folder exists and that fleetwatch can write the ones it creates.

## Programs

All optional: a missing one is a warning, and doctor still exits 0.

| Program | Used for | Without it | Platforms |
|---|---|---|---|
| `claude` | resuming a session; the session tagger during an index refresh | Resume cannot reopen a session, and a refresh needs --no-tag | all |
| `gh` | watches (read-only GitHub calls with your login) | watches report a lookup error | all |
| `rsync` | the archive copy | the archive copies with Python instead | all |
| `osascript` | iTerm2 jumps and notification banners | Enter cannot reach iTerm2 and no banner shows | macOS |
| `terminal-notifier` | a click on a notification raising its session | banners still show through osascript; a click does nothing | macOS |
| `notify-send` | desktop notifications | notifications print one line instead | Linux |
| `pdftotext` | reading PDF files in reference folders | those files are skipped | all |
| `pandoc` | reading Office files in reference folders | those files are skipped | all |
| `soffice` | reading older Office files in reference folders | those files are skipped | all |
| `tesseract` | reading scanned files in reference folders | those files are skipped | all |

## Other checks

- **The archive:** the last archive run (from `archive/last_run.json` in the state folder), and coverage: a live transcript last written before the last run started that has no archive copy is a failure.
- **Indexes:** an index whose scheduled refresh has failed 3 runs in a row, or for 6 hours, is named with its last error's first line. `[alerts] index_failure_runs` and `index_failure_hours` in settings.toml change the limits. The same streak shows on the board's Needs you tab and is sent once per streak by `fleetwatch notify`.
