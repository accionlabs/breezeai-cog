---
name: gap-review
description: >
  Review a breezeai-cog parser — a branch or PR that adds or changes a parser or detector, or
  an existing language/framework — and write a gap analysis report, following
  docs/parser-review-guide.md. Checks scope, structure, the extend-capture rules, claims,
  false positives, pass budget, coverage against official references and the capture
  contract, and a real capture run. Every finding is evidenced with source, what is captured
  and what should be. Use for "review this parser", "gap analysis for <framework>", "what does
  cog miss in <repo>", or as the self-review before opening a parser PR.
---

# Gap Review — breezeai-cog

This skill runs the review in [`docs/parser-review-guide.md`](../../docs/parser-review-guide.md).
The guide says **what** to check and what counts as a failure; this skill says **how to run
it**: the order, the commands, and the rules that keep findings trustworthy. Read the guide's
section before each step — this file does not repeat its tables.

The rules being checked are in [`skills/extend-capture/SKILL.md`](../extend-capture/SKILL.md).
The capture contract — what *can* be captured — is
[`src/breezeai_cog/schemas/capture.py`](../../src/breezeai_cog/schemas/capture.py) and
[`enums.py`](../../src/breezeai_cog/schemas/enums.py).

It is tool-neutral: "ask" means whatever review channel your setup provides.

---

## 1. Rules for the reviewer

A review is read as fact by whoever fixes the parser. The same bar applies as to the parser:
a wrong finding is worse than a missing one.

- **Evidence or it isn't a finding.** Every finding cites `file:line` in the source repo (and
  in cog for a structural issue) and shows what is captured today and what should be — read
  from the capture, not inferred from the parser code.
- **Verify before you report.** Re-open the source line and the captured record for every
  finding. Check it is not already handled elsewhere (an additive detector, another parser,
  the ignore list) and is not intended by a documented decision (the entry-point contract,
  the role table, statement-model). A documented decision you disagree with is an "Other"
  issue that says so — not a capture bug.
- **Build the coverage list from outside the parser** (guide §7): official docs, the capture
  contract, the Target Spec. A list derived from the parser's own code only finds what it
  already does.
- **Don't decide modelling.** A fix that needs a new `semanticType`, field, node or edge goes
  into "Decisions needed" with options and a recommendation (extend-capture §2) — the reviewer
  does not pick it.
- **Don't fix during the review.** The output is the report. Fixing comes after, with the
  report's decisions made.
- **Keep the report out of the repo.** It goes outside the checkout (e.g. a sibling
  `analyses/` folder). Nothing committed may link to it or use its issue numbers (S1, C1, …);
  a committed fix states its own reasoning.

## 2. Pick the mode

| Mode | When | Guide sections |
|---|---|---|
| **Change review** | A branch or PR that adds or changes a parser or detector | §1–§6, plus §8 as a before/after diff on one repo per affected language; Step 9 |
| **Gap analysis** | An existing language/framework, or "what does cog miss in this repo" | §2–§8 in full (§1 only if there is a diff), Step 9, and the full Part B report |

Before starting, write down: the parser(s) and frameworks in scope, the cog commit, the
repositories (name, commit, stack, size) and the framework versions. That is the report's
Scope section. If no real repository is available for the stack, say so and stop at the
static checks — never substitute a hand-written sample for a dogfood run.

## 3. Procedure

Record each finding as you go, sorted as **S**tructural, **C**apture or **O**ther (Part B §4–6).

**Step 1 — Scope (guide §1).** Sort the changed files into zones:

```bash
git diff --name-only main...HEAD
```

Anything in the language-parser, additive-detector, shared-helper or core zone raises the
review level: run the other frameworks' tests for that language and diff their capture.

**Step 2 — Structure (guide §2).** Compare the package against the layouts in the guide and
`docs/parser-reference.md` §4. Look first for the common failures listed there: a re-parse in
the framework parser, hand-built ids, a detector editing `record.statements`, a missing
`capture_statements` / fixture gate, a serial `build_index`.

**Step 3 — Rules (guide §3).** Walk the extend-capture §9 checklist item by item against the
code. Useful greps over the parser package:

```bash
P=src/breezeai_cog/parsers/<pkg>
grep -n 'parse_source' $P/*.py                    # a second parse of the whole file?
grep -n 'nodeType=' $P/*.py                       # each one a real node.type or "synthetic"?
grep -n 'authRequired' $P/*.py                    # set only by a check that rejects anonymous calls?
grep -n 'handlerPath' $P/*.py                     # written only through handler_location?
grep -n 'record.statements' $P/*.py               # a detector appending instead of returning Emits?
grep -nE 'f".*\{.*\}' $P/routes.py                # an endpoint built from a name or variable?
```

A grep hit is a lead, not a finding: read the code around it.

**Step 4 — Claims (guide §4).** For each pair of frameworks of the language, find files that
match both claim strings in a dogfood repo (`grep -l`) and check which parser won
(`FileRecord.framework`).

**Step 5 — False positives (guide §5).** For every derived value in the guide's table that the
parser produces, sample captured records and check them against the source. Look for
look-alikes: a method called `get` on a cache, a string that starts with `select`, a wrapper
called `send`. Check that the test suite has a negative case for each.

**Step 6 — Performance (guide §6).** Count the walks per file in the code. Measure on a large
repo before and after the change with the same `--jobs`; for per-item cost run
`scripts/bench_parse_scaling.py`, and check that `tests/unit/test_scaling.py` has a sample for
any new code path. Write down the pros and cons of the parser's approach (§6.3).

**Step 7 — Coverage (guide §7).** Build the element list from the framework's official docs
(note the version), `capture.py` / `enums.py` and the Target Spec, using the §7.1 checklist.
For entry points, compare the framework's documented inbound constructs with its role table in
`docs/entry-point-roles/`. Then check each element against the capture: captured, captured
wrongly, or missing.

**Step 8 — Run (guide §8).** Capture each repository:

```bash
uv run breezeai-cog repo-to-json-tree --repo <repo> --out <outside-the-repo> --jobs 2
#   → <out>/<repo>-project-analysis.ndjson.gz
#   → <repo>/.cog/<repo>-skipped-report.json, <repo>/.cog/logs/
uv run python skills/gap-review/scripts/check_capture.py <out>/<repo>-project-analysis.ndjson.gz [--framework <label>]
```

`check_capture.py` prints counts by language / framework / `semanticType`, schema errors and
entry-point contract violations with examples. It exits non-zero on a schema error or a
contract error. Then:

- compare the counts with an independent count of the source (`git ls-files`, a grep per
  route form) and explain every difference;
- explain every skipped extension in the skip report as *ignored by design* or *unsupported*;
- check the log for parse errors and timeouts;
- spot-check a few files per category (controller, service, repository, config) line by line.

For a change review, capture the same repo on `main` and on the branch and diff the two; every
difference must be intended. For anything cross-file, also compare `--jobs 1` with `--jobs 2`.

