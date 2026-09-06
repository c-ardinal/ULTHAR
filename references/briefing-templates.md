# Briefing Templates — fill-in-the-blank

Read this file before writing any dispatch prompt. Use these as literal templates:
copy, fill the blanks, dispatch. A weak model reproduces the commander's judgment more
reliably from a filled template than from freeform prose — use the structure even when
it feels like overkill for a small task.

## Creator Brief (S/M/L)

**UI-design classification gate (2026-09-01, per owner instruction — after the rule was
missed for many consecutive rounds).** Before filling this template, classify the unit:
does it CREATE or RESTYLE any user-visible UI (new components, layout, colors,
typography, controls, states)? Geometry-only and bug-fix rounds are exempt, but the
FIRST round that introduces new UI re-classifies the task. If yes, the PM loads the
project's design skills FIRST (whichever the project's CLAUDE.md names) and embeds the
resulting directives (palette/typography tokens, component semantics, states, motion,
anti-patterns) into the brief's Design directives block. A UI brief without that block
is an incomplete dispatch: what comes back is an undifferentiated grid of plain boxes,
and the owner rejects it on sight.

```
Design directives (UI units ONLY — distilled from the design skills, not improvised):
  - Aesthetic direction: <1 line, tied to the site's world view>
  - Tokens: <colors/typography/spacing to use — named, not invented>
  - Component semantics: <e.g. chips are <button aria-pressed>, active state visible,
    44px targets, wrap not clip>
  - States: <hover/focus-visible/active/empty/loading with honest content>
  - Motion: <timings/easing, reduced-motion>
  - Anti-patterns: <the skill's do-not list relevant to this unit>
```

**Standing design law (owner, 2026-08; moved here from project CLAUDE.md 2026-09-01 —
the owner trusts the skill, not CLAUDE.md, to enforce discipline). Every UI brief also
carries these five rules verbatim, and the 監査役 checks them:**

1. 「AI 謹製」と一目で分かるデザインは NG — no thoughtless Inter/Roboto/system fonts,
   no purple-gradient-on-white, no generic card+shadow rows, no personality-free
   default layouts.
2. 「羅列」ではなく「構造化」 — same-kind actions/info never rendered as a row of bare
   buttons/text; aggregate into a single entry point with progressive disclosure
   (e.g. one ＋ button → picker, not N type buttons).
3. UI 機能は実装前にインタラクション仕様を提示し、分岐点は AskUserQuestion で先に
   引き出す — gestures name direction/range/end-meaning/visual feedback/copy/PC-SP
   diff/reduced-motion up front, referencing established UX patterns; never backfill
   "professional obvious" elements from owner feedback.
4. 読み込み中はスピナー、スケルトン禁止 — section headings stay visible + shared ring
   spinner; static layout-reserving placeholders are fine; content-mimicking shimmer
   is not.
5. Design-skill plugins are checked for freshness before use
   (`claude plugin update <name>@<marketplace>` if stale).
Goal: <1-2 sentences, what this unit delivers>
File scope: <explicit, disjoint list of files/dirs this agent may create or edit>
  Do NOT edit: <shared files this agent must leave alone — router, shared tokens, other
  features' folders>
Invariants — must be IDENTICAL before and after, inside the files you may edit
  (2026-08-25, gate-protocol.md § 8 "Invariants"):
  <list the existing appearance/behaviour this change must not disturb — e.g. "the
  carousel's DOM order and card positions", "every font-size on this screen", "the
  share icon's shape", "existing data in src/data/<dataset>.ts">
  For each one, your done-report states HOW you confirmed it survived (diff lines,
  measured values, unchanged snapshot). Silence about an invariant is a finding.
  If you believe an invariant must change to meet the goal, STOP and report it as an
  open issue — you do not decide that.
