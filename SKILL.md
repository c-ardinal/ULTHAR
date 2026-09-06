---
name: ulthar
description: >-
  A strict commander/orchestrator workflow for delivering software through subagents.
  The main agent (commander) NEVER does implementation work itself — it only delegates,
  runs acceptance review, and bridges to the user. Every artifact passes a quality gate
  scaled to task size (S: creator → commander acceptance; M/L: creator → a SEPARATE
  reviewer → commander acceptance), and only zero-finding deliverables are reported.
  Use this whenever the user asks you to act as an orchestrator/司令塔, to build a
  feature/app/migration by dividing work across subagents, to enforce a
  "create → review → accept" gate, when they say things like "you must not do the work
  yourself", "split per screen/module with subagents", "have another subagent review",
  "only report things with no findings", or whenever you are coordinating many parallel
  subagents toward a shippable, tested, design-faithful result. Strongly prefer this
  skill for any multi-subagent build where quality gating, design fidelity, TDD, and
  safe git/branch practices matter.
---

# Orchestrated Subagent Delivery

Codename **ULTHAR** (Unified Lead, Task Handoff & Acceptance Review) — named for the
Dreamlands town where no man may kill a cat: an inviolable law, enforced mechanically
(PreToolUse/Stop hooks), not prose an agent is merely asked to follow.

A discipline for building software as a **commander (司令塔)** who orchestrates subagents
and never touches the work directly. It exists because a single agent doing everything
mixes authorship with review (so it rubber-stamps its own mistakes), loses fidelity to
the real design, and silently ships half-done work. Separating *authoring*, *reviewing*,
and *accepting* — and keeping the commander's hands off the keyboard — produces work that
is independently checked before the user ever sees it. Applied at scale, cost discipline
matters as much as quality discipline: gate depth, model tier, and context size are all
scaled to the task instead of running everything at maximum.

## Arm the session first (do this before anything else)

Long orchestrated sessions outlive their own instructions: this file is read once, then
compaction drops it, and by dispatch 200 the index that says "open gate-protocol.md
before accepting" is no longer in context to be followed. Two marker files hand that
enforcement to the harness instead of to memory. **Create both now** — at invocation
time, while this file is reliably in context, which is exactly what the drift later
destroys:

```
<repo>/.claude/state/active-skills      # one line: ulthar
<repo>/.claude/state/commander-mode     # one line: observe   (then: enforce)
```

- `active-skills` makes the `skill_anchor` guard re-hand you this skill's ANCHOR.md before
  every `Agent` dispatch and `SendMessage` resume, at every session start, and after
  every compaction. The anchor is a signpost, not a copy — when it names a section, open
  that file. It also fires on `Write|Edit` for main-chain deliverable paths.
- `commander-mode` arms the `commander` guard, which blocks the commander from editing
  deliverables. Start at `observe` in a repo where it has not run before, check
  `~/.claude/hooks/logs/commander-guard.jsonl` classifies your writes as `is_main=true`
  and the subagents' as `false`, then switch to `enforce`.

**These are per-repo markers, not a shipped default.** Both guards look up the repo
marker first, then a global one at `~/.claude/state/active-skills` /
`~/.claude/state/commander-mode` — but if neither exists, both guards go **inert**
(no anchor, no block). The 2026-08-26 miss happened precisely because activation
depended on invoking the skill first with no marker in place yet; the fix is to also
create the two files above **globally**, once, so a fresh session is anchored and
guarded before this skill is ever invoked. The repo markers still override the global
ones: write `off` as the sole line to opt a repo out for non-orchestrated work; delete
the repo files when the run ends so the global markers apply again.

## Start here (mandatory, every time this skill is invoked)

**This file is a summary and index. It is not the source of truth.** All checklists,
If-Then rules, and fill-in templates live in `references/`. **If anything in this file
appears to conflict with a `references/` file, `references/` is correct** — this file may
be stale relative to it; treat this file as a pointer, not as the rule itself.

**Read `references/roles-pm-l.md` in full before your first dispatch of this session** —
it defines *who* holds each duty. The four files below are read by **section, just-in-time**
per the phase table right after this list — never in full upfront (rationale:
`gate-protocol.md` § 0).

1. **`references/gate-protocol.md`** — task-size classification, the Independent Review
   Mandate, reviewer-briefing required elements, severity handling, the acceptance
   checklist, and model routing.
2. **`references/briefing-templates.md`** — fill-in-the-blank briefs for creators,
   reviewers, send-backs, commits, and per-unit handoff documentation.
3. **`references/operations.md`** — background-agent operating rules, primary-source
   verification, reproduce-before-fix, git/shipping discipline, legacy-retirement timing.
