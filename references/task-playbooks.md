# Task Playbooks — investigation, research, and release-ops tasks

Read this file whenever the user's request is NOT net-new implementation — bug
investigation/fix, market/technical research, spec/requirements alignment, or
release/operations work. The commander role (delegate / acceptance-review / bridge,
SKILL.md § "The commander's role") and the quality gate (`gate-protocol.md`) still apply
unchanged; this file adds the extra If-Then discipline these task types need that plain
feature-building doesn't. Where a rule already exists in `gate-protocol.md` or
`operations.md`, this file points to it instead of restating it — do not duplicate, do
not let this file drift from those.

## 0. Task-type router

**Match by meaning, not by literal string. Apply this table regardless of the user's
language** — a Japanese request ("バグです", "調査してください", "本番反映して") routes
exactly like its English equivalent below; do not require an English keyword to trigger
routing.

IF the incoming request matches a row below, THEN open that section before dispatching
anything:

| Signal in the request (English) | Signal in the request (日本語) | Section |
|---|---|---|
| "bug", "broken", "doesn't work", "regression", "error in prod/staging", reported unexpected behavior | バグ、不具合、壊れた、動かない、落ちる、エラーになる、直らない、おかしい | § A |
| "research", "investigate options", "compare", "which library/approach", spec unclear or contradictory, PO/user disagrees with a prior assumption | 調査、リサーチ、検討、比較して、どっちがいい、仕様が曖昧、認識齟齬、話が違う | § B |
| "deploy", "release", "cutover", "migrate data", "runbook", "rollback", post-deploy check request | デプロイ、リリース、切り戻し、本番反映、移行、手順書、ロールバック | § C |
| "prod is down", "outage", "emergency", "incident", "everyone is affected", "site is 500ing", currently-ongoing production impact | 本番が落ちている、障害、緊急、インシデント、全員使えない、500エラーが出ている | § D |
| "leaked", "exposed key/credential", "vulnerability", "CVE", "security report" | 漏えい、キー流出、脆弱性、CVE、security | § S |
| "update dependencies", "npm audit", "upgrade package", "it's slow", "performance", "flaky test", "intermittently failing" | 依存更新、アップデート、npm audit、遅い、重い、パフォーマンス、フレーク、たまに落ちる | § E |
| Plain "build/add/implement feature X" with an agreed spec | 実装して、追加して、作って（合意済み仕様に基づく新規機能） | Not this file — proceed with SKILL.md's normal build flow |

A single user request can span sections (e.g. research resolves a fork, then build
proceeds under normal gates, then release follows § C) — treat this as a phase
transition, not a reason to pick only one section.

### 0a. Tie-break when ambiguous or multiple rows match

1. **§ D is always first** when there is current, ongoing production impact, regardless
   of which other row also matches. A live outage that is also "a bug" or "a security
   report" still routes to § D first — stabilize, then the other section's process
   applies during/after § D3's handoff to § A (or § S, if the root cause is
   security-related — see § D3). Do not let "this is really a § A bug" reasoning delay
   the stabilize-first step in § D1.
2. **Default to the cautious path (§ A or § B)** when the request is ambiguous, matches
   more than one row, or doesn't clearly match any row but smells like something already
   went wrong or isn't yet agreed. Treating an unclear request as "just build it" is the
   failure mode this router exists to prevent — when in doubt, investigate or align
   first, don't build first.
3. **Exception — direct to the normal S/M gate**, skipping § A/§ B, only if the user has
   already identified BOTH the root cause AND the specific fix, with concrete evidence
   (file:line reference, or reproduction evidence already captured/attached) — not just a
   confident-sounding description. Even then, do **not** skip the evidence-verification
   step: perform the § A2-equivalent check yourself (Read the referenced file:line,
   confirm the stated cause actually matches what the code does) before dispatching the
   fix as a creator brief. If that check fails to confirm the user's claim, fall back to
   § A rather than proceeding on an unconfirmed premise.

## A. Bug Investigation & Response Playbook

### A1. Reproduce before diagnose — no exceptions

