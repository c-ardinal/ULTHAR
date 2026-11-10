# ULTHAR v2 R1-a — premise probes (AC-10)

Measured 2026-11-10 on Windows 11 (native, Git Bash as the Bash tool), Claude Code 2.1.322,
`claude -p --model haiku`, with `v2/probes/run_probes.py` (re-run:
`C:/Python314/python.exe -X utf8 v2/probes/run_probes.py [--logs DIR]`).
Each probe used a fresh throwaway project under `%TEMP%\ulthar-probes-*`, its own settings file
passed with `--settings`, and `--setting-sources project,local` (the owner's user settings and
hooks took no part). Nothing was written to `~/.claude/{settings.json,hooks,skills,agents}`.
Full stream-json logs of the final run are kept outside the repository (path in the PM report);
excerpts below, with temp paths shortened to `<tmp>`.

| ID | Premise | Result |
|---|---|---|
| P-06 | A subagent's `skills:` loads the full skill text at startup | **holds** |
| P-13 | An agent definition generated while a session is running is usable in that session | **does not hold** as stated; holds only from the next user turn, and only when the `agents` directory already existed at session start |
| P-14 | The Claude Code sandbox can restrict where Bash writes, on Windows | **does not hold** (native Windows, this version) |

## P-06 — holds

Setup: project skill `probe-skill/SKILL.md` of 32,030 bytes (500 filler lines) with one codeword on
its first body line and another on its last line; project agent `prober` with `skills: probe-skill`,
`tools: Read` (one tool is required for an agent to launch) and the instruction not to use any tool;
Grep/Glob/Bash/PowerShell/Skill/Write/Edit disallowed. The main session only called `Agent`.

Evidence:
- Reply returned by the subagent: `HEAD-dde991c0` / `TAIL-34b25181` (both codewords; random per run).
- Subagent tool uses: **0** (stream and the subagent's own transcript), main-chain tools: `["Agent"]`.
- The subagent transcript's second record is an `isMeta` user message of 33,278 characters that
  starts `<command-message>probe-skill</command-message><command-name>probe-skill</command-name>`
  and ends `...Last codeword: TAIL-…` (contains both codewords) — the whole skill body was injected
  before the first model turn, not read through a tool. Same result on an earlier identical run.

Consequence for the design: SKL-03 can rely on `skills:` (no switch to SubagentStart injection needed).

## P-13 — does not hold (as stated)

Setup: a session is started; after it is running, an external process (the harness, standing in for
the installer) writes `.claude/agents/late-agent.md`, whose only instruction is to answer with a
random codeword. Project-level agents were used because user-level `~/.claude/agents` is off limits
for this work; the documented watcher covers both directories alike.

| Variant | Agents dir at session start | Delegation in | Result |
|---|---|---|---|
| same turn | existed | the same turn, ≥ 30 s after the file was written | `Agent type 'late-agent' not found. Available agents: claude, existing, Explore, general-purpose, Plan, statusline-setup` |
| same turn | absent | the same turn, ≥ 30 s after | `Agent type 'late-agent' not found. Available agents: claude, Explore, general-purpose, Plan, statusline-setup` |
| next turn (`--input-format stream-json`, 2 turns) | existed | turn 2, 30 s after the file was written between turns | **codeword returned** (`task_notification … "status":"completed" … "summary":"LATE-cc11cf57"`); reproduced on 3 runs (one sync hand-back, two async notifications) |
| next turn | absent | turn 2, 30 s after | `Agent type 'late-agent' not found. Available agents: claude, Explore, general-purpose, Plan, statusline-setup` |

Verdict: an agent definition generated during a running session is **not** usable in the turn in
which it appears (the docs' "within a few seconds" did not apply within a turn, even after 30 s), and
is never usable when its `agents` directory did not exist at session start (matches the docs'
restart caveat). It **is** picked up at the next user turn when the directory already existed.
Consequence for the design: install must create the `agents` directory before sessions start, and
a newly generated agent can be used from the owner's next message on, not mid-turn; otherwise a
restart (relay) is needed.

## P-14 — does not hold

Setup: settings `{"sandbox": {"enabled": true, "allowUnsandboxedCommands": false,
"filesystem": {"denyWrite": ["<project>/denied"]}}, "permissions": {"allow": ["Bash"]}}`
(Bash fully allowed so that a permission prompt cannot stand in for the sandbox).

Evidence:
- Run A (sandbox enabled, strict): the model ran `echo probe > denied/probe.txt`; reply
  "No error text came back"; **`denied/probe.txt` exists afterwards** (`write_into_denied_dir_succeeded`
  = true). The write into the write-denied directory was not restricted.
- Run B (same, plus `"failIfUnavailable": true`): exit 1 at startup with
  `Error: sandbox required but unavailable: sandbox is enabled but the Windows sandbox is not active
  on this session (feature gate off)` / `startup_failure_reason: "sandbox_required_unavailable"`.
- Docs (code.claude.com/docs/en/sandboxing): "The sandbox runs on macOS, Linux, and WSL2. On native
  Windows, Claude Code runs commands unsandboxed."

Consequence for the design: per §8 P-14, TRUST-02 on Windows is hook blocking only (Bash/PowerShell
command analysis), stated as such; the sandbox becomes available only under WSL2.
