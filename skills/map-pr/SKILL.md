---
name: map-pr
description: Keep a live briefing page for a repo's open pull requests, so a reviewer sees the context, the size and the risks of each one before reading code. For every PR it shows how many lines need a close read (moved code, tests and fixtures set apart), a ledger of assurances and things to watch (hot spots, widely used files, sensitive areas, missing tests, overlap with other PRs), a change map, and a review order with each test under the file it tests. Takes a PR number or a story ID. Use when the user says "map-pr", "map-pr 1100", "map-pr sc-5204", "map this PR", "brief me on the open PRs", "what's waiting for review", or asks where to focus when reviewing a large PR.
license: MIT
compatibility: Requires gh (signed in), git and python3 3.11 or later.
---

# Map Pull Requests

Keep one page per repo that briefs a reviewer on every open PR: what changed, how much of it needs a close read, and
what could go wrong.

Each run is a sync, not a rebuild. A PR is analysed once per head commit, the facts around it are refreshed, and a PR
that merged or closed moves to the archive.

## Arguments

`map-pr [target]`

- **target** (optional) is the PR to point at after the sync:
  - `1100`, `#1100` or `pr 1100`: that PR. A merged or closed PR is analysed straight into the archive.
  - `sc-5204` or `story 5204`: the PR with `sc-5204` in its title or branch name. If several match, the script takes
    the open one, otherwise the most recently updated, and names the others.
  - A bare number that isn't a PR in the repo is treated as a story ID.
  - Nothing: the PR for the current branch, if it has one.
- The repo is the current directory's GitHub repo. For another repo, pass `--repo owner/name` to the script.

## Steps

1. Run this skill's script (`scripts/map_pr.py` in this skill's folder): `python3 <skill folder>/scripts/map_pr.py
   <target>`. The first sync of a repo clones its recent history and analyses every open PR, so it can take a few
   minutes. Later syncs take seconds.
2. If it exits with code 3 and says it is not set up, this is the first run on this machine. Ask the user which
   folder the briefings should be kept in, and offer `~/map-pr` as the default. Then run the script with
   `--set-home <folder>` and run step 1 again. Ask once only: the answer is saved.
3. If it exits with any other error, report the error and stop.
4. Show the page. The script prints the path of `index.html` and, when there is a target, a link that opens straight
   to that PR's briefing. Give the user that path or link to open in a browser.
   - **Claude with the Artifact tool**, when the user wants a link to share: run the script with `--artifact`, then
     publish the page body it names with icon `chart` and the description it printed. Publish the same path again
     to refresh an earlier one.
5. Reply briefly: which PRs were analysed, reused and archived, and for the target PR the lines to read closely, the
   things to watch and the assurances, as the script printed them.

## Where things live

Everything for a repo is under one folder, `<home>/<owner>/<repo>/`:

| Path | What it is |
| --- | --- |
| `index.html` | The live page: open PRs, recently merged and closed PRs, and a briefing for each |
| `archive/<yyyy-mm>/pr-<n>.html` | The briefing for a PR as it stood when it merged or closed |
| `.state/` | One record per PR, and the skill's own clone of the repo. Safe to delete: the next sync rebuilds it |

`<home>` is the folder chosen at first run, saved in `~/.config/map-pr/config.toml`. `--home <folder>` or the
`MAP_PR_HOME` environment variable overrides it for one run. The script never writes inside the repo being analysed
or inside this skill's folder.

## The page

- **Open** lists every open PR with its size and its watch items, so the user can pick what to review first.
- **Magnitude** splits a PR's changed lines into application code to read closely, tests, config, docs, moved lines
  and generated files.
- **Risk read** asks the same questions of every PR. Each comes back as an assurance with its evidence (isolated to
  one module, every changed file has a changed test, easily reversed) or as something to watch with its files.
- **Hot spots** plots each changed file by how many files import it and how often it has changed lately. Click a
  circle to see what depends on it.
- **Change map** is a treemap of the changed files. It can be sized with moved lines left out, and coloured by kind,
  touch rate or dependents.
- **Review order** groups files into layers, foundations first, with each test under the file it tests.

Every signal is counted from the diff, git history and imports. None is a model's judgement.

## Notes

- The page is a snapshot of the last sync. Run the skill again after new commits or merges.
- `--force` analyses every PR again. `--max-prs N` changes how many open PRs are kept live (default 30, most
  recently updated first).
- Dependents are read from Python and Dart imports. Other languages get every other signal, with no dependents.
- A repo can tune the rules with an optional `.map-pr.toml` at the root of its default branch:

  ```toml
  group_roots = ["libs", "projects"]   # folders whose children are modules
  window_days = 90                     # how far back the touch rate looks
  story_prefix = "sc-"                 # story IDs in branch names and titles; "" turns the lookup off

  [areas.billing]                      # add a sensitive area, or change a built-in one by its key
  label = "Billing & entitlements"
  pattern = "billing|stripe|invoice"   # regex on the file path; "" turns an area off
  revert = false                       # true when a git revert would not undo the change
  ```

  The built-in areas are `data`, `deploy`, `deps`, `auth`, `money`, `ai` and `contract`. File kinds come from path
  rules in `kind_of` in the script.
- The page lists the repo's paths and PR titles, so share it only with people who may see them.
