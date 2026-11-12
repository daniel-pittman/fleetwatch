# How fleetwatch works

This page is reference for how the pieces fit together, what the privacy scrubber does to your
text, and how trust groups decide who may read which index. The [README](../README.md) covers
every command and setting.

## Architecture

fleetwatch has three layers. Each one reads less than the one above it needs to know.

<p align="center">
  <img src="images/architecture.svg" width="780" alt="Architecture diagram. Three inputs on the left: each session, which writes one small status file with fleetwatch write-status; Claude Code, which provides the session registry, claude agents --json and the transcripts; and reference folders of Markdown, PDF and Office files. The status file and the registry feed layer 1, the board, a Textual app with the tabs Needs you, Your to-dos, Sessions, Resume and Indexes, which feeds you through the hotkey overlay, where Enter jumps to a session's iTerm2 tab. Transcripts and reference folders feed layer 2, session memory, which parses, scrubs, tags and refreshes, and keeps the registry and trust groups. It sends scrubbed text to an optional tagger, a small Claude model, and passes a scrubbed corpus to layer 3, the search engine: SQLite FTS5, embeddings and a cross-encoder reranker, which answers fleetwatch search for a session or for you.">
</p>

1. **The board** reads Claude Code's own records (the session registry, `claude agents --json`,
   and the transcripts) and the status file each session writes. It decides which sessions are
   alive, which are stuck on a prompt, and what each one needs from you. It reads only; the one
   folder it ever writes to is the status folder, through `write-status` and `prune`.
2. **Session memory** reads transcripts and reference folders, scrubs them, and writes a corpus
   of Markdown files for the engine. It owns the index registry and the trust groups.
3. **The search engine** (`fleetwatch.search.engine`) is a plain library over SQLite FTS5,
   sentence-transformer embeddings and a cross-encoder reranker. It works on any folder of
   Markdown and knows nothing about sessions.

### How the parts connect

- **A session** writes its status with `fleetwatch write-status`. The command finds the calling
  session by walking up the process tree to the first process with a registry entry, stamps the
  name, session ID and time, and writes `~/.claude/state/fleetwatch/<name>.json`. The board joins
  the file to the live session by session ID first and by name second, so a renamed session keeps
  its file.
- **The index registry** (`<config>/indexes/registry.yaml`, plus one file per index in
  `registry.d/`) names every index, its trust group, its owner session and its transcript
  folders. `fleetwatch index add` creates an entry for the calling session.
- **An index** has configuration (tag taxonomy, probe questions, scrub allowlist) under
  `<config>/indexes/<name>/` and data (corpus, search database, state) under
  `<data>/indexes/<name>/`. `fleetwatch index refresh` parses the transcript into exchanges,
  scrubs them, optionally tags them, and rebuilds the search database when something changed.
- **The board** reads the registry and each index's state only to show the Indexes tab. It shows
  statuses and counts, never what an index holds, and it never refreshes one. Each row names the
  owner session from the registry, and Enter on it jumps to that session's iTerm2 tab or resumes it
  through the same path as the Resume tab. A fresh refresh lock marks an index as refreshing, and
  an index that a running refresh has not reached yet is waiting, not stale.
- **Watches** are standing GitHub checks a session declares in its status file, each one kind from
  a fixed menu on one repo. `fleetwatch watch run`, on a 15-minute launchd agent or systemd timer,
  reads every status file, skips the sessions that are active (live, with a status newer than 1.5
  times their cadence), and checks the rest through read-only `gh api` calls. It keeps one state
  file per owner session under fleetwatch's state folder, holding item numbers, states, counts and
  times, never a title, body, branch, check name or login. The board reads those files for its
  Watches section on Needs you and never calls GitHub; a session that resumes reads its own flags
  with `fleetwatch watch report` and clears them with `fleetwatch watch ack`.
- **A search** (`fleetwatch search`) finds the calling session's own index from process ancestry,
  checks the trust guard when you name another index, and hands the query to the engine.

The board loads the memory layers only for the `search` and `index` commands, so `fleetwatch`
and `write-status` start without importing the model libraries.

