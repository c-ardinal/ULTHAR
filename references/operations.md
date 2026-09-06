# Operations — background agents, technical disputes, git/shipping, legacy retirement

Read this file before waiting on background agents, resolving a disputed technical
claim, diagnosing a production/integration bug, or deciding whether to delete legacy
code. These are If-Then rules — apply them mechanically, don't re-derive them from
first principles under time pressure.

## Primary-Source Verification (technical disputes)

IF a decision hinges on a platform constraint that is disputed, unclear from memory, or
consequential (API limits, transaction/atomicity guarantees, undocumented behavior,
version-specific semantics) THEN the commander fetches the primary source directly
(WebFetch to the official docs) before finalizing the decision — do not decide from
training-data recall, and do not let a subagent's unverified claim about a platform
limit stand as fact.

- This applies especially when the claim is the deciding factor in a review verdict
  (e.g. "is this batch write atomic" determines whether an implementation is correct).
- Record the source (URL) and the specific fact confirmed in the contract doc or review
  notes, so the next reviewer doesn't have to re-derive it.
- Example classes of claims that need this: rate limits, batch-size limits, transaction
  isolation guarantees, "is X documented to do Y" questions, storage/quota limits.

## Reproduce-Before-Fix (production/integration bugs)

IF a bug report concerns a production or integration-level system (not a unit-testable
pure function) THEN do not dispatch a creator to guess-and-patch. Require the creator to
reproduce the failure first — with the real client (browser via Playwright/E2E tooling,
real API call, actual container) — and capture evidence (screenshot, log, response body)
before proposing a fix. A fix based on plausible theory without reproduction risks
patching the wrong layer and shipping a false "fixed."

- Brief: "Reproduce the failure with <tool> and capture evidence before touching code.
  Attach the evidence to your report. Only then implement the fix, and re-verify the
  same repro scenario passes after the fix."

## Background Agent Operation

- **Codex CLI review dispatches (gate-protocol.md § 7a) follow these same rules.** A
  Codex reviewer re-running a full gate command list can run long; if the forwarder
  subagent runs it in the background, treat it exactly like any other background agent
  below — the availability/fallback If-Then lives in § 7a, not here, to avoid duplicating
  it in two files.
- **Completion notification is the primary signal.** Long TDD/build tasks legitimately
  take many minutes with no output — silence is not evidence of failure.
- **Set a fallback wake-up 20–40 minutes out**, not shorter. If the `ScheduleWakeup` tool
  (or equivalent scheduled-wake capability) is available, use it: `delaySeconds` in the
  1200–2400 range, and put in the `prompt` field itself: "Check for concrete evidence of
  failure only. If none, do nothing — do not re-dispatch, do not nudge, do not spawn a
  duplicate." This makes the constraint survive even if the commander's own context has
  rotated out the reasoning behind the wake-up by the time it fires.
  If no such scheduling tool is available in the current environment, do not simulate one
  with a manual sleep/poll loop — rely on the completion notification as the sole signal
  and do not poll on short intervals.
- When the fallback fires: check for actual, concrete evidence of failure (error in a
  log, agent explicitly reported stuck, git state unchanged for an implausibly long time)
  before doing anything. If there is no such evidence, do nothing and keep waiting — do
  not re-dispatch, do not nudge, do not spawn a duplicate.
- **Never blind-re-dispatch a silent agent.** Two agents editing the same files
  concurrently corrupts the work. Re-dispatching is justified ONLY by explicit failure
  evidence, never by elapsed time alone.

### Liveness checks and mandatory stall watchers (2026-08-10, per user instruction — repeated cross-session failure, now mechanical)

Background: across multiple sessions the commander judged a silent agent "still
working" from a file mtime alone, without comparing it to the current clock — in the
worst case a stalled agent sat unnoticed for ~12 hours. These rules replace judgment
with mechanism:

