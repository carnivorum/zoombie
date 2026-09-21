# Plan: token economy for agentic usage

## Goal

Reduce what the repo costs an agent to read, without weakening determinism or
losing a constraint that prevents a real regression. Three targets, in order of
recurring cost:

1. **Skills** - loaded every time a skill triggers.
2. **The Zoombie role prompt** - billed on every turn in that mode.
3. **Code comments** - read during any task that touches `scripts/`.

"Progress-style" comments, meaning commentary that records how the code came to
be rather than what would break if it changed, are removed. Constraints stay.

## Decision locked

Skill de-duplication uses **template substitution**, not literal deletion.

- One canonical copy of each shared block lives in `skills/_shared/<name>.md`.
- Each `SKILL.md` source marks the include site.
- [`skills.deploy()`](scripts/zoombie/lib/skills.py:60) expands the markers when
  it writes the deployed copy, so the six skills installed under
  `%USERPROFILE%\.roo\skills\` stay fully self-contained - the agent never sees a
  marker and never has to resolve an include at runtime.
- Tests assert each marker appears exactly once per skill and that the expanded
  output is clean.

The repo source therefore stops paying six times for the same paragraph while the
deployed artifact keeps today's behaviour, including the `%PUBLIC%` fallback that
a non-ASCII user profile depends on.

## Why not literal deletion

The blocks being removed encode two behaviours that are easy to lose and
expensive to lose:

- the CLI lives under `%PUBLIC%` instead of `%USERPROFILE%` when the user name is
  not ASCII, and
- the SRT is the only timing source, so `-NoSrt` destroys downstream timestamps.

Prose that names those facts works; a reference to a README section that the agent
may not load does not. Template substitution removes the duplication without
removing the fact.

## Shared blocks

Four blocks, canonical text in `skills/_shared/`:

| Block | Content | Copied today |
|-------|---------|--------------|
| `cli-resolve` | the `$cli = @(...) Where-Object ... throw ...` snippet and the `%PUBLIC%` reason | 6 |
| `json-contract` | the one-line `{ ok, action, data, error }` contract on stdout | 6 |
| `repo-fallback` | how to run the same subcommand from the repo when no launcher is installed | 5 |
| `shell-note` | the launcher is a `.cmd` shim, so any shell or process spawn works | 6 |

### Marker syntax

In the repo source only:

```markdown
<!-- zoombie:include cli-resolve -->
<!-- /zoombie:include -->
```

Rules for the expander:

- The whole region, both marker lines included, is replaced by the block body.
  The deployed file carries no marker.
- The repo file keeps the markers, so the source stays diffable and the
  duplication is visible rather than implied.
- An unknown block name is a **failure**, not a silent skip: a typo would
  otherwise ship a skill with a missing CLI-resolution step.
- An unclosed marker is a failure.
- The front matter is never touched, so the version marker stays inside the first
  12 lines that [`read_marker`](scripts/zoombie/lib/skills.py:35) inspects.

### Deliberately not shared

Anything whose text differs per skill stays in the skill:

- the concrete `& $cli <subcommand> ...` line, because
  [`test_every_referenced_cli_flag_exists`](tests/test_skills.py:151) reads the
  flags from it per subcommand;
- the one line naming which `data.*` fields that skill reads;
- the per-skill `Notes` and `Procedure` sections.

`repo-fallback` is kept generic on purpose - it states the rule once instead of
repeating a five-variant `python -m zoombie <sub>` snippet that the skill's own
command line already implies.

## Work items

### 1. `skills/_shared/` and the marked sources

Create the four block files. Replace each duplicated region in the six
`SKILL.md` files with its marker. Delete the per-skill duplicates of the
repo-fallback command snippet, keeping the rule.

### 2. `skills.deploy()` expansion

Extend [`deploy()`](scripts/zoombie/lib/skills.py:60) to read the source, expand
includes against `skills/_shared/`, and write the expanded text. Deployment must
still report `created` / `up to date` / `updated` by comparing the **expanded**
text against what is already installed, so idempotency survives the change. The
version comparison is unchanged.

Add a small `expand_includes(text, shared_dir)` helper next to `deploy`, exported
so the tests can call it directly.

### 3. Guards in `tests/test_skills.py`

- every include name a skill uses has a file in `skills/_shared/`;
- every include appears at most once per skill;
- markers are balanced in every skill;
- no skill body contains a shared block's text verbatim, which is what catches a
  re-pasted block;
- `expand_includes` output contains no `zoombie:include` remnant;
- an unknown include name raises;
- `expand_includes` is idempotent on already-expanded text;
- a per-skill byte budget, so the next edit that re-inflates a skill fails the
  suite instead of being noticed by an agent paying for it.

### 4. Documentation contradictions

Three concrete defects found in this review:

- `setup.md` carries **two** `### The Zoombie role` sections. The stub at the
  second occurrence duplicates the first; delete the stub and keep the section
  with the permissions table.