## What the scrubber keeps

Every transcript exchange and reference document is scrubbed before anything is written to an
index, and before any text goes to the tagger. This is a fictional message before and after:

```
Sent:    Send the vendor contract to Jordan Lee at jordan.lee@example.com or
         call 303-555-0142. The vendor login is user 4471923 and the key is
         sk-ant-api03-AbCd...(a fictional key)
Stored:  Send the vendor contract to [name] at [email] or call [phone]. The
         vendor login is user [id] and the key is [secret].
```

The index records how many of each category it replaced (here one each of `secret`, `email`,
`phone`, `numeric-id` and `person-name`), never the values.

The scrubber combines patterns with spaCy's English name recognizer. A core rule set is always
on: secrets and keys, email addresses, phone numbers, numbers beside an id word (`user 4471923`),
and person names. Rule packs add to it. The `education` pack ships with fleetwatch and knows the
id words of student systems. You can write your own packs as YAML files in your config folder and
list them in `settings.toml` under `[scanner] rule_packs`. A pack can define id patterns,
anonymous-id formats with a rule that drops any exchange pairing one with a name, private paths
to flag, and spreadsheet columns to drop on conversion.

Each index has an allowlist (`scan_allowlist.yaml`) for the addresses and names that may stay,
such as colleagues or project names that look like people, and a denylist stored as sha256 hashes
of name tokens, so the names it holds are never written to disk. `fleetwatch index triage NAME`
lists person-name candidates the allowlist does not cover, as counts.

The scrubber is good, not perfect. spaCy misses some names, and no pattern list knows every key
format. An all-capital surname is redacted when it sits beside a name in normal case, a title or
a roster comma ("Jean DUPONT", "DUPONT, Jean"), is on the 2010 US Census surname list, and is not
a known acronym. A name written wholly in capitals, an all-capital surname on its own, and a
surname that is also a common acronym stay in the text. Where names must stay out of an index,
give the index a denylist, which matches a name in any case.

## Trust groups

Every index belongs to a group, such as `work`, `research` or `personal`. The registry declares
the groups and says which groups may read which. A session's own group is the group of its own
index. A session always reads its own index. When it runs `fleetwatch search --index X`, fleetwatch
checks that its group is among X's group readers, or that the session is named in X's
`extra_readers`, and otherwise refuses and names the rule.

Declare groups in `registry.yaml`. These three cover the usual cases:

```yaml
groups:
  team:     {readers: ["*"]}               # shared: every session may read it
  work:     {readers: [work]}              # private: only work sessions
  personal: {readers: [personal, work]}    # work sessions may also read personal indexes
  ferpa:    {readers: []}                  # protected: readers never apply
```

- **Shared.** `readers: ["*"]` opens a group to every session. Use it for team lessons learned,
  a methodology library or project notes.
- **Private.** A group that lists only itself is readable by sessions whose own index is in that
  group. List another group to let it in one direction only, as `personal` does above.
- **Protected.** A group named in `settings.toml` `[groups] protected` (default `ferpa`, after the
  US student-records law) is readable only by its owner session and the sessions in the index's
  `extra_readers`, whatever the group's `readers` say. The refusal reads:

  ```
  fleetwatch search: refused: index 'advising-notes-session' is in the protected group
  'ferpa', which only its owner session and the sessions in its extra_readers may read;
  group readers never apply. To allow it, the owner adds your session to extra_readers in
  that index's registry entry.
  ```

A protected group also scrubs more. `index add` refuses to create an index in a protected group
until the enabled rule packs define both `id_patterns` and `anon_ids` (the built-in `education`
pack defines neither, so this takes a pack file of your own), and a protected index refuses
`keep_patterns` and `person_names: false` in its allowlist.

Every `index` command that names an index runs the same check, and `index status` with no name
lists only the indexes the calling session may read. At a plain terminal, outside any session,
you are the owner and may read every index.

This is a guard against mistakes, not a security boundary. The index files are ordinary files
that anyone who can read your disk can read. What it prevents is the realistic failure: a session
working on one project searching its way into another project's records because a search
returned them.
