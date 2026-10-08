<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# ADR 0001: A clone tree records its content-filter intent

- **Status:** Accepted
- **Date:** 2026-10-02
- **Issue:** [#307](https://github.com/lfreleng-actions/gerrit-clone-action/issues/307)

## Context

Content filtering (`--remove-files`, `--git-filter`, `--redact-secrets`)
removes files and secrets from cloned history, usually before `mirror`
pushes it to GitHub. Until this decision, each run filtered with its own
options only. Leaving an option out on a later run undid the filtering:

- A refresh fetches upstream history over the filtered history.
- `mirror --overwrite` deletes each project directory, clones it again
  and pushes it with `git push --mirror`, replacing the filtered GitHub
  repository with unfiltered history. A project directory deleted by
  hand meets the same fate.
- A project created upstream after the original clone, or a file that
  appears upstream later, gets no filtering at all.

PR #305 added a per-repository record, in the repository's git config,
of the filters that **rewrote** it. That record answers whether a
refresh is safe, but it records an *effect*: the tool withdraws a filter
that changed nothing, so the record cannot say what the operator
*decided*. It also disappears with the repository.

## Decision

Two layers, each authoritative for one question.

<!-- markdownlint-disable MD013 -->

| Layer                                                        | Holds                                     | Answers                                 | Lifetime                           |
| ------------------------------------------------------------ | ----------------------------------------- | --------------------------------------- | ---------------------------------- |
| Tree-level intent, `<tree>/.gerrit-clone/filter-policy.json` | Every filter any run applied, by scope    | What the operator decided for this tree | Persistent; only grows             |
| Per-repository record (PR #305, git config)                  | The filters that rewrote this repository  | Whether a refresh would be safe         | Lives and dies with the repository |

<!-- markdownlint-enable MD013 -->

### The intent file

- **Scopes.** Each entry applies to every project (`"projects": "*"`),
  to a `--git-filter` project pattern (matched as `--git-filter` and
  `--include-projects` match), or to one project exactly
  (`"project": "com/parent"`). A project's policy is the union of every
  entry covering it.
- **Only grows.** A run adds its options to the file, and nothing
  removes anything. Dropping a filter means deleting the file *and*
  re-cloning the repositories: a deliberate act, never an omission.
- **No secrets.** Tokens appear only as SHA-256 digests, as in the
  per-repository record.
- **Fail closed.** A file the tool cannot read, cannot parse, or that
  carries an unknown `schema` stops the run before it touches any
  repository. The tool never reads it as "nothing decided".
- **Atomic.** The tool writes a temporary file, flushes it, then renames
  it over the old one.
- **Location.** The tool searches upwards from the output path for the
  intent or the journal, as git finds `.git`, and creates the file at
  the output path otherwise. Discovery already skips hidden directories,
  which keeps `.gerrit-clone/` from passing for a project.

### Each run

At the start of a run, before it clones, refreshes or deletes anything:

1. Read the intent.
2. Fold in every existing repository's own record, scoped to that
   project. This migrates trees that PR #305's release filtered, and it
   lets `mirror --overwrite` delete a repository without losing what it
   held.
3. Add the run's options.
4. Write the result, unless the run is a dry run, or a refresh of a path
   that does not exist. A run writes under the tree's lock,
   `.gerrit-clone/filter-policy.lock`, held with `flock` (`msvcrt.locking`
   on Windows): it takes the lock, reads the intent again, adds its
   additions and writes. Two runs on one tree, such as a `refresh` and a
   `mirror`, never lose each other's additions. A run waits up
   to two minutes for another to release the lock, then stops with an
   error. A run with nothing to add, and a dry run, never take it.

A run also holds the lock while it publishes a filtered result: a staged
refresh's refs, or a repository `clone` or `mirror` filtered in place.
First it checks that its filters still cover the intent, which another
run may have extended since this one read it. If they do not, the
repository stays unchanged and counts as failed, so `mirror` pushes
nothing; the next run filters it under the larger intent. `mirror`
pushes to GitHub after that check, so two `mirror` runs pushing from one
tree at once remain unsupported.

The tool never writes under `.gerrit-clone/` through a symbolic link,
which a checkout could track to aim those writes outside the tree.

The run then filters every repository with the union of its options and
the intent for that project, through one shared step
(`content_stage.filter_repository`) that `clone`, `refresh` and `mirror`
all use. A refresh stages every mirror the run filters, including ones
the filters have not rewritten yet: it fetches into a copy, filters the
copy, and publishes only if both succeed, so a failed filter never
leaves new unfiltered content in the mirror.

A working copy (`--no-mirror`) the run filters gets the same treatment,
after the usual working-copy preparation. Its copy, kept inside the
checkout's git directory, holds the checkout's remote-tracking refs as
branches, so both filter methods rewrite them and `git filter-repo` has
no `origin` refs to fold into local branches. The copy fetches and gets
filtered. The checkout then takes the copy's refs as its remote-tracking
refs and tags, in one atomic fetch, and fast-forwards or rebases as a
pull would, without fetching again. It does so only if the filtered
upstream extends what the checkout holds: filtering the same content
gives the same commits, so a checkout a staged refresh rewrote follows
it, while one whose own history the filters would now rewrite gets
refused. Under the publication lock, the refs publishing replaces must
also still stand as they did when the refresh made its copy, for a
mirror and a working copy alike, so it never rolls back another refresh
that published meanwhile. Any failure before that leaves the checkout
unchanged, with any stash put back, and the refresh never filters the
checkout in place.
Without `git filter-repo`, the worktree fallback adds its removal commit
on upstream's tip, which no later refresh could extend, so a working
copy it would rewrite gets refused, with a pointer to install it.

The copy stands in for upstream alone. A refresh filters what it
fetches, so it never brings unfiltered content into a checkout; it does
not filter the checkout's own branches, or the local commits a rebase
replays. The operator made those, and the checkout held them already.
Refusing the refresh would not clean them, and rewriting them in place
is what left checkouts unrefreshable. A checkout the filters rewrote
blocks pushing, as every filtered repository does.

A working copy filtered in place, as `clone --no-mirror` does, has lost
its remote-tracking refs to `git filter-repo`, and stays refused;
[#317](https://github.com/lfreleng-actions/gerrit-clone-action/issues/317)
tracks filtering new working copies through a copy too.

### Journal

`<tree>/.gerrit-clone/filter-journal.jsonl` records the runs that
carried the intent out. Each rewrite, through the one shared step,
appends two JSON lines:

- **Before it starts:** the command and release, the project and
  repository, the remote URLs it fetches from, without user information,
  query or fragment, the method (`git filter-repo` or the worktree
  fallback), the filters, with tokens as digests only, and a SHA-256
  digest of every ref. The tool flushes this line to disk first, and
  does not filter a repository it cannot journal.
- **Once it ends:** whether it worked, and the digest of the refs it
  left.

The intent stays the decision and the journal an audit trail, with one
exception. A start without a successful end, from a crash or a rewrite
that failed part-way, may have left its repository partly rewritten. Its
filters stay in force, added to the intent at the next run, until a
later rewrite of that project completes under filters that cover them.
Rewrites of one project never overlap: each holds a per-project lock,
under `.gerrit-clone/locks/`, from its start line to its end line. So a
start without an end, earlier than a later rewrite's start, belongs to
a run that crashed, never to one still at work.

Every entry carries a `schema`, and one from another release stops the
run. The ref digests are the journal's evidence, so the tool never
leaves one out: if it cannot read the refs before a rewrite, it does
not filter the repository, and if it cannot read them after, it writes
no end, which leaves the start binding.

Appends take the tree's lock, so lines from different runs never
interleave. A crash while writing leaves a partial last line: readers
skip it and the next append removes it, since no rewrite followed it.
Any other line the tool cannot read stops the run, as an unreadable
intent does.

### Tokens

`--remove-files` and `--redact-secrets` re-apply from the intent alone.
A `--git-filter` token cannot: the intent holds its digest, not the
token. The tool **refuses** a project whose intent includes tokens the
run does not supply, comparing digests: a refresh skips it before
fetching, and the `clone` and `mirror` filter stages count it as failed,
which makes `mirror` abort the batch and push nothing. The operator passes the
tokens again, as for the original clone.

Alternatives rejected: storing tokens, which defeats the purpose; and
storing a reference to where tokens live, such as a secret name, which
couples the tool to one secret store and still cannot verify the value.

### Repositories filtered by v2.2.4 and earlier

Unchanged from PR #305: the tool recognises them by their traces and
refuses them, with re-clone as the remedy. Those releases never recorded
their filters, so neither the intent nor any check of current history
can show that a later run re-applies them. An "adopt" path fails for the
same reason: it could check that history now lacks the paths an operator
names, but not that those cover what those releases removed.

For the same reason, `mirror --overwrite` takes one out of the run
entirely, along with any directory holding one and any repository whose
record it cannot read. Cloned again, it would go to GitHub unfiltered,
over the filtered copy; and even as it stands, `--recreate` would
replace its GitHub repository with it. The tool neither deletes, clones,
filters nor pushes it, leaves its GitHub repository alone, and reports
it as skipped with the reason, until the operator deletes it by hand and
clones it again with the filters it needs; that run then records them.

### Manifests

The `clone`, `refresh` and `mirror` manifests carry a top-level
`content_filters` block summarising the intent the run resolved: the
intent file's path, and for each scope its removal patterns, whether it
redacts secrets, and the number of tokens it replaces. It carries no
digest, since manifests often end up as public CI artifacts, and a
digest lets anyone confirm a guessed token offline. A tree without
filters reports `null`.

## Consequences

- A later run without filter options filters as the tree decided,
  including new projects and content that arrives later. Filtering what
  the tool already filtered changes nothing, but costs a filter run for
  each repository in each run.
- `mirror --overwrite` and hand-deleted repositories no longer roll a
  GitHub mirror back to unfiltered history.
- The GitHub Action usually clones into a fresh workspace, so its
  workflow inputs remain its policy record; the file mainly serves
  long-lived trees.

### Deferred

Nothing from
[#309](https://github.com/lfreleng-actions/gerrit-clone-action/issues/309)
remains deferred.
[#317](https://github.com/lfreleng-actions/gerrit-clone-action/issues/317)
tracks filtering new working copies through a copy.