- **Never judge liveness from an mtime alone.** A liveness check MUST print the
  artifact mtime AND the current time in the same command output and compare them
  explicitly (e.g. `ls -la <files> | awk '{print $6,$7,$8}'; date +%H:%M`). Stating
  "recently updated" without a same-output now-timestamp is a process violation, not a
  shortcut.
- **Every background creator/reviewer dispatch expected to run >20 minutes gets a
  commander-side stall watcher started IN THE SAME TURN as the dispatch** — a bounded
  background loop that periodically (~10–15 min) compares the newest mtime across the
  agent's file scope (or its exit/output marker) against the current time, and prints a
  STALL line when the gap exceeds ~30–45 min. This generalizes the "Codex
  background+watcher pattern" below to ALL long-running units; completion notifications
  remain the primary signal, the watcher exists solely to bound silent-death detection
  latency. Stop the watcher (TaskStop) once the unit's completion notification arrives,
  so a finished unit's naturally-stale files don't fire a false STALL.
- When a watcher reports STALL: that IS concrete failure evidence — proceed with the
  existing death protocol (assess on-disk state read-only first, then resume the SAME
  agent with a state summary). Elapsed time alone without the stale-mtime comparison
  still justifies nothing.
- **A subagent must never end its turn while its own background command is still
  running.** "You will be notified when it completes" holds only while the agent loop is
  active: once a subagent returns its final message, the harness does NOT re-invoke it
  when its background Bash command finishes — the result sits on disk unnoticed until the
  commander manually resumes the agent (observed with backgrounded Codex
  reviews). Brief any subagent that backgrounds a long command to block on completion
  within the same turn (Monitor tool until-condition, or repeated blocking TaskOutput
  calls) and only then report. Commander-side symptom to watch for: a forwarder/creator
  whose "final" report says it launched something and is waiting for completion — this
  means its turn has already ended, not that it is still monitoring. Do not resume-loop
  it hoping for a follow-up message that a stopped turn cannot produce; see "Codex
  background+watcher pattern" immediately below for the mechanism that avoids depending on
  that agent's turn continuing at all.

### Codex background+watcher pattern (2026-07-19, per user instruction)

Observed **~6 separate times** within a single multi-day session: a haiku forwarder
(`gate-protocol.md` § 7a) launched Codex with `run_in_background: true` and then ended its
own turn anyway, despite an explicit stay-alive instruction in the brief (see
`briefing-templates.md` → Codex Reviewer Brief, Step 3) — the stay-alive instruction alone
was not reliable enough to prevent this. The working pattern that stopped the stalls:

- The forwarder launches `codex exec` with `run_in_background: true`, writing stdout,
  stderr, and a completion marker to three separate scratchpad files ($OUT_FILE,
  $ERR_FILE, $EXIT_FILE — the exit code written to $EXIT_FILE only *after* the process has
  actually completed, so its mere existence is a reliable completion signal).
- **The commander does not rely on the forwarder alone.** In the same turn it dispatches
  the forwarder, the commander also starts its own background watcher — a bounded
  sleep-loop Bash command (e.g. `until [ -f "$EXIT_FILE" ]; do sleep <N>; done`, with a
  sane outer bound) — polling for that completion marker independently.
- When the watcher fires, the commander reads $OUT_FILE directly itself rather than
  waiting on the forwarder's own report. Treat the forwarder's "review complete" message,
  if and when it arrives, as **redundant confirmation, not the critical path** — the
  commander already has the result straight from disk.
- IF a forwarder's final message says something like "launched it, will report back when
  done" THEN do not resume-loop it hoping for a follow-up — its turn already ended by the
  time that message was sent. Rely on the commander's own watcher instead; only resume the
  forwarder if the watcher itself reports the exit marker never appeared within its bound
  (genuine failure), never merely because time has passed.
- **Foreground Codex calls are capped by the Bash tool's ~10-minute timeout.** Any review
  likely to run longer than that — full gate re-execution, a large document — must use
  this background+watcher pattern from the start; do not attempt it as a foreground call
  and discover the timeout mid-review.