**Step 9 — Architecture conformance ([architecture §17](../../docs/architecture.md#17-decisions-at-a-glance)).**
Go through every row of "Decisions at a glance" and mark it **checked** (with the evidence),
**not applicable** (say why), or **violated** (a Structural issue). Don't skip rows: new
decisions are added to that table, and walking it is how the review keeps up with them.

Most rows were already checked by an earlier step — cite that step:

| Rows | Checked in |
|---|---|
| Parser-specific code in its own directory | Step 1 |
| tree-sitter for every language (no hand-rolled or regex parser for a language that has a grammar); framework parsers subclass; cross-cutting libraries are additive; detectors self-register, return emits, run through one runner; facts are registered; shared API / DB / query detection; route emitters skip fixtures | Steps 2–3 |
| One parser per file by `claims` + `priority` | Step 4 |
| Fixed pass budget; one parse per file | Step 6 |
| Flat statements, `nodeType` / `semanticType` separate, unknown values omitted (left `None`, never `""`), the `capture_statements` gate, construct roles, honest-null, open vs allow-listed nodes, reused vocabulary, `SCHEMA_VERSION` | Step 3 (extend-capture checklist) |
| Scan, Execution (process pool with `spawn`), Output streaming, Server | Not applicable to a parser change unless Step 1 put `core/`, `emit/` or `server/` in scope — then review them against their section |

The rows below are not covered by an earlier step. The leads find candidates; read each hit
before calling it a violation.

| Decision (section) | Violated when… | Lead |
|---|---|---|
| **Layering** (§15): parsers never import `core/`, `services/`, `server/`; one framework never imports another framework's package — shared code goes in a `*_common.py` module | `parsers/<a>_<x>/` imports from `parsers/<a>_<y>/`, or a parser imports a layer above it | grep `layering` |
| **Workers share nothing** (§4.2, §4.5) | Module-level dicts / lists filled while parsing, a `global` assignment, a cache keyed by file or directory — the result then depends on which worker parsed which file. Constant tables and an empty `PARSERS = []` are fine | grep `state` |
| **Cross-file data comes from the index** (§4.2) | Another file is read while parsing instead of through `build_index` / a registered fact. A read inside `build_index` or a fact's `collect` is fine | grep `reads` |
| **Ambiguous index entries become empty** (§4.2) | A name that maps to two declarations resolves to the first or last one | Read each `build_index` reduce: a plain `dict[name] = …` over all files, without the collapse helpers in `index_common.py`, is a hit |
| **Claims are a cheap substring check** (§4.3) | `claims()` parses, runs a regex over the whole source, or reads another file | Read `claims()` in `parser.py` |
| **Every parse has a time limit** (§4.4) | An embedded fragment is re-parsed without `ctx.parse_timeout_micros` | grep `parse` |
| **Size limits only at output, by splitting** (§4.6) | The parser cuts a statement's `text` itself, so the output split can't recover it. A cut of a derived value (a name) needs its own reason | grep `cut` |
| **Failures are isolated per file; fail loudly** (§4.5, §14) | An exception swallowed with no log (`pass` / `continue` / `return None`), so a broken file looks like an empty one | grep `except` |
| **Deterministic ids and output** (§8) | Output order depends on iterating a `set`, or on time / randomness — two runs of the same commit differ | grep `nondeterminism`; confirm by capturing twice and diffing |
| **Static, read-only** (§1) | The parser runs code, starts a process or makes a network call | grep `execution` |
| **`language` = format, `framework` = tool** (§12) | A config or data parser sets `language` to a tool (`terraform`) instead of the format (`hcl`) | `FileRecord.language` / `framework` in the capture |
| **Database schemas outside the code contract** (§12) | DDL or Elasticsearch mappings emitted as code statements instead of going through `analyzers/` | The parser's statement kinds for `.sql` / mapping files |

```bash
P=src/breezeai_cog/parsers/<pkg>
# layering — a layer above, or another framework's package (shared helpers are fine)
grep -nE 'from \.\.\.?(core|services|server|cli)\b' $P/*.py
grep -nE '^from \.\.[a-z]+_[a-z_]+' $P/*.py | grep -v _common
# state — module-level mutable containers, globals
grep -nE '^_?[A-Za-z_]+\s*(:[^=]+)?=\s*(\{\}|\[\]|set\(\)|dict\(\)|defaultdict\()' $P/*.py
grep -n 'global ' $P/*.py
# reads — another file opened by the parser
grep -nE '\.read_(text|bytes)\(|\bopen\(' $P/*.py
# parse — every (re-)parse
grep -nE 'parse_source|Parser\(' $P/*.py
# cut — slicing that may shorten a statement's text
grep -nE '\[:[A-Za-z_0-9]+\]' $P/*.py
# except — the line after each handler
grep -nE -A1 'except( [A-Za-z.]+)?( as [a-z]+)?:' $P/*.py
# nondeterminism
grep -nE '\b(random|uuid|time\.time|datetime\.now)\b' $P/*.py
# execution — processes, eval, network
grep -nE 'subprocess|os\.system|\beval\(|\bexec\(|requests\.|urllib|httpx' $P/*.py
```

Record each violated row as a Structural issue that names the decision and links its section.

## 4. Write the report

Follow Part B of the guide exactly: Scope, Capture summary (with a "Working well" note), Skipped
files, Structural / Capture / Other issues, Fix plan with "Decisions needed before coding".

- Severity is the guide's scale (Critical / High / Medium / Low). A false positive is Critical.
- Each issue uses the guide's template: problem, example (source + captured today + should be
  captured), cause with `file:line` in cog, fix options with pros and cons, recommendation.
- Keep it short and readable for someone with no context: tables for comparisons, a small
  diagram for a missing edge or flow, no tracking ids in the prose.

Finish with the guide's §9 summary checklist, each item ticked or explained.

## 5. Hand-off

Report back with:
- where the report was written;
- the counts of issues by severity, and the Critical ones in one line each;
- the decisions needed before any fix can start.

Do not open issues, tickets or PRs from the report unless asked.