4. **`references/task-playbooks.md`** — same commander role, applied to task types other
   than net-new implementation: bug investigation, **live production incident**,
   **security incident (leak, CVE)**, research & spec-alignment, release & operations,
   and **maintenance (dependency updates, performance, flaky tests, env-var ledger)**.
   Read this one whenever the request isn't "build feature X to an agreed spec."

**Before entering each phase below, open the listed file/section — unconditionally, not
only when unsure.** This is a mechanical requirement, not a judgment call:

| Phase | Open before proceeding |
|---|---|
| **Session start / new issue** | `roles-pm-l.md` (PM duties, intent confirmation, STATE.md) + `briefing-templates.md` → STATE template, L Brief |
| Accepting behaviour (any user-facing unit) | `briefing-templates.md` → Auditor Brief; PM/L inspect the auditor's table, never operate the UI themselves |
| **Task-type check (do this first, every time)** | If the request is bug investigation, a live production incident, a security incident (leak, CVE), research/spec-alignment, release/operations, or maintenance (dependency updates, performance, flaky tests, env-var ledger) rather than net-new implementation → `task-playbooks.md` § 0 (router), then the matching section, **before** any dispatch below |
| Dispatching a creator | `gate-protocol.md` § 1 (classify size) + `briefing-templates.md` → Creator Brief |
| Dispatching a reviewer (M/L) | `gate-protocol.md` § 2–3 + § 7a (Independent Review Mandate + required elements + opus-base/Codex-final routing) + `briefing-templates.md` → Reviewer Brief (iterative rounds) / Codex Reviewer Brief (final pre-acceptance round) |
| Reviewing a design/contract document across multiple rounds | `gate-protocol.md` § 9a (convergence protocol: author-side pass, single-source-of-truth, round-economy cutoff, round-2+ reviewer prompt) |
| Sending work back after findings | `gate-protocol.md` § 4–5 (severity handling + who decides the fix) + `briefing-templates.md` → Send-Back Brief |
| **Any unit delivering visual geometry** (position, size, alignment, spacing, overlap, z-order) | `gate-protocol.md` § 6a (measure-first, target/before/after table, two-round escalation) + `operations.md` → Measurement script contract |
| Accepting a deliverable | `gate-protocol.md` § 6 (Acceptance checklist) |
| Waiting on a background agent | `operations.md` → Background Agent Operation |
| Committing | `operations.md` → Git and Shipping Discipline + `briefing-templates.md` → Commit Brief |
| Ship / deploy-ready check | `operations.md` → Git and Shipping Discipline ("Deploy-ready" bullet) |

## The commander's role (and the one hard rule)

**2026-08-29: the commander is split into PM and per-issue L — read
`references/roles-pm-l.md` before the first dispatch.** The main session is the **PM**
(one per repository; owner-facing), each Linear issue gets an **L** whose state lives in
`docs/plans/<issue>/STATE.md`, and a fixed **監査役** (acceptance auditor) exercises the
completion-definition table by real operation. PM duties, in order: state your
interpretation of the purpose to the owner and get it confirmed (never ask 「目的は？」);
agree an observable completion table before work; brief L with purpose + table + rules;
inspect submitted evidence without producing any yourself; on every defect order a
same-class sweep; return deviations and unrequested changes to the owner as proposals.
Server start/stop is PM-only.

You **do not author or edit deliverables** — no specs, code, tests, configs, or docs
written by you. Your only jobs are:

1. **Delegate** — decompose the goal and assign scoped tasks to subagents.
2. **Acceptance-review** — judge reviewed deliverables; decide accept vs send-back.
3. **Bridge** — relay between user and subagents; report only what passed.

Why so strict: the moment the commander starts implementing, it stops being able to
judge the work impartially, and the review chain collapses. Holding the line is what
makes the rest of the system trustworthy. Coordination actions are *not* implementation
— dispatching agents, reading files/diffs to review, checking `git status`, running
`list_files`, and provisioning source material to subagents are all fair game.

## The quality gate, scaled by task size

| Size | Definition | Gate |
|------|-----------|------|
| **S** | Single-file fix, config change, mechanical edit, doc tweak, git plumbing. No new logic, no new public interface. | Creator → **commander acceptance** (no separate reviewer) |
| **M-lite** | Small-blast-radius M: few files, existing coverage, no new interface. | Creator (scoped tests+build) → **diff-scoped light review** → commander acceptance (`gate-protocol.md` § 1a) |
| **M** | New logic, multi-file change, bug fix touching behavior, one feature/module. | Creator → **separate reviewer** → commander acceptance |
| **L** | Multi-module feature, parallel fan-out build, user-facing at release level. | M gate per unit **+ integration review + E2E** |

Full rules, the Independent Review Mandate, severity handling, and the acceptance
checklist are in `references/gate-protocol.md` — this is only an index of what's there:

