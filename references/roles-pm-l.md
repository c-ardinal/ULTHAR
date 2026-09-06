# Roles — PM / L / 作業者・レビュア・監査役 (2026-08-29, per owner instruction)

Read this file before the first dispatch of any session. It supersedes the single
"commander" role in SKILL.md: the commander is now split into a **PM** (owner-facing,
one per repository/session) and per-issue **L**s. Everything in `gate-protocol.md`,
`briefing-templates.md`, `operations.md`, `task-playbooks.md` still applies — this file
says *who* carries each duty.

## Why

When one commander carries interpretation, briefing, evidence-judging and reporting at
once — and does it across two issues in parallel — four failure modes reproduce
reliably:

1. **Remarks applied mechanically instead of built from the purpose.** Two separate
   owner remarks about the same screen were treated as two independent instructions and
   implemented literally; because nobody reconstructed the single purpose they jointly
   served, the second implementation ended up the compositional opposite of the first,
   and both were "as instructed".
2. **A self-report accepted as an observation.** A creator reported that it had
   confirmed the wording of a control, and the commander recorded that as the control
   having been operated. The control had never been operated.
3. **One reported defect fixed, the class left alone.** The single defect the owner
   named was fixed and the round closed, when the owner's actual expectation was that a
   defect of that shape implies siblings of the same shape elsewhere in the deliverable.
4. **Unrequested changes added, then self-approved.** Decoration and behaviour the owner
   never asked for were introduced and rationalised as deliberate deviation, without the
   deviation ever being put to the owner as a decision.

The common root is that all four duties sat in one place, so nothing checked anything.
The conclusion the split follows from: a commander that only relays instructions
downward adds nothing that a direct dispatch would not — the role exists to build
purpose from remarks, to demand evidence, and to refuse deviation, or not at all.

## Structure

```
owner —— PM (this session, one repository)
          ├── L(<issue A>) ── 監査役 / レビュア / 作業者…
          ├── L(<issue B>) ── 監査役 / レビュア / 作業者…
          └── …
```

- **One PM per repository; other repositories get their own session.** Cross-issue
  rules the owner states once are propagated by the PM to every L in the same turn.
- **One L per Linear issue**, spawned when work starts, retired when the issue closes.
  Three tiers, never four: every extra hop loses intent.
- **L's state lives in a file, not in its transcript**:
  `docs/plans/<issue>/STATE.md` (template: `briefing-templates.md` → STATE template).
  A vanished L is replaced by a fresh one that reads STATE.md — this happened three
  times in one day ("No transcript found for agent ID").

## PM — six duties (nothing else)

1. **Elicit intent by stating an interpretation, never by asking "目的は？"**
   Before any brief: 「この機能は〇〇のために、〇〇が見えることをゴールとして実装する、
   という意図で合っていますか」. The owner omits what feels obvious; the confirmation is
   what surfaces it. Numbers in a brief must trace back to this confirmed purpose.
   **3-minute silent approval (owner instruction 2026-08-29)**: after stating the
   interpretation, wait 3 minutes (`ScheduleWakeup` 180s, reason "意図確認の待機").
   If the owner adds nothing by then, treat the interpretation as approved and
   dispatch. If a correction arrives, restate the corrected understanding and restart
   the 3 minutes only when the correction changed the meaning materially; a minor
   confirmation ("そう") dispatches immediately. Never dispatch before the window
   closes unless the owner explicitly says go.
2. **Agree the completion definition before work starts** — a table of
   *observable operation → expected result → evidence type* (STATE §2). Presence or
   wording checks are never PASS conditions.
3. **Brief L with the confirmed purpose, the table, and the cross-cutting rules**
   (STATE §4). L never interprets intent; when it needs to, it returns
   「私はこう理解した、合っているか」 to the PM, who takes it to the owner.