This extends `operations.md` → Reproduce-Before-Fix; that rule already bans
guess-and-patch for production/integration bugs. Add: **code-reading alone is not
diagnosis**, even when the commander or a subagent is confident. Treat every
code-only theory as unconfirmed until an evidence step below confirms or kills it.

IF a bug is reported → THEN dispatch a **read-only investigation agent** (`Explore` or
`general-purpose` with no Edit/Write) whose sole job is reproduction + evidence
capture, before any fix is proposed. Do not let the same agent both diagnose and fix in
one dispatch — keep the phases separate so a wrong diagnosis doesn't get papered over by
a plausible-looking patch.

### A2. Evidence-based report format (mandatory brief for the investigation agent)

```
You are investigating only. Do not edit any files.
Reproduce: <exact steps / URL / input that triggers the bug>
Capture and report verbatim:
  - Exact error message / stack trace text (not paraphrased)
  - Actual network response body + status code (not "it failed")
  - Relevant DOM/state snapshot or screenshot if UI-related
  - The specific code location(s) the evidence points to (file:line)
For each hypothesis you form, label it explicitly:
  - HYPOTHESIS: <theory>
  - STATUS: confirmed by <evidence> / rejected by <evidence> / untested
Do not report a diagnosis as fact unless it is CONFIRMED by captured evidence.
```

Acceptance rule: the commander does not accept a diagnosis, and does not dispatch a fix
creator, until at least one hypothesis is CONFIRMED this way. An investigation report
containing only untested hypotheses is incomplete — send it back for more evidence, not
forward to a fix. A CONFIRMED hypothesis is necessary but not sufficient — see § A2a
before accepting the report as complete.

### A2a. One confirmed cause does not close the investigation (2026-07-19, per user instruction)

IF at least one hypothesis reaches CONFIRMED (§ A2) → THEN the investigation is still not
complete. For **each reported symptom**, enumerate every plausible mechanism that could
produce it, and explicitly disposition each one as confirmed / eliminated (with evidence)
/ untested — not just the first mechanism that happened to confirm. Explicitly include
device-only paths a headless browser cannot exercise, and rule them in or out rather than
skipping them as untestable:

- **BFCache / back-gesture restore** (a page revived from cache on browser back-navigation,
  not re-executing normal load logic).
- **PWA standalone mode** (behavior specific to the installed/home-screen app shell, not
  reproducible in a normal browser tab).
- **Real-touch jitter** (actual finger/pointer noise on a touchscreen, not reproducible by
  a synthetic/headless pointer event).

Where headless tooling genuinely cannot exercise one of these, fall back to § A3's
manual-repro workaround (a real device, an equivalent manual step) rather than leaving it
as a silent gap. A report that confirms one mechanism without dispositioning the others is
incomplete — send it back, do not forward it to a fix creator. This is first-cause bias:
this session shipped a cross-tab-only fix while the real path was BFCache restore, because
the investigation stopped at the first confirmed mechanism instead of checking whether
other reported symptoms had a different cause.

### A2b. Environment-parity verification (2026-07-20, per user instruction)

IF a feature's behavior depends on runtime mode (auth-enabled server sync vs local-only,
online vs offline, PWA service worker, real-device input) → THEN verification MUST run in
a harness reproducing the DEPLOYED mode, not only the local dev default. Concretely: if
local dev disables a subsystem (e.g. auth off → local-only persistence), any verification
of that subsystem's features in dev mode is insufficient — use the project's
synced-mode/fake-auth harness with realistic latency injection. Observed: a persistence
defect lived exclusively in the remote-sync "loading" phase window (~0ms in dev, seconds
on the real deployment); three rounds of dev-mode verification passed while the deployed
app stayed broken, and the defect surfaced only when the user exercised the deployment —
verification that cannot reproduce the deployed timing is not verification.

### A2c. User reports "not fixed" — diff environments before re-investigating (2026-07-20, per user instruction)

