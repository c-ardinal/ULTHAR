# Gate Protocol — sizing, gates, routing, acceptance

Read this file at the start of any task that involves dispatching a creator or a
reviewer, or accepting a deliverable. It is the authoritative detail behind the
"quality gate" and "model routing" sections of SKILL.md.

## 0. Reading discipline (2026-09-05, per owner instruction)

SKILL.md is loaded on every invocation of this skill forever, so its "Start here" section
was cut down to a two-line pointer: read `roles-pm-l.md` in full upfront (it decides *who*
applies every other rule, so reading it late means re-deriving who-does-what mid-session),
and read the other four reference files by *section*, just-in-time, per SKILL.md's phase
table — never in full at session start. This still satisfies the original intent
("discipline lives in the files, not in the commander's memory, so it reproduces
identically whichever model runs the commander role"): a section opened at the point it's
needed is read from the file just the same as one read upfront — what changed is *when* a
section is read, not *whether* it ever is. The previous rule ("read all five files before
your first dispatch") cost ~137KB of mandatory reading every session regardless of what
that session actually needed; the phase table already named which file/section each phase
needs, so the mandatory-upfront wrapper around it was pure cost with no added discipline.

## 1. Classify the task unit

| Size | Definition | Gate |
|------|-----------|------|
| **S** | Single-file fix, config change, mechanical edit, doc tweak, git plumbing. No new logic, no new public interface. | Creator → **commander acceptance** (read the diff yourself; no separate reviewer) |
| **M-lite** | M-size in kind (new/changed behavior, multi-file) but small blast radius: a few files, existing test coverage, no new public interface/architecture, no security-sensitive surface. Examples: moving a section between pages, retargeting a link, layout/wording changes, small refactors within one feature. | Creator → **diff-scoped light review** → commander acceptance (see § 1a) |
| **M** | New logic, multi-file change, bug fix touching behavior, one feature/module. | Creator → **separate reviewer** → commander acceptance |
| **L** | Multi-module feature, parallel fan-out build, anything user-facing at release level. | M gate per unit **+ integration review + E2E** before ship |

- **Escalate, never de-escalate mid-flight.** If an S task turns out to introduce logic
  or touch an interface, upgrade it to M and add the reviewer. Never downgrade a task
  already classified M/L to dodge a review.
- **Creator ≠ reviewer** (M/L), always, no exceptions. A creator reviewing its own output
  defeats the purpose of the gate.

## 1a. M-lite light gate (added 2026-07-15, user instruction)

Cost-calibrated gate for small M-size work. The 3-stage flow (creator → separate reviewer → commander) is retained, but each stage is slimmed:

- **Creator gates**: scoped tests (the affected feature dirs) + build. The FULL suite is required only when the change touches shared constants/components consumed outside the unit's feature, and E2E only when a user-facing flow changed. Otherwise skip them at unit level; run the full suite once per session/batch before shipping instead of once per unit.
- **Reviewer**: diff-scoped review only (same independence rule — opus base reviewer; the final pre-acceptance Codex pass per § 7a still applies to M-lite only when the commander judges the unit acceptance-critical, otherwise the opus review suffices). The reviewer does NOT re-run gate commands; it reviews the diff against the contract and may trust the creator's reported numbers unless something in the diff makes them implausible (in which case escalate to full M gate). Keep the same GO/NO-GO + LOW-listing rules.
- **Commander acceptance**: unchanged (§ 6), except the "reviewer's numbers match creator's" check reduces to "creator's numbers are present and plausible".
- **Classification guard**: when in doubt between M-lite and M, pick M. Escalate M-lite → M mid-flight if the diff grows beyond a few files, touches a shared interface, or a reviewer finding reveals hidden blast radius. Never use M-lite for auth/input-handling/security surfaces, migrations, or anything the user will see as a release-level feature.
- **Why this exists**: a full M gate (dual full-suite runs + reviewer gate re-execution) on a small move/rename-scale change costs multiples of the change itself; a settings-redesign session measured this and the user directed the calibration.

## 2. Independent Review Mandate (do not skip this)

**A creator's self-report of "I had it reviewed internally, zero findings" is not
evidence and does not satisfy the M/L gate.** It is unverifiable — you cannot see what
was actually checked, by what standard, or whether it was checked at all. Treat any such
claim as equivalent to no review having happened.

If-Then:
- IF a creator's final report includes phrases like "reviewed and confirmed clean",
  "self-checked, no issues", "verified internally" → THEN dispatch your own independent
  reviewer anyway. Do not shortcut the M/L gate on the strength of a self-report.
- IF the task is M/L → THEN a commander-dispatched, separate-agent review is mandatory
  every time, with no exception for "the creator seemed thorough."
- This is not paranoia for its own sake: in practice, independent review on
  self-reported-clean deliverables has repeatedly surfaced MEDIUM/HIGH/CRITICAL findings
  the self-report missed.

## 3. Reviewer briefing — required elements (see briefing-templates.md for the fill-in form)

Every reviewer dispatch must include ALL of the following, or the review is incomplete:

1. **Scope**: the exact diff / file list to review — not "the repo" or "the feature."
2. **Design/spec source of truth**: path to the canonical design or contract doc, so the
   reviewer checks against the real spec, not its own assumptions.
3. **Acceptance criteria**: what "done" means for this unit (explicit, testable).
4. **Focus areas**: risk hypotheses the commander wants specifically checked (e.g. "check
   whether the batch write is actually atomic", "check XSS on the user-supplied title
   field"). Don't leave the reviewer to guess where the risk is — the commander forms a
   hypothesis and states it.
5. **Mandatory gate re-execution — round 1 full, round 2+ scoped** (round-split added
   2026-07-19, per user instruction): the reviewer's **round-1** review of a unit or wave
   must re-run every gate command (tests, build, lint, coverage) itself and report the
   **actual numbers**, not trust the creator's report of them. **Round 2+ delta
   re-reviews** (after a send-back) re-run SCOPED tests only — the changed
   directories/files — plus typecheck; do NOT re-run the full suite or build at round 2+.
   The commander runs the full gate list once, at wave-commit time, instead of once per
   round (measured: re-running full gates on every Codex round of a wave was
   that session's dominant wall-clock/token cost). For **M-lite** units (§ 1a), this item
   is replaced entirely by diff-scoped review without gate re-execution at any round — do
   not instruct the reviewer to re-run suites for M-lite.
6. **GO/NO-GO rule**: explicitly state "zero findings required for GO; any LOW findings
   must be individually listed and reported as non-blocking with stated rationale — LOW
   findings do not get silently dropped."

**Batching small reviews**: when several S/M deliverables land around the same time,
audit them with ONE reviewer dispatch by listing every target diff in that single
briefing (one prompt listing all diffs) instead of dispatching one reviewer per
artifact — same independence, fraction of the cost.

**Wave batching for contract-driven builds** (added 2026-07-19, per user instruction):
when a large build follows a locked contract doc (SKILL.md → "Decomposition and
parallelism"), dispatch units in dependency-graph waves and review each wave with **one**
batched reviewer dispatch covering every unit's diff in that wave, not one review per
unit (opus for the iterative rounds; the wave's single final Codex pass is likewise one
batched dispatch, per § 7a's 2026-08-03 sequencing).
Route findings back to each affected unit's creator in parallel — send-backs stay
per-unit even though the review was batched — then commit the whole wave together once
every unit in it is GO. Measured this session: per-wave batched reviews converged in 2-3
rounds each, versus a single ever-growing document review loop (§ 9a) that ran to 20
rounds — batching by wave keeps each review scoped to a coherent, boundable diff instead
of one document that keeps absorbing new findings.

**Split the integration/shared-file role by file group on a large shared surface**
(2026-09-05, per owner instruction): SKILL.md → "Decomposition and parallelism" already
routes all shared-file wiring to one owning unit to prevent concurrent-edit corruption —
that part is not changed here. The gap this closes: on a large refactor, funneling
*every* shared file (e.g. `lib/`, the dispatcher, the installer, and the test harness all
at once) into that single unit serializes every send-back through it regardless of which
file group the finding was actually in — measured this session: one such catch-all unit
ran 18 review rounds while five other, independent units sat idle waiting on it, because
each of their integration dependencies routed through the same bottlenecked unit. Fix:
when the shared surface spans multiple unrelated concerns, assign one owning unit **per
file group** instead of one unit for the whole shared surface, keeping the same
no-two-units-touch-the-same-file rule scoped to each group rather than to the surface as
a whole. Reserve a single combined integration unit for shared surfaces small or tightly
coupled enough that splitting them would just recreate the cross-file coordination
problem this rule exists to prevent.

## 4. Severity handling

- **HIGH/CRITICAL/MEDIUM**: always send back to the creator for a fix. No exceptions,
  no "ship now, fix later" without an explicit, user-visible decision to defer (see
  § 6 "Acceptance checklist" below).
- **LOW**: two valid outcomes, both must be explicit —
  1. **Fix it** if the fix is cheap (small diff, low risk of new regressions) — send back
     to the creator.
  2. **Accept it** only with a stated reason (e.g. "matches existing codebase
     convention", "no real-world impact demonstrated") — and this acceptance MUST be
     recorded in the project's durable documentation (HANDOFF doc, design-deviation log,
     or equivalent DEV ledger). A LOW finding that is silently dropped is a process
     failure, not a minor omission.
- Never report a deliverable to the user while ANY finding — including accepted LOW
  findings without a recorded rationale — is unresolved or undocumented.

## 5. Send-back: commander decides the fix direction

When a reviewer finds issues, the commander does not forward the review open-endedly and
hope the creator picks the right fix. Before sending back:

- **Design/technical judgment calls** (how to restructure a function, which pattern to
  use, which of two valid approaches to take): the **commander decides** and states the
  chosen direction explicitly in the send-back brief. Do not ask the creator to pick.
- **Product/scope judgment calls** (data retention behavior, UX direction, whether a
  feature is in scope at all): the commander does **not** decide unilaterally — raise it
  to the user via AskUserQuestion, get the call, then send back with that decision baked
  into the brief.
- A send-back brief with no concrete direction ("please address the reviewer's
  comments") produces drift and repeated review cycles. Always specify the resolution,
  not just the problem. See briefing-templates.md → Send-Back Brief.

## 6. Acceptance checklist (commander, before reporting to user)

Do all of these yourself — acceptance is not reading the reviewer's verdict and trusting
it:

- [ ] Read the actual diff / key hunks yourself (Read/Grep), not just the reviewer's
      summary.
- [ ] Confirm the reviewer's reported numbers (test count, coverage %, pass/fail) match
      what the creator separately claimed. A mismatch is itself a finding — investigate
      before accepting.
- [ ] Confirm every finding from the last review round is either fixed (verify in the
      diff) or explicitly accepted-with-rationale-recorded (§ 4 "Severity handling").
- [ ] **Temp-file hygiene** (2026-08-11, per user instruction): run `git status --short`
      and confirm the unit left NO stray temp files in the repo tree (one-off scripts,
      .bat/.sh runners, log dumps, probe outputs). Scratch work belongs in the session
      scratchpad; anything the unit had to create in-tree must be deleted before
      acceptance. A stray temp file is a send-back item — the creator deletes it and
      re-reports; the commander does not silently clean it up. **Only files the unit
      itself created count as stray** (2026-08-26): an untracked or modified file that
      pre-dates the unit is the owner's working file — it is neither deleted, restored
      nor staged by anyone; the commander lists it in the acceptance notes and moves on.
- [ ] **Gitignored artifact directories — `git status` CANNOT see these** (2026-08-19,
      per user instruction, after this exact miss): `git status --short` omits every
      ignored path, so a hygiene check built only on it is blind to precisely the
      directories agents are told to write into. **Additionally list the artifact
      directories directly** before accepting:
      `ls .playwright-mcp/ test-results/ playwright-report/` (plus any project-specific
      output dir). Screenshots, measurement JSON, trace/video output and probe crops
      accumulate there across a multi-unit session and are invisible to every
      `git status` the creators and the commander run.
      - Do NOT accept a creator's "git status is clean" as evidence of hygiene — it is
        evidence about TRACKED files only, and repeating it back is how the miss
        propagates. The commander lists the ignored dirs itself.
      - Investigation evidence (screenshots, measurement dumps) legitimately needs to
        exist DURING the unit — the deletion point is at commander acceptance, not at
        creation. Delete it once the numbers it supports have been recorded in the
        durable ledger (Linear comment / HANDOFF), since the recorded numbers are the
        lasting artifact, not the PNGs.
      - Keep a file only when something durable references it by path; then say so
        explicitly in the acceptance notes rather than leaving it silently.
- [ ] Confirm the gate depth actually matches the task size (no M/L task that skipped
      independent review, per § 2 "Independent Review Mandate").
- [ ] For units built on a disputed technical claim (platform limits, undocumented
      behavior), confirm the claim was checked against a primary source, not just asserted
      (see operations.md → Primary-Source Verification).
- [ ] For units that change product behavior/spec, confirm the gate command list included
      the FULL existing test suite affected by that behavior change — not only new tests
      written for this unit (a spec change can silently break an existing suite the brief
      forgot to name; check for separate configs/projects, e.g. an E2E suite that runs
      under a different config than the unit tests).
- [ ] For **user-facing** waves/units, confirm a **real-browser E2E verification pass** of
      the changed behaviors was performed — not just the spec/unit-test suite — and that
      any findings from it were routed back as normal send-backs (added 2026-07-19, per
      user instruction). Do not skip this because code-review rounds came back clean: this
      session's E2E pass caught 5+ genuine defects (event-ordering races, wiring to a
      non-scrollable container, a missing production writer on a boot path) that 2-5
      rounds of code review had missed entirely — E2E and code review catch different
      defect classes, and neither substitutes for the other.
- [ ] For verification criteria (E2E assertions, manual-check scripts, reviewer focus
      areas) covering behavior where **content** matters, confirm the criteria assert
      semantic CORRECTNESS, not mere presence (2026-07-19, per user instruction).
      "Element appears/is visible" is never sufficient where the element's content must be
      right — rewrite as "content X at position Y equals expected Z". Observed: an E2E
      test asserted that a value-readout element appeared while an item was being
      manipulated, but never asserted the value shown matched the item being
      manipulated — the presence-only check kept passing for a long time while the
      displayed value was wrong for every item.
- [ ] For units whose deliverable is **visual geometry** (position, size, alignment,
      spacing, overlap, z-order), confirm the verdict carries **measured numbers**, not
      a screenshot and a claim (2026-08-25, per user instruction — see § 6a). A unit is
      not accepted on "looks right now"; it is accepted on a target/before/after table
      whose after-column matches the target. Presence, semantic correctness and E2E all
      pass on a mis-aligned element — geometry is a defect class none of them cover.
- [ ] For features whose behavior depends on runtime mode (auth-enabled server sync vs
      local-only, online vs offline, PWA service worker, real-device input), confirm
      verification ran in a harness reproducing the DEPLOYED mode, not only the local dev
      default (2026-07-20, per user instruction). If local dev disables a subsystem (e.g.
      auth off → local-only persistence), any verification of that subsystem's features in
      dev mode is insufficient — use the project's synced-mode/fake-auth harness with
      realistic latency injection. Observed: a persistence defect lived exclusively in
      the remote-sync "loading" phase window (~0ms in dev, seconds on the real deployment);
      three rounds of dev-mode verification passed while the deployed app stayed broken,
      and the defect surfaced only when the user exercised the deployment — verification
      that cannot reproduce the deployed timing is not verification.

- [ ] **Behaviour acceptance comes from the 監査役's table, not from the PM/L's own
      hands** (2026-08-29, per owner instruction — `roles-pm-l.md`). Every row of the
      completion definition carries evidence of the *result* of a real operation
      (screenshot of the outcome, click log, measured value, network/console capture).
      "Element/text exists" rows are rejected. The PM/L checks that the evidence exists
      and shows what the claim says; it does not open the browser or run the measurement
      script itself — "Read the diff yourself" above means read the submitted evidence,
      not re-produce it. Observed: an audit reported a timed-playback control as PASS on
      the strength of the surrounding *text* being correct; the control itself had never
      been activated, and threw the first time it was.
- [ ] **No 見せかけ UI** (2026-08-29): every interactive control on touched screens was
      operated once by the auditor with an observable effect recorded, and every
      UI-driven feature traces render → handler → route → backend (+ CSP for external
      resources). A control with no effect, an error, or a "未実装/未接続" notice is a
      FAIL — remove it or connect it; never ship it.
- [ ] **Same-class sweep attached** (2026-08-29): for every defect found this round, the
      report includes the sweep table for the same defect shape across the deliverable.
      A fix-only report is incomplete. **Applies identically to the test harness/tooling
      itself** (2026-09-05, per owner instruction): a session found seven instances of one
      family — the harness silently degrading (a frozen clock, the sandbox listed in its
      own exemptions, a missing `gh` shim, an unread fixture field, comparing raw vs
      wrapped values) — fixed one per round at a full round-trip each, because the sweep
      was ordered only after the pattern became obvious by repetition. The sweep is
      dispatched with the FIRST fix, not after the sixth repeat — a harness/tooling defect
      is evidence of a missing class of check exactly like a product defect is (§ "Same-
      class sweep", `roles-pm-l.md` PM duty 5), not a one-off to patch and move past.
- [ ] **No unrequested change and no self-approved deviation** (2026-08-29): anything
      not in the completion definition or decisions is listed in STATE §6 as a proposal
      with the owner's decision recorded; a creator's "意図的な逸脱" without an owner
      decision is a send-back, not an acceptance note.

Only after every box is checked does the deliverable count as "zero-finding" and get
reported to the user.

## 6a. Geometric verification gate (2026-08-25, per user instruction)

**Why this exists.** A history analysis across a multi-session UI project measured
visual work at roughly a third of all agent time, and found that the largest single
sink inside it was *repeat rounds on the same geometric defect*. A single
centre-alignment defect between two adjacent labels consumed four-plus send-back
rounds, with the user escalating each time because the same defect kept coming back
"fixed". The existing checklist could not catch it: code review reads the diff, E2E
asserts presence and semantics, and both pass on an element that is simply 6px off.
Nobody measured, so each round was a new guess at a fix for an undiagnosed defect.

**The rule.** For any unit whose deliverable is visual geometry:

1. **Diagnose before fixing.** The creator's first action is to measure the current
   state, not to change CSS. "It looks off" is a symptom report, not a diagnosis — the
   diagnosis is a number and the computed style that produces it.
2. **Measure with a script, not by eye.** Use a project measurement script that reports
   `getBoundingClientRect()` plus the relevant computed styles for a named selector list
   (see `operations.md` → Design Fidelity for the script contract). Screenshots are
   supporting evidence, never the verdict.
3. **Verdicts carry a target / before / after table.** Every reported number names the
   selector, the property measured, the target value, the value before the change, and
   the value after. The after-column matching the target IS the GO condition for the
   geometric part of the unit.
4. **Send-backs carry measurements too** — see `briefing-templates.md` → Send-Back Brief.
   The commander translates the user's "まだずれてる" into a measured delta before
   dispatching; sending back an unmeasured complaint is what produces the repeat rounds
   this rule exists to stop.
5. **Two-round escalation rule.** If the same geometric defect survives two send-back
   rounds, STOP patching. The defect is not in the values being nudged; it is in the
   layout method (wrong flex/grid alignment axis, a baseline vs centre mismatch, an
   absolutely-positioned child inside a non-relative parent, a competing specificity).
   Escalate to re-designing the containing element's layout approach from the design
   source — the same "don't patch piecemeal, re-skin from source" discipline that
   `operations.md` → Design Fidelity applies to appearance, applied to geometry.
6. **Scope note.** This gate applies to geometry, not to taste. "AI感が抜けない",
   "ダサい", "世界観に合わない" are design-direction rejections, not defects — they route
   to the user via AskUserQuestion for a direction decision, not to a creator for another
   nudge.

## 7. Model routing

Match the model to the job rather than running everything on the top tier — orchestrated
builds spawn many agents and the cost adds up fast. Prefer the pre-tuned agent roster in
`~/.claude/agents/` (each already carries a sensible default model); pass an explicit
`model` override only when deviating from that default.

> **Project overrides (2026-09-01, owner instruction — discipline lives in the skill,
> not CLAUDE.md):** a project may override the reviewer routing below. A typical
> override: ALL independent review rounds (iterative and final) go to Codex CLI
> (`codex exec --sandbox read-only`); opus reviewers are the fallback ONLY when Codex is
> unavailable (quota/auth), and the substitution is stated in the user-facing report.
> UI eyeball audits (screenshot measurement) stay separate from code review, via
> Playwright-script agents.
>
> **2026-08-03 update (user instruction — supersedes the 2026-07-13 Codex-first
> routing):** all independent reviews — M-size code review, acceptance-critical/L-size/
> architectural review, and design/contract-document review — run on **Claude opus
> reviewers as the base reviewer** for every iterative round (round 1, send-back
> re-reviews). **Codex CLI (`gpt-5.6-sol`) is dispatched exactly once per unit/wave, as
> a third-party final review immediately BEFORE commander acceptance** — after the opus
> review loop has reached GO, and before the commander runs the § 6 acceptance
> checklist. Findings from that final Codex round go back to the creator as normal
> send-backs and are re-verified by Codex (delta-scoped) until Codex reports zero
> findings; only then does commander acceptance proceed. See § 7a for dispatch
> mechanics and the availability/fallback If-Then. Creator, mechanical, and planning
> rows are unchanged.

| Job | Agent to dispatch | Model |
|-----|-------------------|-------|
| Contract/plan authoring (once, up front) | `planner` / `architect` | opus (their default) |
| Feature/test/spec implementation (creators) | `general-purpose` or `tdd-guide` | sonnet |
| **Independent review (iterative rounds) — M-size units, acceptance-critical/L-size/architectural units, and design/contract documents** | language reviewer (`typescript-reviewer`, `python-reviewer`, `rust-reviewer`, …) or `code-reviewer` | **override `model: opus`** |
| **Third-party final review (once, immediately before commander acceptance)** | **Codex CLI**, dispatched via a Bash-capable forwarder subagent (§ 7a) | Codex itself: `gpt-5.6-sol` (`-m gpt-5.6-sol`). Forwarder subagent: **override `model: haiku`** — it only constructs/runs the command and returns Codex's output, it does not itself judge the code |
| ↳ Fallback (Codex unavailable, § 7a) for the final review | same opus reviewer agent as the iterative rounds (a FRESH instance, not the one that ran the iterative loop) | **override `model: opus`** — and state the substitution in the user-facing report |
| Security-sensitive surfaces (auth, input handling, endpoints) | `security-reviewer` | sonnet |
| Build/type failures | `build-error-resolver` (or language variant) | sonnet |
| Mechanical work: file moves, config wiring, git plumbing, commits, status checks — **concrete trigger shapes** (2026-09-05, per owner instruction): adding/fixing a citation or cross-reference, moving/renaming a file, applying a diff/fix someone else already fully stated, writing a commit | `general-purpose` | **override `model: haiku`** |
| Read-only exploration / on-disk state assessment | `Explore` | (its default) |
| Docs/codemap updates | `doc-updater` | haiku (its default) |

Routing principles:

- **Spend the top tier where judgment concentrates**: the plan/contract (written once,
  read by every creator) and the reviews that gate what the user sees. Cheap mistakes in
  a creator get caught by the gate; a weak contract or a blind reviewer poisons everything
  downstream. The opus base reviewer carries the iterative judgment; the single final
  Codex pass adds cross-model independence exactly at the ship gate, where it matters
  most, without spending Codex quota on every round.
- **Creators default to sonnet.** Writing code/tests to a clear brief is well within a
  mid-tier model and is the bulk of the token spend.
- **Everything mechanical goes to haiku.** If the brief could be executed by following
  literal instructions, it does not need sonnet — this now includes the Codex-forwarder
  subagent itself (§ 7a): it runs a CLI command and relays output, it does not review.
  Concretely (2026-09-05, per owner instruction, after a session where citation edits,
  file moves and doc fixes were sent to sonnet in practice despite this rule): a citation
  or cross-reference fix, a file move/rename, applying a diff someone already fully
  stated, and a commit are ALL haiku by default — name the shape, don't default to sonnet
  because the task happens to touch code or docs.
- Don't restart already-running agents just to change their model — finishing on a higher
  tier is cheaper than redoing the work. Apply the routing to *new* dispatches.

## 7a. Codex final-review routing — dispatch, availability, and fallback

**Position in the gate (2026-08-03, per user instruction)**: Codex runs once per
unit/wave, as the third-party final review between the opus review loop's GO and the
commander's § 6 acceptance. Sequence: creator → opus reviewer rounds until GO → **Codex
final review** → (findings? send back to creator, then Codex delta re-verify until zero
findings) → commander acceptance. Do not dispatch Codex while the opus loop still has
open findings, and do not skip the Codex pass because the opus loop came back clean —
clean opus rounds are precisely when the cross-model check earns its keep.

**Independence**: Codex is always a different model/process from whatever Claude agent
authored or reviewed the unit, so the final pass adds a genuinely independent
perspective on top of the creator ≠ reviewer rule (§ 1, § 2). This does not relax
anything else in § 2–§ 6 — the Independent Review Mandate, required briefing elements,
severity handling, and acceptance checklist all still apply in full; only *where in the
sequence* each reviewer runs changed. Gate re-execution stays with the opus round 1
(full) and wave-commit (commander); the final Codex pass is diff-scoped by default and
re-runs scoped tests only, unless the commander's brief says otherwise.

**Dispatch shape**: the commander never calls the Codex CLI directly (that would be the
commander doing review work, forbidden by SKILL.md's one hard rule). Instead, dispatch a
Bash-capable subagent (`general-purpose`, `model: haiku`) as a pure forwarder: it
constructs the `codex exec` invocation from the Reviewer Brief, runs it, and returns
Codex's output verbatim — it adds no judgment of its own. See `briefing-templates.md` →
Codex Reviewer Brief for the exact command shape, prompt-passing method, and sandbox
choice.

**Commander-side watchdog — mandatory at every external-process dispatch (2026-09-02,
per owner instruction, after a ~9h stall):**

A forwarder's self-report that it "armed a Monitor / will wait for completion" is **not
evidence** — the same principle § 2 applies to review self-reports. Forwarders have
repeatedly ended their turn with no live wait mechanism (claimed-armed monitors that
never fired, quota-failure retries never re-armed), leaving completed results unread for
hours until the owner asked. Per-incident nudges do not fix this; the structural rule is:

- **The brief names a deterministic output path** in the session scratchpad (e.g.
  `<name>-prompt.txt` → `<name>.out`) that the forwarder MUST write Codex's full output
  to. The artifact on disk, not the forwarder's relay, is the source of truth.
- **At the same time as dispatching the forwarder**, the commander arms its OWN
  background watchdog (`run_in_background` poll loop, 15-30s interval, bounded timeout)
  on the external process and/or the output path. When it fires, the commander reads the
  output file directly — it does not wait for, or depend on, the forwarder's relay.
- **On quota/limit failures**, the retry schedule is also commander-owned: the watchdog
  timeout doubles as the retry check-in; never rely on the forwarder's promise to re-arm.
- A forwarder that stalls is simply retired (§ 9 transcript-retirement applies); the
  commander already has the result path and loses nothing.

**Availability check and fallback — If-Then (apply before every Codex review dispatch):**

- IF about to dispatch the final Codex review for an M/L or design/contract-doc unit
  THEN have the forwarder subagent first confirm Codex is usable: CLI present (`codex`
  resolves) and authenticated (`codex login status` reports a logged-in state). Skip a
  redundant re-check only when the same commander turn already confirmed availability
  moments earlier for a batched set of reviews — don't shell out the probe once per
  artifact when batching (see § 3 "Batching small reviews").
- **Pre-Codex cleanliness bar (2026-08-03, per user instruction)**: historically, Codex
  rounds failed to converge because they kept surfacing wording inconsistencies and
  definition gaps that upstream stages should have eliminated. Therefore: the creator's
  author-side consistency pass (§ 9a(a)) and the opus iterative rounds MUST treat
  consistency/wording/definition-completeness as first-class finding classes and clear
  them ALL before the final Codex dispatch — the opus loop's GO asserts "no known
  wording/consistency/definition debt remains", not just "no functional defects". This
  is NOT an instruction to tell Codex to ignore such findings (never scope-restrict the
  final Codex brief that way for code units): if Codex still finds one, it counts as a
  normal finding AND as evidence the upstream pass under-delivered — fix it and tighten
  the next opus brief. The § 9a(d) round-2+ "mechanical-implementer-only" narrowing for
  DOCUMENT reviews is unchanged.
- IF the CLI is not found, OR `codex login status` reports not-logged-in/auth error THEN
  Codex is unavailable — do not attempt the review call; go straight to the fallback row
  in § 7's table.
- IF the `codex exec` review call itself exits non-zero, prints an authentication or
  model-rejection error, or the forwarder's Bash timeout is reached with no result THEN
  treat this as unavailable too. One retry of the identical command is acceptable if the
  failure looked transient (e.g. a bare network blip); a second failure is unavailable —
  fall back, don't loop.
- IF Codex runs to completion and returns a review — including a NO-GO verdict or a long
  findings list — THEN that review **is** the third-party final review satisfying the
  gate. Findings, disagreement, or a NO-GO are normal review outcomes, not availability
  failures. Never fall back to a Claude reviewer merely because you'd have preferred a
  different verdict — that defeats the point of an independent second opinion. Findings
  route back to the creator as normal send-backs; the re-verification after fixes goes
  to Codex again (delta-scoped), and commander acceptance waits for Codex zero findings.
- IF any of the above triggered a fallback THEN the final review runs on a FRESH opus
  reviewer per § 7's fallback row (fresh instance — not the reviewer that ran the
  iterative loop, to preserve what independence remains), AND when the commander
  eventually reports this deliverable to the user, state the fallback explicitly and
  specifically — e.g. "Codex 不可のため最終レビューは Opus 代替（別インスタンス）".
  This is not optional: silently substituting reviewers erodes the record of which
  reviewer actually checked the work, the same concern § 2 raises about unverifiable
  self-reports.

## 8. Development standards to enforce in every creator brief

- **TDD.** Tests first (RED→GREEN→REFACTOR), 80%+ coverage on the unit being built.
  Pure logic (calculations, encoders, reducers) must be testable as pure functions.
- **Mutation check at unit completion, not deferred to integration** (2026-09-05, per
  owner instruction): a session that ran mutation testing only at L-size integration found
  40 of 82 deliberately-seeded breakages undetected by the "done" unit tests, forcing five
  already-finished creators to be resumed. Cheap form, done by the creator itself before
  reporting done: for each test that matters, revert the creator's own implementation
  change (not the test), confirm the test now FAILS, then restore the implementation. A
  test that still passes with the fix reverted is not exercising the behavior it claims to
  cover — that is a finding fixed before the unit is reported done, not a gap discovered
  later at integration when the context to fix it cheaply is gone. State in the
  done-report which tests were checked this way. This is required at every unit's TDD
  gate regardless of project size; a fuller automated mutation-testing tool remains
  optional, additional coverage at L-size integration, not a substitute for this.
- **Run it for real.** Every implementation subagent must actually run the test suite
  and the build and report the numbers — not claim green. The reviewer re-runs them
  independently (see § 3 "Reviewer briefing — required elements", item 5 "Mandatory
  gate re-execution", above).
- Immutability, small focused files, boundary validation of untrusted input
  (URLs, localStorage, API responses), explicit error handling, no hardcoded secrets.
- In monorepos, brief creators to run tests with absolute paths from the project root
  and to run separate suites (e.g. Pro vs Core) individually — never mixed globs.
- In Docker projects, "done" includes verifying the route/container actually serves
  (curl returns 200 after volume mount / container recreation), not just green tests.
- **Gate scope must include the full existing test suite affected by a spec/behavior
  change**, not only the tests newly written for this unit. When a unit changes a
  product-level assumption (e.g. changes what "success" means for an operation), name
  every existing suite that assumes the old behavior — including suites under a
  different config/project (a separate E2E config is easy to forget) — in the gate
  command list.
- **Invariants — what must NOT change** (2026-08-25, per user instruction). The Creator
  Brief's `Do NOT edit` line scopes which *files* an agent may touch; it says nothing
  about what must stay the same *inside* a file the agent is allowed to edit. That gap
  is where unrequested changes come from, and they were the second-largest source of
  rework measured in a multi-session history analysis. The four recurring shapes: an
  instruction to move an element that also changed its size; a component swapped for a
  different one when only one of its properties was in scope; an unrelated element
  restyled because the creator judged it ugly; and a change reverted on the creator's
  own initiative without anyone asking for a rollback. Every creator
  brief therefore names an explicit **Invariants** list — the existing appearance and
  behaviour that must be identical before and after — and the creator's done-report must
  state how it confirmed each one survived (diff lines, measured values, or an unchanged
  snapshot). Silence about an invariant is a finding, not a pass. A creator that believes
  an invariant must change reports it as an open issue and stops; it does not decide.
- **Documentation four principles**: code says *How*, tests say *What*, commit messages
  say *Why*, code comments say *Why not* (only non-obvious constraints/rejected
  alternatives). Include this in every creator brief and check it at review.
- **Semantic-consumer sweep** (2026-07-19, per user instruction): when a change alters the
  MEANING of a value shared across files (a coordinate space, a ratio's denominator, a
  unit, an enum's semantics) — not just its identifier name — the creator must locate
  every CONSUMER of that value and confirm each one's interpretation still matches the new
  meaning. Grepping for the identifier is not enough; a consumer can reference the same
  variable name while silently assuming the old semantics. State this sweep explicitly in
  the creator's done-report (which consumers were checked, and how each was confirmed
  consistent), and brief the reviewer to re-check it independently rather than trust the
  creator's list. Observed: one layout component switched to DOM-measured ratios while a
  second component sharing the same ratio kept interpreting it as a uniform-time
  fraction — an identifier-only grep would have found the reference but missed the
  semantic mismatch — and the resulting wrong readout shipped through multiple review
  rounds before being caught.

## 9. Token-efficiency operating rules (2026-07-16, per user instruction)

Background: measured on a UI-refresh session — doc loops of 10-11 rounds, authors
resumed at 500k+ token transcripts, and verbatim finding relays were the top three
token sinks. These rules target ~40-50% reduction at equal quality.

- **Transcript-retirement rule**: resuming a background agent re-reads its entire
  transcript every turn. IF an agent's accumulated transcript exceeds ~300k tokens AND
  the next task can be fully specified by a compact brief + file paths (doc edits,
  applying a findings list, mechanical fixes) THEN dispatch a **fresh** agent instead of
  resuming — the artifact on disk is the state, not the transcript. Resume the *same*
  agent only when in-flight implementation context genuinely cannot be re-briefed
  (mid-TDD, half-written refactor). Retire (stop resuming) giant-transcript agents at
  their current unit's completion. Measured 2026-07-19: resumed creators above this
  threshold cost proportionally more on every send-back round in that session's 20-round
  doc-review loop; a fresh agent with a compact brief was repeatedly cheaper for
  prescribed fixes — keep resuming reserved for genuine mid-TDD state, not convenience.
- **Delta-review gate lightening for wave/multi-round reviews** (2026-07-19): generalizes
  the M-lite gate (§ 1a) to any multi-round review, code or doc — see § 3 item 5 for the
  exact round-1-full vs round-2+-scoped split, and § 3's "Wave batching" paragraph for how
  this combines with dispatching a build in dependency-graph waves. This was the largest
  single cost reduction measured this session: full gate re-execution (whole suite +
  build) had been happening on every Codex round of a wave before this rule.
- **Lint-before-Codex pyramid**: before dispatching a Codex review of a *document*
  (contract/spec/skill), run a cheap haiku/sonnet lint pass first for the mechanical
  finding classes — hedge words, cross-reference validity, grep syntax/escaping, stale
  line numbers, mojibake/encoding damage — so the Codex round only faces substantive
  issues. Code reviews keep going straight to Codex (§ 7); this pyramid applies to
  document reviews only.
- **Delta-scoped re-reviews**: from round 2 onward, scope Codex re-reviews to the changed
  hunks plus a one-paragraph cross-consistency instruction, rather than re-submitting the
  whole document each round. Run **one** final full-document pass before declaring GO —
  the final full pass is mandatory; delta scoping alone misses cross-section conflicts.

## 9a. Design-document review convergence protocol (2026-07-19, per user instruction)

Background: a contract-document review against Codex ran **20 rounds** on one
multi-day session — roughly half that session's total token spend — because
most rounds surfaced only consistency/wording findings that a careful author-side pass
would have caught before the first submission. Apply these rules to every doc
creation/revision brief and every doc-review dispatch:

- **(a) Author-side consistency pass is a mandatory completion criterion**, not optional
  polish, before a document creator reports "done" and hands the doc to review: grep
  every changed concept for ALL its occurrences in the document, verify every §/test-
  number cross-reference actually resolves to the right target, then do a full reread of
  the document from an implementer's perspective — "if I only had this doc, would I build
  the right thing at every point it matters?" — BEFORE submitting to review. State this
  explicitly in the Creator Brief for any doc unit (`briefing-templates.md` → Creator
  Brief).
- **(b) Single-source-of-truth within a doc**: each normative rule is stated exactly
  once; every other place that needs it references that one location instead of
  restating it. When recording a revision, note only the finding-number(s) and the
  location(s) changed — never restate the normative text itself in the revision-notes
  section (a restatement is a second copy of the rule, and the two copies will drift).
- **(c) Round-economy cutoff**: when a review round's findings are ALL
  consistency/wording/style items with no MEDIUM-or-higher implementability defect (i.e.
  nothing a mechanical implementer would build incorrectly from), the commander
  dispositions each finding directly — fix it, or accept-with-stated-rationale — instead
  of sending the whole batch back for another full review round. Any substantive MEDIUM+
  finding always goes back to the creator and always earns a follow-up review round; only
  the wording/consistency tail is commander-dispositioned directly.
- **(d) Reviewer prompt tightening from round 2 onward**: starting with a document's
  second review round, the reviewer brief adds "report ONLY findings a mechanical
  implementer would build incorrectly from — no stylistic items" so the reviewer stops
  re-surfacing wording preferences every round. Round 1 keeps the full severity-tagged
  format (§ 3 above) since round 1 is where implementability defects are most likely to
  still be present. See `briefing-templates.md` → Codex Reviewer Brief for the exact
  round-1 vs round-2+ wording.