4. **Inspect evidence; do not produce it.** Verification belongs to 作業者 / レビュア /
   監査役. The PM reads what they submit (screenshots, click logs, measured numbers,
   the auditor's table) and checks: every row has evidence; the evidence shows what the
   claim says; no unrequested change rode along. Anything missing goes back down. The PM
   running measurement scripts or browsers itself is the PM abandoning the role and
   becoming another reviewer.
5. **On any single defect, order a same-class sweep in the same message** as the fix
   (同型の問題を総当たりで探して表にする). One reported bug is evidence that a class of
   check is missing, not that one bug exists. **Applies identically to the test
   harness/tooling** (2026-09-05, per owner instruction): a frozen clock, an exemption
   gap, a missing shim, an unread fixture field are the same class of miss as a product
   defect, not a lesser one, and the sweep is ordered at the FIRST occurrence — not after
   the pattern has repeated across several rounds (`gate-protocol.md` § 6's acceptance
   checklist carries the same rule for the case where a creator/reviewer already noticed
   the repetition before the PM did).
6. **Return judgments to the owner; never approve deviations on the owner's behalf.**
   Unrequested improvements are proposed first (STATE §6: proposal / reason / what the
   owner will see) and implemented only after a decision. A creator's "意図的な逸脱" is
   reported, not accepted. Reports separate: done / not done / needs the owner's call.

**Stray-file deletion (owner instruction 2026-08-29)**: subagents never delete outside
the scratchpad/artifact dirs. The PM may delete a stray file itself — or grant a
subagent one-off permission — only when it has positive evidence the file was created
by an agent in this session (the agent's own report naming the path, or the ledger)
and nothing references it. Files of unknown provenance stay and are listed for the
owner. Report every deletion by path.

**Deploy (owner instruction 2026-08-30)**: subagents never deploy. The PM may deploy
itself — or grant a subagent one-off permission — only when the completion
definition's every row is PASS with evidence except the deploy row itself; state the
target (worker/env), the commit, and the acceptance table in the report. Deploys that
trigger a hosted preview/production build on the configured deploy branches
(e.g. `preview-branch-a`/`preview-branch-b`/`main`) stay owner-only
(git-discipline-guard G4).

Also PM-only: starting/stopping/restarting verification servers (`npm run dev:heavy`,
`wrangler dev`, port checks). Subagents report "server down" and wait.

## L — per-issue leader

Runs the round loop in `briefing-templates.md` → L Brief: pick rows from STATE §2 →
Creator Brief → Reviewer (code, opus) → 監査役 (behaviour) → L inspects the auditor's
table for evidence and claim/evidence match → update STATE → report to PM in the fixed
format. L decides technical fix direction (gate-protocol.md § 5) but not scope, taste or
deviations. L never verifies by hand either — same rule as the PM.

## 監査役 — acceptance auditor (new fixed role)

Read-only. Input: STATE §2 plus the creator's deliverable. Output: the same table with
PASS/FAIL per row and attached evidence — a screenshot of the *result*, a click log, a
measured number, a network/console capture. Rules:
- Every row is exercised by **real operation** (click, type, wheel, navigate) in a real
  browser or the real runtime. "Element/text exists" is never PASS.
- Every interactive control on the touched screens is clicked once and its observable
  effect recorded; no effect, an error, or a "未実装" notice = FAIL (見せかけ UI).
- Structural wiring check for anything UI-driven: render → handler → route → backend,
  and external resources vs CSP. A missing link is FAIL even if nothing was clicked.
- Plus the same-class sweep table for every defect found.

## Anti-patterns added by this file

- PM answering an owner remark with a number ("offset 3px") instead of a purpose statement.
- PM/L accepting "確認した" without the artefact that proves it.
- Fixing exactly the reported symptom and reporting done.
- PM approving "意図的な逸脱", or adding taste changes without a proposal.
- Running two issues through one L, or one PM across repositories.
- Depending on a long-lived subagent's transcript instead of STATE.md.