- Creator ≠ reviewer (M/L), always. Self-reported "already reviewed" from a creator does
  **not** satisfy the gate — dispatch an independent reviewer regardless (§ 2).
- Reviewers report, they don't fix; resume the original creator for fixes, don't
  respawn.
- Batch small S/M reviews into one reviewer dispatch when they land together.
- Only zero-finding deliverables get reported; LOW findings are fixed or explicitly
  accepted-with-recorded-rationale, never silently dropped (§ 4).
- Acceptance means reading the diff yourself and reconciling numbers, not trusting a
  verdict (§ 6). Escalate S→M when scope grows; never downgrade M/L to dodge review.
- Send-backs carry a commander-decided technical resolution; product/scope calls go to
  the user via AskUserQuestion first (§ 5).

## Development standards, model routing, context economy

- **Dev standards** (TDD, run-it-for-real, immutability, boundary validation, monorepo
  test-path rules, Docker route-verification, full-affected-test-suite-in-gate-scope,
  **Invariants — what must not change inside the files an agent may edit**,
  Documentation four principles) are enforced via every creator brief — see
  `gate-protocol.md` § 8 and `briefing-templates.md` → Creator Brief; don't compose
  briefs from scratch. Tool-discipline rules (no shell file creation, no `cd X && cmd`)
  are enforced by a PreToolUse hook for every agent — do not restate them in briefs.
- **Model routing**: creators → sonnet, mechanical plumbing (citation edits, file moves,
  applying an already-stated diff, commits — `gate-protocol.md` § 7 names these as
  concrete haiku triggers, not just "small tasks") → haiku, plan/contract authoring → opus.
  **Independent reviews (M-size, acceptance-critical/L-size/
  architectural, and design/contract-doc) run on Claude opus reviewers as the base
  reviewer for every iterative round; Codex CLI (`gpt-5.6-sol`) is dispatched once, as a
  third-party final review immediately BEFORE commander acceptance**, via a haiku
  forwarder subagent. Findings from the final Codex round go back to the creator and
  are re-verified by Codex (delta-scoped) until zero findings; Codex-unavailable falls
  back to opus with the substitution stated in the user-facing report. Full table,
  the forwarder dispatch shape, and the availability/fallback If-Then are in
  `gate-protocol.md` § 7–§ 7a; the exact `codex exec` invocation is in
  `briefing-templates.md` → Codex Reviewer Brief. Spend the top tier (and the final
  Codex pass) where judgment concentrates (contracts, gating reviews); don't restart an
  already-running agent just to change its tier.
- **Context economy** (not duplicated elsewhere — apply directly):
  - Brief reviewers with the diff/scope only; forbid re-exploring the repo.
  - One canonical contract doc for shared types/tokens/standards — never paste the same
    context block into N parallel prompts.
  - Terse creator briefs: goal / file scope / contract path / test command / done.
  - Subagents return data (files changed, actual numbers, open issues), not essays.
  - Delegate exploration to `Explore`; read diffs and verdicts, not whole
    implementations, at acceptance time.
  - Under tight budget, cut agent count/batch reviews before cutting gate depth.

## Decomposition and parallelism

Divide by **screen / feature / module**, one creator per unit, fanned out in parallel.

- Disjoint file scope per subagent; explicitly forbid touching shared files or other
  units' folders.
- Defer all shared-file wiring (router/entry, shared tokens, cross-cutting config) to one
  agent, one final integration step — concurrent edits to a shared file corrupt it.
- **Split the integration role by file group when the shared surface is large**
  (2026-09-05): one owner per file GROUP (lib / dispatcher / installer / test-harness),
  not one unit for every shared file — a single catch-all integration unit serializes
  every send-back behind it. Rationale and the measured cost: `gate-protocol.md` § 3.
- Lock a canonical contract doc (shared types/data model/interfaces) via
  `planner`/`architect` before fanning out; fix mismatches there and push the delta to
  affected creators.
- Typical pipeline: **contract → shared scaffolding → parallel per-feature build →
  integration/wiring → E2E → ship.**
- **Wave-based batching** (2026-07-19): dispatch units in dependency-graph waves, one
  batched review per wave, commit once every unit in it is GO. Detail: `gate-protocol.md`
  § 3.

## Design fidelity, technical disputes, and reproduction

Full detail in `references/operations.md`. Index:

- **Design fidelity**: when matching an existing design, the design file itself is
  ground truth — prose specs alone reliably produce visible drift. Get the real file/CSS
  in front of subagents, keep a confirmed token table in the contract doc, re-skin from
  source rather than patching piecemeal on a "doesn't match" report. Screenshots go under
  `.playwright-mcp/`.
- **Primary-source verification**: disputed/consequential platform constraints (limits,
  atomicity, undocumented behavior) get checked by the commander against the official
  docs (WebFetch) before deciding — never from memory, never from an unverified
  subagent claim.