- `setup.md` says `cvrm-zoombie-version: 4.3.0` in two places while
  [`SKILL_VERSION`](scripts/zoombie/__init__.py:27) is `4.4.0`. README is already
  correct.
- The `readpdf` `-ImageDir` row in
  [`zoombie-pdf-to-md`](skills/zoombie-pdf-to-md/SKILL.md:80) says the default is
  `<base>.images`, while the note below it correctly says the item layout
  (`.data/img`). The CLI default is the item layout; fix the row.

### 5. Role prompt compression

[`modes/zoombie.yaml`](modes/zoombie.yaml:28) is the always-on cost.

- `roleDefinition` states "you are not a software engineer, hand it to Code"
  three times (intro, `Writing.`, `Delegation.`). Fold into one statement.
- The Criticism rule appears in `roleDefinition` and again in
  `customInstructions` step 4. Keep one.
- `customInstructions` restates the item model (folder shape, `.data/`,
  `item.json` fields) and the block-6-is-a-copy rule, both owned by
  [`zoombie-summarize`](skills/zoombie-summarize/SKILL.md:18). Replace with a
  pointer to that skill.

Keep, because they are correctness guards rather than restatement:

- `nextNumber` comes from the scan and is never invented;
- `zoombie items` is read-only and is the one sanctioned way to ask what a
  workspace holds, rather than enumerating directories;
- the `fileRegex` edit restriction and the "no JavaScript, inline CSS" HTML rule.

Add a test asserting the role's prompt fields stay under a character budget, so a
future edit cannot silently double the per-turn cost.

### 6. Comment pruning in `scripts/zoombie/`

The PowerShell implementation these comments contrast against **has already been
deleted** - `scripts/` holds only `bootstrap.cmd`, `bootstrap.ps1`, `pdf/` and
`zoombie/`. A comment explaining what the code is *not* now has no referent.

Remove the migration framing from, at least:

| File | Comment being pruned |
|------|----------------------|
| [`process.py`](scripts/zoombie/lib/process.py:62) | "byte-comparable ... during the port"; child_env "The PowerShell original toggled PYTHONUTF8" |
| [`paths.py`](scripts/zoombie/lib/paths.py:190) | "the main simplification over the PowerShell original" (keep the `\\?\` leak warning) |
| [`download.py`](scripts/zoombie/lib/download.py:3) | the `curl.exe` per-file story (keep: resume exists) |
| [`hardware.py`](scripts/zoombie/install/hardware.py:3) | the `Get-CimInstance` comparison (keep: why `wmic` is avoided) |
| [`manifest.py`](scripts/zoombie/lib/manifest.py:67) | "Replaces the PowerShell Get-ManifestValue/Get-Field pair" |
| [`env.py`](scripts/zoombie/lib/env.py:3) | "Replaces the PowerShell Get-Environment/$Env hashtable" |
| [`archive.py`](scripts/zoombie/lib/archive.py:3) | the `Expand-Archive` comparison |
| [`whisper.py`](scripts/zoombie/lib/whisper.py:131) | "the one place the PowerShell version needed a heuristic NUL-strip" |
| [`cli.py`](scripts/zoombie/cli.py:3) | "unchanged from the PowerShell CLI, so no caller has to change" |
| [`install/main.py`](scripts/zoombie/install/main.py:4) | the `$script:DryRun/$script:Check` globals note |
| [`install/components.py`](scripts/zoombie/install/components.py:9) | the same globals note |
| [`__init__.py`](scripts/zoombie/__init__.py:11) | "the PowerShell original was far too verbose" |

Also remove the module-docstring rationale that duplicates README or `setup.md`,
chiefly the long ASCII-root and 260-character explanation in
[`paths.py`](scripts/zoombie/lib/paths.py:1), which README already covers in
full.

**Keep** a comment when it names a live trap that a future edit could undo:

- a `\\?\` path must never reach JSON output or a native tool call;
- a bare `customModes:` is not an empty list when items follow it;
- `read_marker` only reads the first 12 lines;
- the pipeline deliberately exposes no `-Format`;
- the anchor/heading rules in `markdown.py`, which the block-3 criticism
  sub-block depends on.

### 7. Rule in README

Add a short bullet to the Hacking section naming what a comment must earn to
stay: it must prevent a realistic future regression or explain a non-obvious
constraint. History, restatement and "this used to be X" do not qualify.

### 8. Version bump

`SKILL_VERSION` `4.4.0` -> `4.5.0` (skill content changed) and `ROLE_VERSION`
`1.1.0` -> `1.2.0`. Update the six `SKILL.md` markers and the version strings in
`setup.md` and `README.md`.

## Verification

- `python -m pytest tests` passes, including the new include and role-budget
  guards.
- `python -m zoombie.selftest` passes.
- `python -m zoombie.install` then a second run reports the skills and the mode
  as `up to date`, and the deployed `SKILL.md` files contain no `zoombie:include`
  marker.
- The deployed `zoombie-summarize` still carries the `%PUBLIC%` fallback.
- In the Zoombie mode, writing a `.md` succeeds and writing a `.py` is refused.

## Deliverable metrics

Measured when the work landed:

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Total bytes across the six `SKILL.md` sources | 42513 | 38148 | -4365 (-10.3%) |
| Largest `SKILL.md` (zoombie-summarize) | 10920 | 10382 | -538 (-4.9%) |
| Smallest `SKILL.md` (zoombie-extract-audio) | 3122 | 2358 | -764 (-24.5%) |
| Role `roleDefinition` characters | 2632 | 2077 | -555 (-21.1%) |
| Role `customInstructions` characters | 4248 | 4464 | +216 (+5.1%) |
| Role prompt total (both fields, per turn) | 6880 | 6541 | -339 (-4.9%) |
| Duplicated shared-block lines in the repo | ~50 | 0 | -50 |

The source-side saving is the visible half. The larger effect is that
`customInstructions` no longer restates the item model and the block-6 rule that
`zoombie-summarize` already carries: the role defers to the skill, so the same
text is not billed twice in one session and a change to that rule is made in one
place.

`customInstructions` grew slightly because two clauses the existing suite asserts
were restored in compressed form rather than dropped; the net is still negative,
and `TestPromptSize` now holds both fields to a ceiling so the next edit cannot
silently re-inflate the per-turn cost.

The repo's shared blocks are not "saved" so much as moved: the deployed skill
still carries all four inline (verified: no marker in the output, `%PUBLIC%`
fallback present). What the repo stops paying for is the sixfold copy.

## Verification results

- `python -m pytest tests` - 492 passed, including the new include, budget and
  prompt-size guards.
- `python -m zoombie.selftest` - PASS end to end: GPU path (`deviceUsed=cuda`,
  `realtimeFactor=0.6178`), deliberate `-NoGpu` CPU run, Cyrillic destination
  path, PDF to Markdown with image sidecar, and `postprocess -Apply` byte
  idempotency.
- A deploy of `skills/` wrote six expanded files with no `zoombie:include` marker
  and the `%PUBLIC%` fallback intact; a second deploy reported `up to date` for
  all six.

## Out of scope

- Any change to CLI behaviour, flags or the JSON contract.
- Any change to the deployed skill semantics; the expansion must be
  byte-equivalent to today's hand-written text.
- Rewriting the role's analytical method - only its duplicated phrasing.