IF a user reports that something already verified as fixed is "not fixed" → THEN FIRST
diff the verification environment against the user's environment (deployed URL/branch,
PWA service-worker version, runtime mode) before re-investigating the code. A mismatch
here (verification ran on a stale build, a different branch, or dev-mode with a subsystem
disabled that the user's environment has enabled — see § A2b) is a common and
cheap-to-check explanation; rule it out before spending turns re-diagnosing logic that was
already confirmed correct.

### A3. When automation is blocked — design a workaround before asking the user

IF an automated repro (headless browser, CI job, sandbox) is blocked by something outside
the agent's control (CAPTCHA, auth wall, environment-only bug) → THEN, in this order:

1. **Design an equivalent manual/clean-environment repro** the agent CAN run itself
   (e.g. a fresh private/incognito browser profile to rule out cached state or extension
   interference, a curl/API-level equivalent of the UI action, a local emulation of the
   blocked environment). Try this before involving the user.
2. **Only if (1) is genuinely not possible**, design a **minimal single-action** request
   to the user — never a vague "can you check this?". State exactly what to open, what to
   click, and which single value to report back:
   ```
   Please open <exact screen/URL> and do <one exact action>.
   In DevTools → Network, find the request to <endpoint>.
   Report back just: the "status" field value shown for that request.
   ```
3. Do not deep-dive retrying the same blocked automated path more than once — recognize
   the block and move to (1) or (2) rather than burning turns on repeated identical
   attempts.

### A4. Gate hypotheses on real data, not plausibility

IF a hypothesis can be checked against data already available (user-supplied logs, an
environment config dump, prior investigation evidence) → THEN check it there before
asking the user another question. When a new fact does arrive from the user, re-evaluate
which hypotheses it kills — one fact often eliminates several branches at once, so pick
the next question (if still needed) for maximum discriminating power, not just the next
item on a list.

### A5. Regression bisection (isolated, disposable)

IF the bug is a regression (worked before, broken now) and the introducing commit is
unknown → THEN:

1. Create an isolated `git worktree` for the bisect (`EnterWorktree` / `git worktree add`
   at a separate path) — never bisect in the main working tree while other agents may be
   using it.
2. **Do not share `node_modules`/build caches via symlink into a worktree you intend to
   delete** — deleting the worktree can delete the shared target. Install fresh inside the
   worktree instead.
3. Run `git bisect` with an automated evidence check per commit (the reproduction from
   § A2, scripted) where possible; manual bisection with the same repro steps otherwise.
4. On finding the introducing commit, **always** clean up: `git worktree remove` the
   bisect worktree, confirm `git status` is clean on the main tree afterward.
5. After identifying the commit, classify it before deciding where to fix:
   - **"Product is right, test is stale"**: the commit intentionally changed behavior and
     an existing test still asserts the old behavior → the fix target is the test, and
     this needs an explicit, stated decision (product/scope call → confirm with the user
     if the intent is ambiguous, per `gate-protocol.md` § 5).
   - **"True regression"**: the commit broke intended behavior → the fix target is the
     code, routed through the normal creator/reviewer gate below.

### A6. Fix through the normal gate; retire diagnostic scaffolding

Once root cause is CONFIRMED (§ A2), the fix itself is a normal S/M/L unit — classify it
with `gate-protocol.md` § 1 and run the full creator → (reviewer if M/L) → acceptance
gate. Do not let "we already understand the bug" skip the independent review mandate for
M/L fixes.

- IF the investigation added temporary diagnostic code (extra logging, a debug endpoint,
  a flag to force a code path) → THEN track it explicitly and confirm its removal is part
  of the fix unit's diff before acceptance. Diagnostic scaffolding left in the shipped
  fix is itself a findable review issue — brief the reviewer to check for it.

### A7. Record the lesson

Every confirmed root cause + fix gets a note in the project's HANDOFF/durable doc (reuse
the Per-Unit Handoff Documentation Brief in `briefing-templates.md`, add a "Root cause"
and "How this class of bug is now prevented/detected" line). This is what keeps the same
bug class from recurring across sessions — do not treat it as optional polish.

Proportional to size: an S-size trivial fix only needs a one-line HANDOFF entry
(what broke, one-line cause); reserve the full format above for M/L fixes and for any bug
whose class is likely to recur.

## B. Research & Spec-Alignment Playbook

### B1. Stop on spec conflict — do not build through ambiguity

IF, at any point, the commander detects that the user's request conflicts with an
already-agreed spec, or two stated requirements are mutually exclusive → THEN:

1. **Immediately stop any in-flight creator agent** working on the affected unit
   (`TaskStop` or equivalent) — do not let it keep building on a premise that may be
   wrong. Resuming after realignment is cheaper than unwinding shipped-then-reverted work.
2. Do not guess the resolution yourself and proceed. Move to § B2.

### B2. One-question-at-a-time alignment loop

Resolve conflicts via `AskUserQuestion`, one question per turn, each with:

- 2–4 concrete options (not open-ended "what do you want?").
- A **stated recommendation** with a one-line reason, even though the user decides.
- Ask in **dependency order**: resolve the fork whose answer changes what the later
  questions even are, first. Don't ask five independent-looking questions when answering
  #1 would make #3 moot.

```
Question: <the fork, stated concretely>
Options:
  A. <option> — recommended: <why in one line>
  B. <option>
  C. <option>
Trade-off note: <what each option costs/gives up, if not obvious from the options alone>
```

### B3. Durably record every confirmed decision — the chat log is not the record

The moment a decision is confirmed, dispatch an S-task creator (mechanical, haiku-tier
per `gate-protocol.md` § 7) to commit it to a durable artifact **in the same turn**, not
"later":

- A spec/contract file (new or updated section) with the confirmed decision stated as a
  fact, not a discussion.
- The project's HANDOFF/PO-decisions ledger updated with the same fact.
- Commit both via the normal Commit Brief (`briefing-templates.md`).

A decision that lives only in the conversation transcript is not durable — the next
session (or the next context compaction) loses it.

### B4. Research sourcing discipline

- **Primary sources first.** Official docs, actual data, the real API response — not
  secondhand summaries. This extends `operations.md` → Primary-Source Verification from
  "disputed platform constraints" to research tasks generally: any consequential claim
  gets checked at the source before it's used to decide something.
- **Cross-check with a second independent source** before treating a non-trivial claim as
  settled, especially for market/competitive research where a single source may be
  stale, biased, or wrong.
- **Label every claim** in the output as one of: **Fact** (source-confirmed), **Estimate**
  (derived/inferred, state the method), or **Unconfirmed** (single weak source or
  contradicted elsewhere). Never present an Estimate or Unconfirmed claim unlabeled.