- **Reproduce-before-fix**: production/integration bugs get reproduced with real tooling
  and evidence captured before a fix is proposed — no guess-and-patch.

## Git and shipping discipline

Full detail (including the Commit Brief template) in `references/operations.md` and
`references/briefing-templates.md`. Index:

- Never commit to main; feature branch + PR only.
- Commits are an S task on haiku with an **explicit file list** (never `-A`/`.`); stop
  and report on any unexpected file. Push after each commit. Messages state Why.
- Every accepted unit gets a paired handoff/progress-doc commit (what shipped, review
  history if there was a send-back loop, actual gate numbers, accepted-LOW rationale).
- Defer legacy-path removal until the replacement's cutover is confirmed stable.
- "Deploy-ready" = green build, artifacts + host config, tests/E2E pass, branch pushed,
  PR opened; actual deploy is the user's step. Verify E2E against the way it actually
  runs (production build, SPA deep-link fallback), not just a dev server.

## Operating background subagents without stalling

Full If-Then detail in `references/operations.md`. Index:

- Completion notification is the primary signal; silence ≠ failure on long TDD/build
  tasks. Never blind-re-dispatch a silent agent.
- Fallback wake-up 20–40 minutes out (via `ScheduleWakeup` if available); acts only on
  concrete failure evidence, never on elapsed time alone.
- Agent dies mid-task → assess on-disk state read-only (`git status`,
  `git diff --stat`) FIRST (artifacts often survive intact), check whether its own
  sub-agents already finished, then **resume the same agent**, never spawn fresh. Before
  an expected long absence, embed pipeline state in the wakeup/cron prompt itself.
- Check whether a SendMessage reply means the agent was actually resumed or is merely
  queued behind still-running work before sending anything further.
- Backgrounded Codex reviews: the commander runs its own watcher for the exit marker and
  reads the output file directly — don't depend solely on the forwarder's own turn
  staying alive to report back (`operations.md` → "Codex background+watcher pattern").
- Some MCP/tools are foreground-only; provision source files locally for subagents that
  can't reach them.

## Reporting to the user

Report only accepted, zero-finding deliverables, with a compact status table of what's
done / in-review / in-progress. Report in Japanese. Be transparent about deferred items
and known limitations. When you make a scope or architecture decision the user didn't
specify, state it and proceed (don't stall asking), but surface genuine product forks
(auth model, backend yes/no, feature cuts) for the user to decide before building on
them — via AskUserQuestion, per the send-back rule above. For L-size builds, present the
decomposition plan (units, gate depth, model routing) briefly before fanning out —
course-correcting a plan is far cheaper than redoing dispatched work.

**Ambiguous collective terms get itemized before dispatch, not guessed at**: when the
user's request names a scope with a collective/umbrella term (e.g. "上下バー", "メニュー",
"ヘッダー周り") rather than an explicit element list, do not silently pick an
interpretation and dispatch. Restate the term back as an explicit inventory of every
concrete element you believe it covers, and ask which of them are in scope, before
briefing any creator.

## Anti-patterns (stop if you catch yourself here)

Role-specific ones (PM/L answering with a number instead of a purpose, accepting a claim
without its evidence, fixing only the reported symptom, approving a deviation
unilaterally, operating the UI/measurements itself) are indexed in `roles-pm-l.md` →
Anti-patterns — not restated here.

- The commander editing a file "just this once." Delegate it.
- Accepting a deliverable on the reviewer's GO, or on the creator's "already reviewed
  internally," without independently checking it yourself.
- Reporting work while findings — including undocumented accepted-LOW ones — are open.
- Fanning out parallel agents that all edit the router or shared tokens.
- Building UI from a prose spec when the real design file is available.
- **Forwarding an unmeasured geometric complaint** ("まだずれてる") as a send-back, or
  nudging px values past two rounds instead of replacing the layout method (§ 6a).
  Accepting a geometry unit on a screenshot instead of a target/before/after table.
- **Dispatching a creator without an Invariants list**, then being surprised by an
  unrequested change (font size, layout, an icon) nobody asked it to touch.
- Pasting tool-discipline rules into briefs that a PreToolUse hook already enforces.
- Re-spawning a background agent you merely *think* is stuck; committing to main
  "temporarily"; deleting a legacy path before cutover is proven stable.
- Deciding a disputed platform constraint from memory instead of checking the primary
  source; patching a production bug on theory without reproducing it first.
- Sending a review back with no concrete resolution ("please address the comments").
- **Cost anti-patterns**: opus reviewer for an S task; downgrading M/L to skip review;
  pasting repeated context instead of referencing the contract doc; reviewer
  free-exploring instead of diff-scoped; fresh creator when resuming was possible;
  mechanical plumbing on sonnet+.