### If an agent dies mid-task (session/usage limit, disconnect)

Agents died mid-task at usage-window limits **4 times** within one multi-day session —
size for this in advance rather than treating it as a rare edge case:

0. **Before an expected long absence** (a session/usage-window limit approaching, a
   scheduled break), prefer embedding the pipeline state directly in the wakeup/cron
   prompt (e.g. via `ScheduleWakeup` or the `schedule` mechanism) rather than relying on
   conversational context to survive — an embedded prompt works even after context
   rotation or a full session loss, whereas context the commander merely "remembers" does
   not (2026-07-19, per user instruction).
1. **Assess on-disk state first, read-only**: `git status`, `git diff --stat` (and, if
   needed, run the build/tests) to see exactly what's half-written. Do not guess. Do this
   FIRST, before deciding whether or how to resume anything — artifacts frequently survive
   intact, and in about half of this session's mid-task deaths, only the final report was
   lost while the actual work on disk was complete or nearly so.
2. **Resume the SAME agent** via SendMessage, summarizing the on-disk state you just
   found and the remaining work — do not spawn a fresh agent. A fresh agent has no
   memory of the original brief's nuance and may re-do or conflict with partial work.
3. Only fall back to a fresh agent if the original is confirmed unrecoverable (e.g. the
   session is permanently gone and SendMessage fails to resume it).
4. **Check whether sub-agents the dead agent had dispatched already completed their
   work** before re-dispatching anything — a dead parent does not mean its children died
   too. Re-doing already-finished sub-agent work wastes exactly the tokens this whole
   assessment step exists to save.

### Checking SendMessage responses

After sending to a background agent, check the response text for one of these two
concrete signals — do not infer from vibes:

- Response indicates something like **"resumed it in the background"** (or otherwise
  states the agent was stopped/idle and has now been resumed by your message) → the
  agent had actually stopped; it is now acting on what you sent.
- Response indicates something like **"Message queued for delivery at its next tool
  round"** → the agent is **still executing** prior work; your message is waiting, not
  yet acted on. Treat this as "still running": do not send a follow-up message, do not
  dispatch a duplicate/fresh agent, and do not treat the lack of an immediate reply as a
  problem. Wait for the actual completion notification.

## Git and Shipping Discipline

- **Never commit directly to the protected branch (main).** All work happens on a
  feature branch; main is updated by **pull request only**. Confirm the current branch
  before any commit; if you find yourself on main with changes, create/switch to a work
  branch (this carries the uncommitted tree over) before committing.
- **Commits are an S task, routed to haiku, with an explicit file list** — see
  briefing-templates.md → Commit Brief. Never let a commit agent use `git add -A` or
  `git add .`; always name files. If `git status` shows an unexpected file, the commit
  agent stops and reports rather than guessing whether to include it (protects against
  scooping up a concurrent agent's in-progress work).
- **PRs are opened ONLY on the owner's explicit instruction** (moved from project
  CLAUDE.md 2026-09-01): commits/pushes at good granularity are fine, but whether and
  when to `gh pr create` is the owner's call — never the PM's. When multiple branches
  ship together, ONE integrated PR (a `release/<date>`-style integration branch), never
  one PR per branch — each PR burns a production build (violated twice, each time
  opening three PRs where one integration PR would have done).
- **Before pushing additional commits to a branch that has an open PR, check the PR state
  first** (`gh pr view <n> --json state,mergedAt`) (2026-08-26, per user instruction). A
  MERGED or CLOSED PR does not pick up later pushes; in that case cut a fresh integration
  branch from the updated `main` and open a new PR. Silent push-after-merge lost a whole
  batch of changes once.
- **Push after each commit**, not batched at the end — keeps remote state close to local
  and limits blast radius if something goes wrong later in the session.
- **Commit messages state Why, not What** — the diff already shows what changed; the
  message is for the reader who wants to know why the change was necessary.