Contract/spec source: <path to canonical contract doc / design source of truth>
Standards to follow:
  - TDD: tests first, 80%+ coverage, pure functions for pure logic
  - Immutability, small files, boundary validation, explicit error handling, no
    hardcoded secrets
  - Documentation four principles (code=How, tests=What, commits=Why, comments=Why-not)
  - For document/spec units specifically: author-side consistency pass before reporting
    done — grep every changed concept for ALL occurrences, verify every §/test-number
    cross-reference resolves correctly, then a full reread from an implementer's
    perspective (gate-protocol.md § 9a(a), added 2026-07-19)
  - If this unit changes the MEANING of a value shared across files (coordinate space,
    ratio, unit, enum semantics) — not just its name: sweep every CONSUMER of that value
    and confirm each one's interpretation still matches; report which consumers were
    checked and how (gate-protocol.md § 8 "Semantic-consumer sweep", added 2026-07-19)
Gate commands (run for real, report actual numbers):
  - <test command, absolute path from repo root>
  - <build command>
  - <lint/typecheck command, if applicable>
  - <any existing suite affected by this change — see gate-protocol.md § 8
    "Development standards to enforce in every creator brief">
Geometric verification (ONLY for units delivering visual geometry — position, size,
  alignment, spacing, overlap, z-order; gate-protocol.md § 6a):
  - MEASURE FIRST, then fix. Your first action is to run the measurement script and
    report the current numbers plus the computed styles producing them. Changing CSS
    before you have a measured diagnosis is what causes repeat rounds.
  - Script: <path to the project measurement script> <selector list>
  - Report a table: selector | property | target | before | after. The unit is not done
    until every after-value matches its target. A screenshot is supporting evidence, not
    the verdict.
  - If this is the 3rd round on the same defect, do NOT nudge values again — report the
    layout method you believe is wrong (alignment axis, baseline vs centre, positioning
    context, specificity conflict) and stop for a commander decision.
Temp-file hygiene (2026-08-11, per user instruction): create scratch files (helper
  scripts, log dumps, probe binaries, one-off .bat/.sh runners) in the session
  scratchpad directory, NOT in the repo tree. You may delete ONLY files you created
  in this run, by explicit path (2026-08-26): pre-existing untracked or modified files
  in `git status` are the owner's working files — never delete, `git clean`, restore
  or stash them to make the tree "match the expected list"; leave them and mention
  them in your report. If a temp file must live in the repo tree
  (e.g. a build wrapper the toolchain requires), DELETE it before reporting done and
  list the deletion in your report. "Done" with stray temp files in the tree is not
  done — the commander's acceptance checklist rejects it.
Artifact-directory hygiene (2026-08-19, per user instruction): screenshots, measurement
  JSON, trace/video output and probe crops written into the project's artifact
  directory (e.g. `.playwright-mcp/`, `test-results/`, `playwright-report/`) are
  GITIGNORED, so they never appear in `git status` — yours or the commander's. They are
  therefore easy to leave behind and accumulate across a session.
  - Report them explicitly: list every file you wrote into those directories, by name,
    in your done-report. Do not substitute `git status --short` for this — it cannot
    see them, and reporting a clean `git status` as if it covered them is misleading.
  - Delete them yourself once the numbers/observations they support are written into
    your report, UNLESS the brief tells you to keep them for the commander to inspect.
    The recorded measurements are the durable artifact; the images are scaffolding.
  - If you keep any, say which and why, so the commander can decide at acceptance.
Definition of done: <explicit, testable>
Report back: files changed, actual test/build numbers, per-invariant confirmation,
  geometric measurement table (if applicable), temp files created and their cleanup
  status, open issues. No prose beyond that — this feeds the gate, not the user.
