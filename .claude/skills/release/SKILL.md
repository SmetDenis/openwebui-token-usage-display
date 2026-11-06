---
name: release
description: Prepare a release of this plugin on the current branch - refresh the built-in model tables from models.dev (`make tables`), fix the context table from its report, pick the semver part by the CLAUDE.md rules, `make bump`, write the CHANGELOG section, run `make pre-commit`, then stop and report. Use when the change on the branch touches usage_display.py and is ready, or when the user asks - "/release", "подготовь релиз", "готовим релиз", "обнови таблицы моделей", "prepare the release", "refresh the model tables". Never commits, pushes, tags or publishes; committing and the PR stay with /commit and /pr-self, tagging with CI.
---

# Prepare a release

Every merge to `main` that changes `usage_display.py` is a release (CLAUDE.md, "Releasing a change" -
the rules live there; read that section now, this skill only orders the steps). Part of every release is
refreshing the plugin's offline tables from models.dev, because users without network access depend on
them and models.dev keeps adding models.

## 1. Is this a release?

```bash
git fetch --tags
python3 scripts/release.py check
```

`release check: usage_display.py changed since vX.Y.Z, but its version is still X.Y.Z: bump it` means a
release is due. A clean exit with the plugin untouched since its tag means no release: say so and stop.
(An unreleased version already bumped on this branch also passes - then continue from step 2 and rerun
`make bump` in step 3; it renumbers that section.) A
branch whose only plugin change would be the table refresh is still a release (patch) - but don't start
one just for that unless the user asked.

## 2. Refresh the tables

```bash
make tables
```

- **Prices** (`_STATIC_PRICES`) are rewritten between the `# BEGIN/END generated static prices` markers:
  first-party vendors' models of the last 4 months. Never edit that block by hand. If the fetch fails or
  selects nothing, the script writes nothing and exits 1 - report it and stop; do not keep the old block
  "for now" silently.
- Look at `git diff usage_display.py` for the block: note how many models came in and went out, and any
  price that moved a lot - those go into the CHANGELOG.
- **Context** (`_STATIC_CONTEXT_SIZES`) is hand-written. The script prints `missing` / `differs` rows for
  current first-party models. Fix the ones a chat user would hit: add or correct a family key (the longest
  matching key wins, so a specific key overrides its family), and keep open-weight families conservative
  (their window varies by host - prefer specific keys over a broad one, as the Qwen block does).
  Specialised variants (TTS, image, live, OCR, translate) may stay on their family size. Rerun
  `make tables` until only rows you chose to leave remain, and update the "last:" month in the table's
  header comment.

## 3. Pick the semver part

By the CLAUDE.md rule, from what the branch changes in `usage_display.py`: `major` breaks an install,
`minor` adds a metric, valve or capability, `patch` is anything else (a table refresh alone is a patch).
Name the part and the reason in one line. Ask the user only when the change honestly sits between two
parts (e.g. a behaviour change that might break an existing setup).

```bash
make bump PART=<major|minor|patch>
```

## 4. CHANGELOG

Under the new `## [X.Y.Z]` heading, describe the change for users - the section becomes the GitHub
release text. Follow the style of the previous sections: a short intro line with the issue link, bold
lead-ins, what a user sees, what they must do (e.g. add a `price_map` entry). Add one bullet for the
table refresh when it changed anything visible: notable new models with an estimate, models that left
the price table, corrected context sizes.

Check that the documentation still matches the change (README, `docs/plugin-internals.md`, and
`docs/community-post.md` if user-facing - its publishing is the user's manual step, so mention it).

## 5. Gate

```bash
make pre-commit
```

It must be green: lint, mypy strict, tests with the 95% coverage floor, the release check (version in
four places, CHANGELOG section not empty, above every tag) and the valve-compatibility check against
every release tag. Fix causes, not tests; a failing valve-compat test means a valve was narrowed -
see CLAUDE.md, never ship that.

## 6. Stop and report

Report: the version and why that part; what the price refresh changed (in/out counts, notable rows);
which context rows you edited and which report rows you left on purpose; the gate result. Then stop.
Committing, the PR and the merge happen only when the user asks (`/commit`, `/pr`, `/pr-self`); CI
creates the tag and the GitHub release after the merge - never create them by hand.