- Conventional commit messages (`<type>: <description>`); verify `.gitignore` excludes
  build/test artifacts.
- **Every accepted unit gets a handoff/progress doc commit** alongside the code commit
  (see briefing-templates.md → Per-Unit Handoff Documentation Brief). This is part of
  "done," not optional follow-up.
- "Deploy-ready" means: build is green, output artifacts + host config exist, tests and
  E2E pass, branch pushed, PR opened. Actual deploys needing the user's credentials are
  the user's step — prepare everything and hand over exact commands.
- Verify end-to-end the way it will actually run (e.g. serve the production build and run
  E2E against it, check SPA deep-link fallback) rather than only a dev server.
- **Verification servers are the PM's alone** (2026-08-29, `roles-pm-l.md`): only the
  PM starts, stops or restarts them; L, creators, reviewers and auditors use the
  existing port and report "server down" instead of acting. Subagent briefs state this
  (start/kill of npm run dev:heavy / e2e:server / wrangler dev, and taskkill of
  node/wrangler/workerd, are all forbidden to them). At every subagent acceptance the
  PM checks for leftover processes (workerd/wrangler) and port state. When a subagent reports
  it down, the PM checks the process chain (`Get-Process node,workerd` + `netstat`),
  stops it with `python "$HOME/.claude/hooks/stop_dev_orphans.py" --all --root <worktree>`,
  restarts, confirms 200,
  then resumes the waiting agent. Servers the owner runs themselves (`!` commands) are
  restarted by the owner — the PM asks with the exact command. **Never run
  `stop_dev_orphans.py --all` while an owner-started server may be alive** — orphan chains
  are killed regardless of `--root` (2026-08-30: it took
  the owner's own dev server on one port down together with the PM's on another). Stop only the PIDs of
  the chain the PM started (its own background task's process tree), then verify the
  owner's ports still listen before reporting.
- **Every tool started for a task is stopped when that task is done — the whole process
  chain, not just the parent** (2026-08-26, per user instruction; repeat of 2026-08-21).
  Dev/e2e servers (`npm run dev:heavy` / `e2e:server` → concurrently → vite/wrangler →
  workerd), `tail -f` watchers, export CLIs (draw.io), ad-hoc `http.server`s. `TaskStop`
  kills only the top `npm` process; the children survive, hold git handles (bash/tail),
  squat 8788/5173 and burn CPU across sessions. Procedure after the last verification
  that needed the tool: run `python "$HOME/.claude/hooks/stop_dev_orphans.py" --all --root <worktree>`,
  then confirm `Get-Process bash,tail,workerd` is empty and `netstat` shows no LISTEN on
  the dev ports, and only then report completion. The Stop hook's
  `dev_process_hygiene` guard blocks the end of a turn while orphaned (parent-dead)
  tool processes exist; it does not flag chains whose parent is alive, so a server that
  is intentionally serving a background audit agent is not a false positive.
- **Defer legacy/old-path removal until cutover is complete and stable.** When a build
  introduces a new path alongside an old one (e.g. behind a flag), do not delete the old
  path in the same wave. Removing the fallback before the new path has proven itself in
  practice destroys the safety net the flag existed for. Retire legacy code only as its
  own, later, explicitly-scoped unit, after the cutover has been confirmed stable.

## Design Fidelity: use the real source, not a transcription

If the work must match an existing design (Figma, ClaudeDesign `.dc.html`, a mockup),
**the design file itself is the ground truth.** Text specs *describing* the design are
useful for enumerating elements and behavior, but building only from prose reliably
produces something that looks different — wrong layout, spacing, color application,
typography. This is a real failure mode; budget for it.

- Get the actual design file in front of the subagents (have the user place it locally
  if a remote design tool isn't reliably reachable — some MCP/tools are only reachable
  from certain contexts, e.g. a design MCP that works for the commander/foreground but
  not background subagents; provision the source file locally for them rather than
  assuming they can fetch it). Extract the real CSS and per-screen markup as reference,
  and have implementers port it, not reinvent it.
- Keep a confirmed token table (colors, type, spacing, semantic colors) in the canonical
  contract doc and verify the implementation actually *applies* the confirmed values — a
  common gap is approximate placeholder values shipped instead of the real ones.
- When the user reports "it doesn't look like the design," don't patch piecemeal:
  extract the source, diff against current implementation, and re-skin from the source.
- Screenshots taken via Playwright MCP go under the project's `.playwright-mcp/`
  directory — never scattered at the project root.

### Measurement script contract (2026-08-25, per user instruction)

Appearance fidelity is judged by eye against the design source; **geometry is not** —
it is judged by numbers (`gate-protocol.md` § 6a). Each project that does UI work keeps
one measurement script so creators and reviewers produce comparable output instead of
each inventing an ad-hoc probe:

- **Location**: this distribution ships no such script — you provide your own. Keep it at
  a stable path (typically `scripts/measure-rects.mjs` in the project, or one shared
  implementation outside it if you run several projects) and run it from the project
  root:
  `node <path-to-script> --url <url> --selectors "<a,b>" --pairs "<a|b>"`.
  Have it resolve Playwright from the PROJECT's `node_modules` (so the project needs
  `@playwright/test`, not a vendored copy of the script); name whichever script is in
  use in the brief. What matters is that the tool is stable across rounds — a one-off
  probe that dies with the session cannot support a before/after comparison.