- **Delegation boundary**: the commander may use `WebFetch`/`WebSearch` directly only to
  confirm a single disputed fact already narrowed down (one URL, one claim). Any broader
  research (surveying multiple sources, open-ended market scans) is delegated to a
  subagent — the commander stays in the delegate/accept role, not the research-execution
  role (SKILL.md's one hard rule applies here too).
- Research deliverables go through the same S/M gate as code: a research report with
  consequential conclusions is at least an M-task (separate reviewer checks the sourcing
  and labeling, not just the prose).

### B5. Large design phases: contract → review → approval → split

For research/spec work that precedes a large (L-size) build, do not let "we researched
it" substitute for the existing contract-doc gate. Follow the existing decomposition
rule (SKILL.md → "Decomposition and parallelism"): `planner`/`architect` produces the
design doc, an independent reviewer checks it, the **user approves it explicitly** before
it is split into build units. This is the same contract-lock step already required for
parallel builds — research just feeds it, it doesn't replace it.

## C. Release & Operations Playbook

### C1. Full runbook, not fragments, for anything the user executes

IF a release/operational step must be performed by the user (deploy trigger, DNS change,
migration run, secret rotation) → THEN the deliverable is a **complete runbook**, dispatched
as its own S/M creator task and gated normally, containing ALL of:

```
Pre-check: <exact command(s) to confirm starting state / preconditions>
Execute: <exact command(s) or UI steps, in order, copy-pasteable>
Verify success: <exact command/URL + expected output, e.g. curl returns 200,
  specific log line appears>
Rollback: <exact command(s) to revert if verification fails>
Known pitfalls: <environment-specific gotchas confirmed from this project's history,
  e.g. invisible whitespace in an env var name, a required container recreation step>
```

A verbal or partial handoff ("just run the deploy command") is not acceptable — the user
should not have to reconstruct missing steps or guess at rollback under pressure.

### C2. Cutover discipline

- **Flag-gated, single-switch cutover**: introduce the new path behind a flag, flip it
  once confirmed ready, rather than a gradual/manual per-user migration that leaves the
  system in a mixed state longer than necessary.
- **Legacy retirement timing**: this reuses `operations.md` → "Defer legacy/old-path
  removal until cutover is complete and stable" verbatim — do not remove the old path in
  the same wave as the cutover; retire it as its own later unit after stability is
  confirmed.
- **Data migration — success-only cleanup**: when a migration step can partially fail,
  only the successfully-migrated records get cleaned up/marked complete; failed records
  stay in their original state for retry, never partially cleaned up on a failed attempt.

### C3. Post-deploy verification request → loops back to § A on failure

After deploy, request the user's real-environment confirmation with the same precision
as § A3's minimal-ask template:

```
Please check: <exact URL/screen>
Expected result: <exact expected state/output>
If it does not match, report exactly what you see (screenshot if visual, exact error
  text if not) — do not summarize as "it's broken."
```

IF the user reports a failure here → THEN treat it as a new bug report and re-enter
§ A (Reproduce-Before-Fix) immediately — do not attempt a live theory-based patch on a
production system.

## D. Live Incident First Response Playbook

This section governs while production impact is **currently ongoing**. It is a
prerequisite phase, not a replacement for § A — once the bleeding stops, hand off to § A
for root cause. Confusing the two is the failure mode this section exists to prevent:
running a careful multi-step investigation while users are actively affected is the
wrong trade at that moment, even though it's the right trade once things are stable.

### D1. Stabilize before you diagnose

IF service impact is currently ongoing → THEN, before opening any § A-style
investigation, list the available stop-the-bleeding options and put them in front of the
user immediately:

- **Flip the relevant feature flag off** (if the affected path is flag-gated).
- **Roll back to the last known-good revision.** In this project's setup this is
  typically a **manual redeploy the user performs**, not something the commander can
  trigger directly — treat it as a user action to request, not a step to execute.
- **Disable the affected feature/endpoint** entirely if neither of the above applies
  cleanly.
- **Immediately rotate any secret suspected of being leaked**, if the incident may
  involve a compromised/exposed credential (e.g. anomalous access consistent with key
  misuse) — do not wait for confirmation first; this is the same rotate-first rule as
  § S1, just triggered earlier because the impact is live. Run it as one more option in
  this same request, not a separate later step.

Because the action is usually the user's to take, request it as a **single, minimal,
copy-pasteable action** — the same discipline as § A3's minimal-ask template, not a menu
of vague options:

```
Production impact is ongoing: <one-line description of what's broken>.
Fastest stop-the-bleeding option: <the one option you recommend, with a one-line reason>
Please do exactly this now: <the single exact action — flip flag X off / redeploy
  commit <sha> / disable route Y>
Reply once done, or tell me if that action isn't available and I'll give the next option.
```

Do not start a root-cause investigation agent before this request has gone out. If the
user is unavailable or the action fails, escalate to the next option on the list rather
than defaulting to a live theory-based patch on the production system.

### D2. Immediate impact assessment (run in parallel with D1, never instead of it)

Capture this minimal template — from logs/monitoring/user report already at hand, not a
fresh investigation — so the stabilize decision and the eventual § A handoff both have a
factual starting point:

```
Since when: <first observed timestamp, or "unknown — establishing">
Who/what is affected: <all users / specific segment / specific feature path>
What is observed: <exact symptom — error text, wrong data shown, request failing>
Data corruption suspected: <yes/no — see below if yes>
```

**IF data corruption/loss is suspected** (not just requests failing, but writes
persisting wrong/partial data) → THEN recommend disabling the write path for the
affected feature as part of the § D1 stop-the-bleeding request, even if that's a broader
action than the minimal fix — a corrupted write compounding while diagnosis is underway
is worse than a temporarily read-only feature. State this trade-off explicitly to the
user rather than deciding it silently.

### D3. Hand off — § A, or § S first/in-parallel if security-related

Once the stop-the-bleeding action confirms impact has stopped (re-use § C3's
post-deploy-style confirmation request to verify), the incident moves into normal
root-cause mode:

- **Default**: open § A (Reproduce-Before-Fix) for the underlying investigation.
- **IF the suspected or confirmed root cause involves a security event** (a leaked
  credential being used, unauthorized access, exploitation of a known vulnerability) →
  THEN do § S's rotation, exposure-window assessment, and lateral check **before, or in
  parallel with, the § A investigation** — do not let root-cause diagnosis delay
  rotation/lateral-check any further than § D1 already has. § A's evidence-gathering can
  proceed alongside § S's containment work; they are not mutually exclusive, but
  rotation/exposure/lateral-check do not wait on § A's diagnosis to complete first.

Either way, the eventual permanent fix goes through the normal S/M/L gate per
`gate-protocol.md` § 1 — being an incident does not justify skipping the independent
review mandate for an M/L fix. The only thing § D changed was sequencing: stabilize now,
investigate and fix properly afterward, not "investigate less carefully because it was
urgent."

### D4. Lightweight postmortem (proportional to size)

After the permanent fix ships, dispatch an S-task documentation update (reuse the
Per-Unit Handoff Documentation Brief in `briefing-templates.md`) containing:

```
Timeline: <first impact -> detected -> stop-the-bleeding action -> stabilized ->
  root cause confirmed -> permanent fix shipped, with timestamps where known>
Root cause: <from the § A investigation>
Stop-the-bleeding action taken: <what, and who performed it>
Prevention/detection: <what now catches this class of incident earlier — a test,
  an alert, a guard — or "none yet, tracked as follow-up">
```

Apply the same proportionality rule as § A7: a brief, low-blast-radius incident gets a
few lines in HANDOFF; reserve a fuller writeup for incidents with real user impact or a
root cause likely to recur. Do not skip this step entirely even for small incidents —
the timeline line alone is what lets a future session recognize a repeat.

## S. Security Incident Playbook

### S1. Secret exposure — rotate first, investigate second

IF a secret (API key, credential, token) is confirmed or suspected leaked → THEN
rotation is the immediate action, ahead of investigating how it leaked. Request it from
the user as a single minimal action, same shape as § D1's template:

```
Suspected/confirmed exposed: <which secret, where it was found exposed>
Please rotate it now: <exact steps — which console/dashboard, which key to revoke and
  reissue>
Reply once done so I can move to the exposure assessment below.
```

- **A deleted commit does not remove a secret from git history.** If the secret ever
  reached a commit that was pushed (even briefly, even in a since-force-pushed or
  since-reverted commit), treat it as permanently exposed and rotation is mandatory —
  do not treat history rewrite (`git filter-repo`, force-push) as a substitute for
  rotation; it's a separate, optional cleanup step, not the fix.
- After rotation, assess: **exposure window** (when it became reachable, when rotated),
  **access logs** if the provider exposes them (was it actually used by anyone besides
  the project), and **lateral check** — search the codebase and secret manager for other
  secrets of the same class/provider that may share the same exposure path (e.g. the
  same misconfigured env-var scope, the same committed `.env`).
- Only after rotation and the lateral check is complete does the leak *path* itself
  (how it got exposed) become a normal fix routed through the S/M gate per
  `gate-protocol.md` § 1 (e.g. add the file to `.gitignore`, fix a logging statement that
  printed a secret, correct an env-var scope).

### S2. Vulnerability report / CVE

1. **Confirm against the primary source first** — this reuses `operations.md` →
   Primary-Source Verification: read the actual advisory/CVE entry, not a paraphrase of
   it, before deciding anything.
2. **Evaluate applicability to this project specifically**: does the project actually use
   the affected code path/version/configuration named in the advisory? A CVE against a
   dependency the project has installed but never invokes in the vulnerable way may not
   need the same urgency as one on a directly-exercised path — state this assessment
   explicitly rather than treating "package X has a CVE" as automatically actionable.
3. **Patching is a dependency update** — route it through § E1 below (changelog check,
   full gate re-run, M-gate if it's a major bump) rather than a rushed unreviewed patch,
   unless § D1's live-incident conditions also apply (active exploitation with ongoing
   impact), in which case § D1 governs the immediate mitigation first.

## E. Routine Maintenance Playbook

### E1. Dependency updates

- **Read the changelog/breaking-changes notes at the primary source before touching the
  lockfile** — this reuses `operations.md` → Primary-Source Verification. Do not update
  blind on the assumption that a semver-compliant bump is safe; deprecations and behavior
  changes routinely ship inside minor/patch releases in practice.
- **A lockfile-only update still requires the full gate**: run the complete test suite
  and the build, not just "install succeeded" — a transitive version bump can change
  runtime behavior with no diff in the project's own source.
- **Major version bumps are an M-size unit** (creator → independent reviewer →
  acceptance) per `gate-protocol.md` § 1, never an S-task nod-through, even when the
  diff is small — the risk is in the dependency's behavior change, not the size of this
  project's edit.
- **"Tests pass" is not the full bar** — also check the build/test output for new
  deprecation warnings introduced by the bump and note them in the HANDOFF entry (per
  the Per-Unit Handoff Documentation Brief) even if nothing is broken yet; an
  unaddressed deprecation warning today is next release's breaking change.

### E2. Performance work

**Measure before optimizing — no exceptions.** IF a performance concern is raised (page
is slow, a query is heavy, a build is sluggish) → THEN the first deliverable is a
baseline measurement, not a change:

```
Baseline: <the actual measured number — load time, query duration, bundle size —
  with the tool/method used to measure it, e.g. "Lighthouse on <URL>, cold cache,
  3 runs averaged">
Environment: <where measured — local/staging/prod, device/network conditions if
  relevant — must match the re-measurement environment>
Hypothesis: <what is suspected to be the bottleneck, and why, from profiling
  evidence — not a guess>
```

- Do not dispatch a creator to "optimize X" without this baseline attached to the brief
  — an unmeasured optimization risks solving the wrong bottleneck and cannot be verified
  as an improvement.
- After the change, **re-measure under the same method and environment as the baseline**
  and report both numbers side by side. A report that says "should be faster now" without
  a re-measured number does not pass acceptance.
- Route the change itself through the normal S/M gate based on its actual size (a config
  tweak is S; an algorithmic/architectural change is M).

### E3. Flaky tests

IF a test fails intermittently → THEN do not immediately quarantine it; establish
whether it's real flakiness first:

1. **Re-run the single test in isolation, multiple times** (a handful of consecutive
   runs) to measure the actual failure rate before deciding anything. "It failed once" is
   not evidence of flakiness — it might be a real, intermittent bug.
2. **If a root cause is identifiable and fixable cheaply, fix it through the normal
   gate** rather than quarantining around it. Known root-cause classes from this
   project's history: races that need an explicit `waitFor`-style wait instead of a fixed
   sleep, and viewport/`innerWidth` state leaking between tests that need an explicit
   reset in setup/teardown. Check for these patterns first before assuming a novel cause.
3. **If no cheap root cause is found**, quarantine it (skip/mark-flaky per the test
   framework's mechanism) AND open a tracked follow-up (ticket or TODO item) AND record
   both the quarantine and the ticket in HANDOFF — a silently-skipped test is a coverage
   gap nobody will notice until it matters.
4. **Do not claim "it's just flaky" as an accepted conclusion without evidence**: require
   at minimum a consecutive multi-run pass (e.g. 3 consecutive green runs in isolation)
   attached to whatever conclusion is recorded, whether that conclusion is "fixed" or
   "quarantined as environmental."

### E4. Environment/secret ledger

Every unit that adds or changes an environment variable is not "done" at green tests —
it is gated on updating the project's environment/secret ledger (HANDOFF or equivalent)
with a row for each variable:

```
| Variable name | Scope (Preview/Production/local) | Where it's set | Set by |
|---|---|---|---|
| <NAME> | <scope> | <e.g. hosting provider dashboard, CI secret store, .env.local> | <user action
  required, or set by this unit> |
```

- Treat this as a commander acceptance-checklist item (alongside `gate-protocol.md` § 6):
  do not accept a unit that introduces an env var without this row filled in.
- **Known pitfalls to check for explicitly**: invisible/trailing whitespace in a variable
  name entered through a dashboard UI (silently creates a variable that never matches),
  and the build-time vs. runtime distinction (a variable baked in at build time will not
  reflect a later runtime-only change without a rebuild) — name which of the two applies
  for each variable added.