```

**Do not paste tool-discipline rules into this brief.** File-creation-via-shell
(`cat >`, `echo >`, `tee`, `Set-Content`/`Out-File`) and `cd X && cmd` compounds are
blocked mechanically for every agent by the `tool_discipline` guard (PreToolUse Bash/PowerShell)
(installed 2026-08-17). Restating them as a "HARD RULE" paragraph in each dispatch — as
was routine before the hook existed — now costs tokens in every brief and buys nothing.

### Compact header — replaces the four creator markers (2026-09-05, PE-19)

A creator/round brief used to need FOUR separate hand-written markers to clear the
brief-gate hook's gates 1/2/3/6: an `APPROVAL:<file>.md` reference, a `STATE`/acceptance-
table mention, an `OWNER-CONFIRMED:` block, and — for UI-shaped briefs — a named design
skill (or a "Design directives" section). Restating all four in every dispatch is the
boilerplate cost this section exists to cut; it also blocked formatting variants that
were plainly compliant in spirit (`OWNER-CONFIRMED (decided this session):` with no bare
colon; a codex forwarder message missing the literal `.out`).

**One line replaces the four markers:**

```
BRIEF: <approval-file>.md | <kind> | no-ui
```

- `<approval-file>.md` is the SAME file gate 6 already requires: write the presented
  understanding + acceptance-criteria deltas to
  `~/.claude/state/brief-approvals/<approval-file>.md` before dispatching, exactly as the
  long form's `APPROVAL:<file>.md` protocol always required. The guard resolves this
  reference for gate 6 (existence + ≥3-minute age, or an `OWNER-OK` line) exactly as
  before — the compact header does not relax that check.
- Put the acceptance-table reference (STATE.md §2 rows) and the `OWNER-CONFIRMED:`
  decision list INSIDE that same approval file instead of restating them in the dispatch
  text. Gates 1 and 3 accept either the dispatch text OR the approval file's content —
  the file the owner already reads to approve the round is now also where those two
  markers live, once, instead of twice.
- `<kind>` is one of `forwarder` / `committer` / `reviewer` (classifies the brief exactly
  like the bracket form, e.g. `[Committer Brief]`, would) or `creator` — `creator` is not
  matched specially, it is simply the fallback every unrecognised/unlabelled brief already
  falls into, so writing it is documentation, not a functional token.
- `| no-ui` is optional and ONLY meaningful for gate 2 (the UI-design-skill case): if this
  brief's OWN wording trips a UI trigger (layout/design/responsive/viewport/etc.) and it
  genuinely is not UI work, the literal `| no-ui` tag satisfies that case in place of
  naming a design skill. It does nothing else — a header that says `| ui`, or omits the
  tag, or is simply wrong, still requires naming an actual design skill
  (`/ui-ux-pro-max`, `/emil-design-eng`, `/frontend-design`) if the trigger fires. The
  tag cannot be used to wave away real UI work.

**The approval file itself must satisfy the gates it stands in for (2026-09-05, live-test
finding).** The compact header does not weaken gates 1/3 — it moves *where* their tokens
may live, from the dispatch text to the approval file, once instead of every round. That
only works if the approval file actually contains the tokens gate 1 and gate 3 look for:

- Gate 1 needs an acceptance-table reference: the word `STATE` (as in `STATE.md`), or
  `完成の定義`/`良品条件`, or the literal phrase `acceptance table`.
- Gate 3 needs the literal `OWNER-CONFIRMED:` — **with the colon**. `OWNER-CONFIRMED
  （AskUserQuestion で確認済み）` (full-width parenthesis, no colon) does NOT match
  `OWNER-CONFIRMED\s*:` and will not satisfy gate 3, even though it reads as an owner
  confirmation to a human. This is the exact trap a live test hit: an approval file with
  that heading passed gate 6 (file exists, old enough) and still BLOCKED on gate 3, with
  no text in the dispatch itself to explain why. The colon is load-bearing — write
  `OWNER-CONFIRMED:` (or `OWNER-CONFIRMED (...)​:`, colon after the parenthetical) if you
  want prose around it.

If you skip this and the approval file is missing either token, the compact-header
dispatch fails at gate 1 or gate 3 with no clue in the brief text pointing at the file —
diagnose by opening the referenced approval file and checking it literally contains
`STATE`/`acceptance table` and `OWNER-CONFIRMED:`.

Minimal approval-file skeleton (copy this into
`~/.claude/state/brief-approvals/<approval-file>.md` before dispatching a compact-header
brief that relies on the file for gates 1/3, rather than restating them in the dispatch
text):

```
Presented understanding: targets STATE.md §2 acceptance table rows <n>/<n>.
OWNER-CONFIRMED: <quote or summary of the owner's actual decision>
```

(Whether the guard should also accept a full-width-parenthesis heading like
`OWNER-CONFIRMED（…）` instead of requiring the colon was considered and rejected: that
would loosen `require_pattern`'s `OWNER-CONFIRMED\s*:` match for every creator brief, not
just approval files, and a pattern loosened to tolerate one typo class tends to tolerate
the next one too — the gate exists precisely to force an unambiguous, machine-checkable
marker instead of "reads as approval to a human." Keeping the pattern strict and fixing
the file is the correct direction; this section is that fix.)

**Worked example** (a non-UI creator round, fully compact):

```
BRIEF: <issue>-round12.md | creator | no-ui
Handle the retry-queue backoff fix for <issue> rows 3/4. <...rest of the Creator Brief
template above, minus the APPROVAL:/OWNER-CONFIRMED:/STATE restatement...>
```

where `~/.claude/state/brief-approvals/<issue>-round12.md` (already written and presented to
the owner per gate 6's protocol, ≥3 minutes old or carrying an `OWNER-OK` line) contains
something like: "Presented understanding: targets STATE.md §2 acceptance-table rows 3/4.
OWNER-CONFIRMED: owner approved this scope in chat today."

**The long form still works, unchanged** — `APPROVAL:<file>.md`, a `STATE`/acceptance-
table mention, an explicit `OWNER-CONFIRMED:` block, and a named design skill, restated
directly in the dispatch text, keep passing gates 1/2/3/6 exactly as before. The compact
header is an alternative, not a replacement requirement — use whichever costs fewer
tokens for a given round; mixing (e.g. a compact `BRIEF:` header plus an inline
`OWNER-CONFIRMED:` restatement) is also fine, since each gate is satisfied by ANY source
that matches its pattern.

## Reviewer Brief (M/L iterative rounds — opus reviewer, always a different agent from the creator)

Use this for every iterative review round (round 1 and send-back re-reviews), routed to
an opus reviewer per `gate-protocol.md` § 7 (2026-08-03 routing). The final third-party
pass before commander acceptance uses the Codex Reviewer Brief below instead.

```
You are reviewing, not authoring. Do not fix anything yourself; report findings only.
Scope: <exact diff / file list — do not explore beyond this>
Cleanliness mandate (this is an ITERATIVE round; a Codex third-party final review runs
  after you): treat wording inconsistencies, definition gaps, cross-reference drift, and
  terminology mismatches as first-class findings and surface ALL of them now — the goal
  is that the later Codex pass faces only substantive content. Do not defer "minor"
  consistency items to a later round (gate-protocol.md § 7a "Pre-Codex cleanliness bar").