- **Input**: a URL, a viewport size, and a list of CSS selectors.
- **Output**: JSON on stdout — for each selector, `getBoundingClientRect()` (x, y, width,
  height, top, right, bottom, left) plus the computed styles that commonly cause
  alignment defects: `display`, `alignItems`, `justifyContent`, `textAlign`,
  `verticalAlign`, `position`, `zIndex`, `margin`, `padding`, `lineHeight`,
  `transform`. The computed styles are what turn a measurement into a diagnosis.
- **Derived checks** it should report directly, because these are the defects that
  recur: centre-offset between two elements, vertical/horizontal edge deltas, and
  bounding-box overlap between named pairs.
- **Determinism**: fixed viewport, fonts loaded, animations disabled. A measurement that
  changes between runs cannot settle an argument about 6px.
- Its output goes into the unit's report and the reviewer's verdict; the raw JSON is
  scaffolding and is cleaned up at acceptance like any other artifact-directory output.

## Token-efficiency discipline (2026-07-16, per user instruction)

Background: measured on a UI-refresh session — doc loops of 10-11 rounds, authors
resumed at 500k+ token transcripts, and verbatim finding relays were the top three
token sinks. These rules target ~40-50% reduction at equal quality. (See
gate-protocol.md § 9 for the companion rules on transcript retirement, lint-before-Codex,
and delta-scoped re-reviews.)

- **Findings by reference**: a send-back passes the reviewer's output **file path** plus
  one-line commander decisions per finding — never re-paste the full findings text into
  the send-back brief.
- **Batch small doc amendments**: queue single-line/paragraph contract amendments and
  send them as **one** batched instruction, not one message each — each resume of a
  background agent costs a full transcript re-read.
- **5h-window sizing**: size creator units to complete within one usage window (~40-file
  units should be split in two). After a session-limit termination, the resume brief
  still must contain the on-disk state assessment (see "If an agent dies mid-task"
  above) — but note that an oversized unit is the root cause to avoid in the first place.
- **Fold spec-amendment verification into the next implementation review** when an
  implementation of that spec is about to be reviewed anyway (one Codex round checks
  both), instead of dispatching a standalone doc-verification round.

## Mechanical enforcement: which rules are hooks (2026-08-25)

Some of this skill's rules are now enforced by `PreToolUse`/`Stop` hooks rather than by
the commander remembering them. **Do not restate an enforced rule in a brief** — it costs
tokens in every dispatch and buys nothing.