Design/spec source of truth: <path>
Acceptance criteria: <explicit, testable>
Focus areas (commander's risk hypotheses — check these specifically):
  - <e.g. "confirm the batch write is actually atomic; check the platform's
    documented guarantees, don't assume">
  - <e.g. "check XSS handling on the user-supplied <field> — is it sanitized or
    dangerouslySet?">
  - <...>
Mandatory: re-run every gate command yourself and report the actual numbers you
  observed. Do not trust or restate the creator's numbers without re-running.
Invariants to re-check independently (gate-protocol.md § 8): <same list given to the
  creator>. Verify each one yourself in the diff — do not accept the creator's
  self-report. An invariant the creator did not mention is a finding.
Geometric verification (visual-geometry units only, gate-protocol.md § 6a): re-run the
  measurement script yourself and reproduce the creator's target/before/after table.
  A GO on geometry requires numbers you measured, not a screenshot the creator attached.
GO/NO-GO rule: zero findings required for GO. List every finding found, including LOW
  severity — do not omit LOW findings as "not worth mentioning." For each LOW finding,
  state whether you believe it's fixable cheaply or acceptable-as-is with reasoning.
Report format: severity-tagged list (CRITICAL/HIGH/MEDIUM/LOW), each with file:line,
  what's wrong, why it matters. End with an explicit GO or NO-GO verdict.
```

## Codex Reviewer Brief (M/L, design/contract docs — third-party FINAL review before commander acceptance; repositioned 2026-08-03, per user instruction)

Per `gate-protocol.md` § 7/§ 7a, Codex CLI (`gpt-5.6-sol`) runs once per unit/wave as
the third-party final review, dispatched only after the opus iterative loop has reached
GO with all consistency/wording/definition debt already cleared (§ 7a "Pre-Codex
cleanliness bar"), and immediately before the commander's acceptance checklist. Findings
here go back to the creator and are re-verified by Codex (delta-scoped) until zero
findings. This template keeps the same six required elements as the Reviewer Brief above
— nothing about *what* gets checked changes, only *who runs when*. Do NOT scope-restrict
Codex away from consistency findings on code units; upstream is responsible for having
already removed them. Dispatch a Bash-capable forwarder subagent (`general-purpose`,
`model: haiku`) with this brief; the forwarder's only job is to build and run the
`codex exec` command and relay output verbatim — it does not itself judge the code
(that would make it a second reviewer's worth of judgment on haiku, not a forwarder).
Because the opus round 1 already re-ran the full gates, the default Mandatory line for
this final pass is the round-2+ scoped variant (scoped tests + typecheck) unless the
commander's brief explicitly requests full gate re-execution.

```
You are a forwarder, not a reviewer. Build and run exactly one `codex exec` call from the
brief below, then return its output verbatim plus the exit code. Do not read the diff
yourself, do not form an opinion about it, do not summarize or trim Codex's output.

Step 1 — availability probe (run first, every time unless the commander already confirmed
availability for this batch in the same turn):
  codex login status
  IF this reports not logged in, or `codex` does not resolve as a command, STOP and report
  "CODEX_UNAVAILABLE: <what the probe showed>" — do not proceed to Step 2.

Step 2 — write the review prompt to a temp file (avoids shell-escaping/length issues with
long multi-line prompts — do not inline a large prompt as a quoted Bash argument):
  Write the text below to a temp file, e.g. via the Write tool or
  `cat > "$PROMPT_FILE" <<'EOF' ... EOF`.

  --- prompt file contents ---
  You are reviewing, not authoring. Do not fix anything yourself; report findings only.
  Scope: <exact diff / file list — do not explore beyond this>
  Design/spec source of truth: <path>
  Acceptance criteria: <explicit, testable>
  Focus areas (commander's risk hypotheses — check these specifically):
    - <e.g. "confirm the batch write is actually atomic">
    - <e.g. "check XSS handling on the user-supplied <field>">
    - <...>
  Mandatory (round 1 of this unit/wave): re-run every gate command yourself in this
    working directory and report the actual numbers you observed (test command: <...>,
    build command: <...>, lint/typecheck: <...>). Do not trust or restate the creator's
    numbers without re-running.
  Mandatory (round 2+, delta re-review after a send-back — delete the round-1 line above
    when filling this variant): re-run SCOPED tests only, for the changed
    directories/files (<paths>), plus typecheck. Do NOT re-run the full suite or build —
    the commander runs the full gate list once at wave-commit time
    (gate-protocol.md § 3 item 5, added 2026-07-19).
  <Round 2+ on a document/contract review only, append:> Report ONLY findings a mechanical
    implementer would build incorrectly from — no stylistic items
    (gate-protocol.md § 9a(d), added 2026-07-19).
  GO/NO-GO rule: zero findings required for GO. List every finding, including LOW severity
    — do not omit LOW findings as "not worth mentioning." For each LOW finding, state
    whether it's cheaply fixable or acceptable-as-is with reasoning.
  Report format: severity-tagged list (CRITICAL/HIGH/MEDIUM/LOW), each with file:line, what
    is wrong, why it matters. End with a line that is exactly "VERDICT: GO" or
    "VERDICT: NO-GO" so the result can be parsed mechanically.
  --- end prompt file ---

Step 3 — run the review from the working directory that contains the unit under review
(the same repo/worktree the creator worked in — do not run from an unrelated cwd). Short
review, safely foreground (well under ~10 min):
  codex exec -m gpt-5.6-sol -s workspace-write "$(cat "$PROMPT_FILE")" \
    > "$OUT_FILE" 2> "$ERR_FILE"
  CODEX_EXIT=$?

  Longer review (gate re-execution, large document) — background with an exit marker:
  ( codex exec -m gpt-5.6-sol -s workspace-write "$(cat "$PROMPT_FILE")" \
      > "$OUT_FILE" 2> "$ERR_FILE"; echo $? > "$EXIT_FILE" ) &
  (run this as a `run_in_background: true` Bash call; see the stay-alive note below)

  Sandbox note (why not read-only): the brief requires Codex to re-run gate commands
  (tests/build/lint) itself per the Mandatory line above — that means executing processes
  that write build output, coverage reports, or cache files inside the working tree.
  `-s read-only` blocks that, so use `-s workspace-write` (not `danger-full-access` — no
  need for network/system-wide write access for a code review).
  If Codex refuses because the working directory isn't recognized as a git repo, retry once
  with `--skip-git-repo-check` appended — do not add this flag pre-emptively when already
  inside a known git repo.
  Foreground calls are capped by the Bash tool's ~10-minute timeout — any review likely
  to run longer (gate re-execution, a large document) MUST use `run_in_background: true`
  from the start; do not attempt it as a foreground call and discover the timeout
  mid-review. When backgrounding, write stdout/stderr/exit code to three separate
  scratchpad files ($OUT_FILE / $ERR_FILE / $EXIT_FILE — write $EXIT_FILE only after the
  process has actually completed) so the result is checkable from disk independent of
  this agent's own turn.

  STAY ALIVE in the same turn until it finishes: block on the result with the Monitor
  tool (until-condition: $EXIT_FILE exists) or repeated blocking TaskOutput calls. NEVER
  end your turn / return your final report while the codex run is still in flight — a
  subagent that has returned its final message is NOT re-invoked when its background
  command later completes, so the review result would sit on disk unnoticed until someone
  manually resumes the agent.

  **Do not treat this stay-alive instruction as sufficient on its own** (2026-07-19, per
  user instruction): forwarders ended their turn anyway while the backgrounded codex run
  was still in flight in ~6 separate instances across one multi-day session despite
  this explicit instruction. The commander compensates structurally rather than relying on
  the forwarder's discipline — see operations.md → "Codex background+watcher pattern" for
  the commander-side half of this: the commander starts its own bounded polling watcher
  for $EXIT_FILE in the same turn it dispatches this forwarder, and reads $OUT_FILE
  directly when the watcher signals completion. Treat any eventual "review complete"
  message from this forwarder as redundant confirmation, not the critical path.

Step 4 — report back to the commander (for the backgrounded variant, read $EXIT_FILE for
the exit code instead of the shell's $?; this report is redundant confirmation once the
commander's own watcher has already read $OUT_FILE, per the note above — send it anyway
so the record is consistent):
  IF the exit code (CODEX_EXIT or the contents of $EXIT_FILE) is non-zero, OR $ERR_FILE
  contains an authentication/model-rejection message, THEN report
  "CODEX_UNAVAILABLE: <the relevant error text>" — do not paraphrase a partial result as
  if it were a completed review.
  ELSE return the full contents of $OUT_FILE verbatim, unedited, plus the exit code and the
  parsed "VERDICT: GO/NO-GO" line if present.
```

**Fallback**: if the forwarder reports `CODEX_UNAVAILABLE`, the commander dispatches the
Reviewer Brief (above) to a FRESH opus reviewer instance (not the one that ran the
iterative loop) per `gate-protocol.md` § 7's fallback row, and states the fallback
explicitly in the eventual user-facing report (see § 7a's last If-Then rule for the
exact phrasing convention).

## Send-Back Brief (after a NO-GO or findings)

```
Reviewer findings to address: <paste the reviewer's findings list>
Resolution for each finding (commander-decided; do not leave open-ended):
  - Finding 1: <commander's chosen fix direction, stated concretely — not "fix this",
    but "change X to use Y approach because Z">
  - Finding 2 (product/scope call — resolved via AskUserQuestion with the user):
    <the decision the user made, stated as a fact to implement>
  - ...
Resume as: <same creator agent via SendMessage — do not spawn a fresh agent unless the
  original is unrecoverable>
Re-run gate commands after the fix and report actual numbers again.
```

For **geometric** send-backs (position/size/alignment/spacing/overlap/z-order), the
commander converts the complaint into a measured delta BEFORE dispatching
(gate-protocol.md § 6a). Never forward "まだずれてる" verbatim — that is a symptom, and
sending symptoms back is what produced the 4-round alignment loop this rule exists to
prevent. Add to the brief above:

```
Measured defect: <selector> — <property> is <actual>, target is <expected>,
  delta <n>px. Measurement command: <script + selectors used>
Round count on this defect: <n>. If this is round 3+, the fix direction is NOT another
  value change — <commander's decision on which layout method to replace, per
  gate-protocol.md § 6a rule 5>.
```

## Commit Brief (S task — route to haiku)

```
Stage exactly these files (do not use -A or .):
  - <file 1>
  - <file 2>
  - ...
If `git status` shows any OTHER modified/untracked file not in this list, STOP and
report back — do not stage it, do not guess whether it's related.
Commit message (Why-focused, conventional commit type):
  <type>: <description>

  <optional body explaining WHY this change, not what — the diff already shows what>
After committing successfully, push. Report the commit hash and push result.
```

## STATE template (per issue — `docs/plans/<issue>/STATE.md`; 2026-08-29, roles-pm-l.md)

The PM creates it (§1–§4 filled, §1 confirmed with the owner) before spawning an L. L
reads it at every round start and updates it before every report. It is the only state
that survives a vanished L.

**§2 establishment triggers (2026-09-03, after an owner escalation asking where the
acceptance criteria were):** the 完成の定義/良品条件 table must exist BEFORE the first
creator dispatch and must be RE-ESTABLISHED (owner-confirmed) at: (a) issue start,
(b) any scope pivot the owner directs mid-issue, (c) promotion of a prototype to
production (「本適用」— this is the moment iterative per-round briefs stop being an
acceptable substitute). A PM running per-round briefs without a current §2 table is the
violation that produces late-stage churn: "done" silently becomes whatever the latest
review round said, the goalposts move every round, and the owner is the one who has to
ask what the acceptance criteria were. Observed on an issue that ran dozens of rounds
this way. No creator round may be dispatched while §2 is missing or stale.

**Anti-decay rule (2026-09-03, after an owner escalation asking why the mandated design
skills had not covered the case):** the UI classification gate and Design-directives
section apply to EVERY creator dispatch — including send-backs, addenda, and
"round N+1" continuation briefs, not just the first brief of an issue. The decay
pattern, observed across a run of consecutive send-backs late in one issue: the first
briefs carried full directives, then successive send-backs shrank to "keep the
existing tokens" one-liners, design-skill invocation silently stopped, and the owner
ended up specifying compact-layout UX themselves and asking why the mandated design
skills hadn't covered it. A brief that touches UI without naming which design skills
the creator must invoke (and requiring the "Design directives applied" section in the
done-report) is an invalid brief — same standing as a dispatch with no §2 table.

```
# <ISSUE-ID> STATE
## 1. 目的（PM がオーナーと確認済み・変更禁止）
- 何のための機能か / ユーザーに何が見えれば成功か
## 2. 完成の定義（観測できる操作と結果のみ。文言・存在確認は不可）
| # | 操作 | 期待される結果 | 証拠の種類（スクショ/実測値/ログ） | 状態（未/PASS/FAIL） |
## 3. 決定事項（オーナー決定。L は変えない、疑問は PM へ）
## 4. 横断ルール（PM 配布。全ブリーフに同梱）
- 見せかけ UI 禁止（無反応・未実装通知・行き先のない導線は撤去か接続）
- 依頼外の変更は実装前に PM へ提案（見え方・理由付き）。勝手に足さない
- 検証は作業者・レビュア・監査役が行い証拠を添える。主張のみは差し戻し
- 指摘 1 件 = 同型の問題を総当たりで探して表にする
- サーバ起動/停止は禁止（既存ポートを使う。落ちていれば報告）。未追跡ファイルは触らない
- 逸脱・未達は L が承認しない。PM 経由でオーナー判断に返す
## 5. 進行中（ラウンド / 担当エージェント / ブリーフ要約 / 成果物パス）
## 6. 未依頼の変更・提案待ち
| 提案 | 理由 | 見え方 | オーナー判断 |
## 7. 保留・オーナー判断待ち
## 8. 履歴（ラウンドごとに 1 行）
```

## L Brief (issue leader; 2026-08-29, roles-pm-l.md)

```
役割: あなたは <ISSUE-ID> のリーダー（L）。実装・検証は自分でしない。作業者・レビュア・
  監査役を立て、証拠を検品し、PM に表で報告する。オーナーとの窓口は PM だけ。意図の解釈が
  必要になったら作業せず PM に「私はこう理解した、合っているか」の形で返す。
入力: docs/plans/<issue>/STATE.md（§1–§4 は変更禁止）/ 作業ツリー / ブランチ /
  検証サーバのポート（起動・停止は禁止）
1 ラウンド: STATE §2 の未/FAIL 行を選ぶ → Creator Brief（このファイル）→ Reviewer Brief
  （コード、opus）→ Auditor Brief（挙動）→ L 検品（各行に証拠があるか／主張と証拠が一致
  するか／未依頼の変更が混ざっていないか。欠けていれば作業者へ差し戻し）→ STATE 更新 →
  PM 報告。コミットは PM の指示後に Commit Brief で haiku へ。
PM への報告書式（これ以外を書かない）:
  - 完成の定義: PASS n / FAIL n / 未 n（FAIL・未は行番号と理由）
  - 証拠一覧（パス）
  - 未依頼の変更・提案（§6 の表。実装していないこと）
  - オーナー判断が要ること（§7）
  - 次ラウンドの対象
禁止: 逸脱の自己承認 / 証拠なしの PASS / 目的の裏付けなく定数・見た目を変える /
  サーバ操作 / 未追跡ファイル削除 / push / `git add -A`
```

## Auditor Brief (監査役 — behaviour acceptance, read-only; 2026-08-29, roles-pm-l.md)

```
You are the acceptance auditor. Read-only: no edits, no commits, no server operations
  (use the existing server at <url>; if it is down, report and stop).
Input: STATE §2 table at <path>; deliverable under review: <files / build>.
For EVERY row: perform the operation for real (click / type / wheel / navigate) in a real
  browser (Playwright via node scripts, not the extension) or the real runtime, capture
  the RESULT (screenshot of the outcome, measured value, network/console capture, click
  log), and mark PASS/FAIL. "Element or text exists" is never PASS.
Interaction sweep: on every screen touched by the deliverable, at <widths>, enumerate
  every button/link/input/select/kebab item, operate each once, and record the observable
  effect. No effect, an error, or a "未実装/未接続" notice = FAIL (見せかけ UI).
Structural wiring: for every UI-driven feature in scope, trace render → handler → route →
  backend/external service, and external resources vs CSP; a missing link is FAIL.
Same-class sweep: for each FAIL, search the deliverable for the same defect shape and add
  a table (feature | broken link | evidence file:line | severity).
Output: the STATE §2 table with status + evidence path per row, the interaction-sweep
  table, the structural table, and the same-class table. Raw facts; do not claim anything
  you did not operate.
```

## Per-Unit Handoff Documentation Brief (after each unit is accepted)

Every accepted unit gets a documentation commit alongside (or immediately after) the
code commit — this is not optional polish, it's part of "done." Route to `doc-updater`
or the same mechanical/haiku tier as commits.

```
Update the project's handoff/progress doc (e.g. HANDOFF.md, or the project's
equivalent durable ledger) with:
  - What shipped in this unit (1-3 bullets, user-visible outcome)
  - Review history: NO-GO → what was found → what was fixed → final GO (only if there
    was a send-back loop; skip if it passed clean)
  - Actual gate numbers at acceptance (test count, coverage %, build status)
  - Any accepted-LOW-findings with their recorded rationale (gate-protocol.md § 4
    "Severity handling")
  - Any deferred/future-TODO items the commander consciously chose not to block on
Commit this alongside the code, or as a immediately-following docs: commit.
```