| Rule | Enforced by | Applies to |
|---|---|---|
| No shell file creation (`cat >`, `echo >`, `tee`, `Set-Content`/`Out-File`); no `cd X && cmd` | `tool_discipline` guard (PreToolUse Bash/PowerShell) | every agent |
| No blanket staging (`git add -A` / `.`, `git commit -a`); no commit/push/merge/rebase on `main`/`master` | `git_discipline` guard (PreToolUse Bash/PowerShell) | every agent |
| `git push` to a branch whose PR is already MERGED/CLOSED (no OPEN PR) | `pr_state` guard (PreToolUse Bash/PowerShell, asks `gh pr list --head`) | every agent |
| Gitignored artifact directories left behind at end of turn | `artifact_hygiene` guard (Stop, once per session) | the commander |
| Orphaned dev-tool process chains (dev/e2e server, wrangler/vite/workerd, `tail -f`, export CLIs) left behind at end of turn | `dev_process_hygiene` guard (Stop, every turn) + `stop_dev_orphans.py` (cleanup helper) | the commander |
| Commander does not author deliverables | `commander` guard (PreToolUse Write/Edit) | main chain only |
| This skill's core rules re-handed at dispatch time, session start, and after compaction | `skill_anchor` guard (PreToolUse Agent/SendMessage, PreToolUse Write/Edit/MultiEdit/NotebookEdit, SessionStart) | the commander |

**Both guards look up a marker, they do not ship a default value**: each reads
`<repo>/.claude/state/<marker>` first, then falls back to `~/.claude/state/<marker>` — but
if neither file exists, the guard is **inert** (no anchor, no block, no log), not
"on" with some built-in setting. A first line of `off` in the repo marker disables that
guard for that repo. Create both markers globally, once (`active-skills` = this skill,
`commander-mode` = `enforce`), so a fresh session is anchored and guarded before this
skill is ever invoked — see SKILL.md → "Arm the session first" for the full setup (a
session that never invoked the skill, and had no marker in place, used to be exactly
the unguarded one; that motivation stands regardless of what any given machine's
marker files currently contain). Details of each:

- `<repo>/.claude/state/commander-mode` (first line `enforce`, `observe`, or `off`) sets the
  commander guard's mode. It distinguishes main from subagent using two independent signals — an
  `agent_id` on the payload, and a `subagents` segment in `transcript_path` — and blocks
  only when both say "main". Measured 2026-08-25: `agent_id` is empty on the main chain
  and carries the subagent's id on a dispatched agent, so it is the signal doing the
  work; the transcript check is a fail-open second opinion that never blocks on its own.
  Coordination stays open: the scratchpad, `.claude/`, `docs/plans/`, `plans/`, HANDOFF
  documents, and anything outside the repo are exempt. Every decision is appended to
  `~/.claude/hooks/logs/commander-guard.jsonl`, so a misclassification is auditable
  rather than mysterious.
- `<repo>/.claude/state/active-skills` (skill names, one per line; or `off`) sets the
  anchor's skill list. Each listed skill's `ANCHOR.md` is injected before every `Agent`
  dispatch and `SendMessage` resume, at every session start (including after compaction,
  via `source=compact` — there is no separate post-compaction hook), and (since
  2026-08-26) before every main-chain `Write`/`Edit` to a deliverable path — subagent
  writes and coordination paths (scratchpad, `.claude/`, plan docs) are skipped. **Keep every ANCHOR.md under ~250
  characters** — it is paid for on every dispatch, and a heavy orchestrated session runs
  hundreds. It is a signpost to `references/`, never a copy: a copy drifts from the file
  it duplicates, and § 9 exists to stop exactly this class of repeated-context cost.

**The bar for adding a hook.** Only purely syntactic or purely structural checks — a
pattern match on a command string, a path prefix, a directory listing. Anything requiring
judgment ("is this finding really LOW severity", "does this match the design") stays with
the commander; a hook that guesses at judgment produces false blocks, which teach everyone
to work around it. Changes to `settings.json` are made only when the user asks for them.
