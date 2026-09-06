# ULTHAR hook runtime — design notes

This document summarizes the architecture and design invariants of the `hooks/`
runtime for people integrating or extending it. It is derived from an internal
implementation contract that also carried private review history, an exception
table, and findings; those live in an internal record that is not part of this
distribution. Where a `_note` in `config.json` still carries a short tag such as
`PE-n`, `F-n`, `N-n`, `M-n`, `I-n`, `U-n`, `S-n`, `C-n`, `B-n`, `H-n`, `D-n`,
`R-a/b/c`, or `Jn`, that tag is a retained internal-review identifier from that
history; it is not documented further here and can be treated as an opaque label
— except `PE-n`, which is documented publicly in `docs/PARITY-EXCEPTIONS.md` (see "Parity exceptions" below).
For the same reason, docstrings, comments and `_note` fields throughout this tree cite
internal records by name — chiefly `CONTRACT.md`, `CONTRACT-ARCHIVE.md` and `danger.md`
— and those documents are not published with this distribution. Read such a citation as
opaque provenance for the decision it annotates, not as a broken link to something you
were meant to be able to open.

## 0. Target layout

```
<skill-root>/
  config.json                  <- the single policy/config file (skill root)
  SKILL.md, ANCHOR.md
  docs/
    DESIGN.md                  <- this file
    PARITY-EXCEPTIONS.md       <- public design-decision record for every PE-<n> id
  references/                  <- human-readable operating discipline (see SKILL.md)
  hooks/
    dispatch.py                <- single entry point, one process per hook event
    install.py                 <- deploy / --verify / --uninstall / --register-settings
    lib/
      io.py  ctx.py  paths.py  procs.py  gitcmd.py
    guards/
      __init__.py
      tool_discipline.py  git_discipline.py  pr_state.py  dev_server.py
      deploy.py  commander.py  skill_anchor.py  brief_gate.py
      artifact_hygiene.py  dev_process_hygiene.py
    stop_dev_orphans.py         <- manual CLI, not a guard
    tests/
      run_all.py  parity.py  fixtures/*.json  test_<guard>.py
```

Deployed copy (derived, never edited directly): `config.json`, `dispatch.py`,
`install.py`, `stop_dev_orphans.py`, `lib/*.py`, `guards/*.py`, plus a `logs/`
directory, under whatever directory `install.py` deploys to (default
`~/.claude/hooks`, overridable with `--deploy-dir`). `tests/` is never deployed; it
persists inside the skill only. The actual deploy directory used at runtime is
always wherever `dispatch.py`/`install.py` are actually running from — there is no
separate config key naming it.

**Config resolution.** `dispatch.py` loads `config.json` from **next to itself**
(the deployed copy), not the master. This keeps `install.py --verify`'s hash
comparison (master vs. deploy) meaningful, and keeps the runtime working if the
skill directory is later moved. The observable install procedure is therefore:
*edit master `config.json` → run `install.py` → the deployed copy takes effect.*
`install.py` deploys `config.json` plus the `lib/` and `guards/` trees, not a flat
file list.

An env var `ULTHAR_CONFIG` (absolute path) overrides config resolution for tests only;
it must never be set in production.

## 1. Guard module interface

Every guard is an importable module under `hooks/guards/` exposing exactly one
public function:

```python
# hooks/guards/<guard_id>.py
def run(payload: dict, cfg: dict, ctx: Ctx) -> Result: ...
```

```python
# hooks/lib/io.py
class Result(NamedTuple):
    exit_code: int   # 0 = allow, 2 = block. No other value is ever returned.
    stdout: str = "" # passthrough text (SessionStart) or JSON envelope. "" = nothing.
    stderr: str = "" # block reason, or a non-blocking notice. "" = nothing.
```

Hard rules for guard modules:

- MUST NOT call `sys.exit()`, `print()`, write to `stdout`/`stderr`, or read `stdin`.
  The dispatcher owns all process I/O.
- MUST NOT read environment variables (`USERPROFILE`/`HOME`/`TEMP`/etc.) directly.
  Use `ctx`.
- MUST NOT call `subprocess` directly. Use `ctx.run_cmd` / `ctx.list_processes`.
- MUST NOT import another guard module. Shared logic goes in `lib/`.
- MUST NOT contain a policy literal (a path, a threshold, a message, a branch name,
  a regex describing *what* is forbidden). Those live in `config.json`.
- MUST be deterministic given `(payload, cfg, ctx)`. All non-determinism (clock,
  filesystem, subprocesses, process table) enters through `ctx`.
- MAY perform filesystem side effects, but only under `ctx.state_dir`,
  `ctx.logs_dir` or `ctx.temp_dir`, and only for the few guards that need it
  (`tool_discipline` consuming its delete-allowlist, `commander` appending its
  decision log, `artifact_hygiene` writing its once-per-session marker) — plus,
  since owner grants landed, **any guard that consults `lib/grant.py`**, which
  consumes a grant file under `ctx.state_dir` and appends one audit line under
  `ctx.logs_dir` on its behalf. That list is therefore no longer closed by guard
  name; it is closed by *destination* (those three directories) and by the fact
  that all three of those writes live in exactly two shared `lib/` functions, not
  in the guards themselves.

**Why this shape.** Calling guards in-process, one guard-module call at a time,
collapses what would otherwise be one new subprocess per guard per tool call into a
single process for the whole dispatch — and makes every guard a pure function that
a unit test can call directly with a plain `dict`, no process, no stdin, no
exit-code plumbing.

### `Ctx` — the injection seam

```python
# hooks/lib/ctx.py
class Ctx:
    home: Path            # user home
    state_dir: Path        # state directory
    deploy_dir: Path       # directory dispatch.py lives in
    skills_root: Path
    logs_dir: Path
    temp_dir: Path
    cwd: Path              # payload["cwd"] or process cwd
    is_windows: bool
    event: str
    def now(self) -> datetime: ...
    def run_cmd(self, argv, kind: Literal["local", "remote"], cwd=None) -> CmdResult: ...
        # `kind` is REQUIRED and selects the timeout budget: "local" (git plumbing)
        # vs. "remote" (network calls like `gh`) get different budgets, so a network
        # call can never silently inherit a too-tight local timeout.
    def list_processes(self) -> list[ProcInfo]: ...
    def log_error(self, guard: str, exc: BaseException, payload_keys: list[str]) -> None: ...
```

`Ctx` is constructed once per dispatch and passed to every guard. Tests construct a
`Ctx` pointing at a temp tree with a frozen `now()`, and stub `run_cmd` /
`list_processes` — which is what makes every clock-, git- or process-table-sensitive
check testable without touching the real machine.

### Main-chain test

A single, shared `is_main_chain(payload)` function is the only authority for "is
this the main/commander chain, or a subagent". It requires **both** signals to say
"main": an empty `agent_id` **and** a `transcript_path` that does not look like a
subagent transcript. A subagent whose `agent_id` happens to be empty but whose
transcript path indicates a subagent is still classified as a subagent.

## 2. Shared library modules — `hooks/lib/`

- **`lib/io.py`** — process I/O, UTF-8 encoding rules, the `Result` type. Every
  `open()` passes an explicit encoding; state/marker files are read as
  `utf-8-sig` (tolerating a BOM); writes use `utf-8` with no BOM; `stdout` is
  written as raw bytes with no trailing newline.
- **`lib/ctx.py`** — `Ctx`, `is_main_chain`, config load + cache (keyed by path,
  mtime, size), the sole owner of `re.compile()` for guard-declared patterns
  (guards may not call `re.compile` themselves), and `merge_overlays` — the merge
  algorithm for the two overlay layers (see "Overlays" below).
- **`lib/paths.py`** — platform-correct path handling, and `resolve_python_exe(cfg,
  ctx=None)`, the ONE resolution point for the effective interpreter: `paths.python_exe`
  non-empty means "pin this interpreter" (what `install.py --register-settings` writes
  into `settings.json`); empty (the shipped default) means "resolve from the
  interpreter currently running" (`sys.executable`), computed on demand at each call
  site, never cached at config load. Two distinct, never-confused
  normalisation strategies:
  - `norm_for_compare()` / `is_under()` — **equality** compares (delete-allowlist
    entries vs. command tokens); lexical only (`os.path.normpath` semantics, never
    `Path.resolve()`, since that would follow symlinks/junctions and can be
    evaluated on paths that no longer exist); case-folded only on Windows.
  - `matches_any()` — **regex** matching (exempt paths, guarded extensions/names);
    separator-normalised only, case sensitivity comes from the pattern's compiled
    flags, not from this function.
- **`lib/procs.py`** — dev-process discovery. A single implementation over
  `psutil` (no OS-specific branching, no fallback path — a silent fallback would
  mean two behaviours to test and a guard that can quietly change semantics).
  `psutil` is the runtime's only non-stdlib dependency. `dev_process_hygiene`
  (Stop) and the manual `stop_dev_orphans` CLI are the only *guard-shaped*
  consumers; the guard-purity AST scan still asserts no other `guards/*.py`
  file imports it. As of ulthar-enforcement-round1 (session-scoping), that is
  no longer the whole story: `dispatch.py` itself also imports `lib.procs`
  lazily — inside a function, never at its own module level — on `PreToolUse`
  for `Bash`/`PowerShell` tool calls AND on every `Stop` (the guard is about
  to READ the ledger there, so it is refreshed one last time first), to
  snapshot the session's process descendants into a per-session ledger at
  the places new ones actually appear (`update_session_ledger`). Every OTHER
  tool call on `PreToolUse`, and every other event, never touches it.
  `dev_process_hygiene` itself only ever READS that ledger (targeted per-pid
  `psutil.Process`/`psutil.pid_exists` probes) — it no longer calls
  `psutil.process_iter()` over the whole machine on Stop at all. The writer's
  primary path drives descendant discovery and widening from
  `psutil._psplatform.ppid_map()` (one bulk OS query, zero per-process
  `Process()` construction); when that private API is unavailable on a given
  psutil version/platform, a FALLBACK reconstructs the descendant subtree
  from `psutil.Process(root).children(recursive=True)` instead (each
  returned `Process` already knows its own `.ppid()`, so no extra
  construction is needed to rebuild a local parent map) — descendants only,
  deliberately with NO widening, since there is no cheap way to ask "which
  live pid's ppid is a ledger pid" without either the bulk map or a
  `process_iter()`-shaped full scan.
  RESIDUE, stated rather than hidden: a wrapper process that is BOTH spawned
  AND has already exited entirely between two hook dispatches — never
  overlapping a ledger snapshot — is never recorded, so a long-lived worker it
  left behind cannot be linked to the session either, even though the ledger
  widening rule (round 2, H-1: any live pid whose ppid is an already-ledgered
  pid) catches the same shape of gap once the wrapper WAS captured at least
  once. Closing the remaining class needs continuous monitoring between hook
  calls (polling or an OS process-creation subscription), which this
  hook-driven design does not attempt.
  round-5 correction: a widening PARENT must additionally be either dead
  (no live process holds that pid) or, if a live process does hold it,
  confirmed the SAME process by `create_time` — otherwise a wrapper's pid
  reused by an unrelated foreign process would widen that foreign process's
  own children in as if they were this session's. LOCKING: the read-modify-
  replace and the sweep's per-file delete decision both take lib/io.py's
  existing append-lock primitive, whose retry budget is bounded at roughly
  0.5s (Windows timer granularity rounds it nearer 0.4-0.5s in practice)
  before this update gives up and lets the next dispatch retry — never an
  unbounded wait on the hook path. Measured NON-CONTENDED PreToolUse Bash
  p95 784ms against the 1052ms acceptance budget.
  round-6 correction: an identity that cannot be read at all (`AccessDenied`)
  is recorded with `is_root_boundary: None` ("unknown") and is NEVER
  descended through — treated the same as a confirmed foreign boundary for
  traversal, since the owner's requirement is absolute isolation and an
  unconfirmed identity must never default to "assume ours" (this reverses
  the earlier round-4 choice to keep traversing past it). Symmetrically, a
  CACHED boundary/unknown/non-boundary verdict for any internal node (one
  with live children right now) is re-verified by create_time before being
  trusted at all — a stale confirmed boundary whose pid was later reused by
  an ordinary process would otherwise keep pruning that now-legitimate
  subtree forever, the mirror image of trusting a stale non-boundary too
  far. `AccessDenied` is also no longer collapsed into "dead" for sweep/TTL
  purposes: an unreadable root or entry is "unknown", preserved exactly like
  a confirmed-alive one, and never reported as an orphan or killed.
  RESIDUE (double degradation): when the `_ppid_map()` fallback AND an
  unreadable node occur in the same session, that node's subtree stays
  invisible for the rest of the session even after it dies, since the
  fallback has no widening mechanism to link it back in the way the main
  path's H-1 widening does — narrow (needs both conditions at once) and
  accepted rather than closed.
- **`lib/gitcmd.py`** — git command-line parsing: the global-option prefix
  (`-C|-c|--git-dir|--work-tree|--exec-path`), quote stripping, phase-ordered
  evaluation for git discipline (blanket-staging → destroyers → hook-bypass →
  write-subcommand gate → force-push → deploy-ref/protected-branch → current
  branch), and PR-state parsing.

### Regex dialect and case sensitivity

Every pattern is Python `re`. Patterns are classified by **kind**, declared once
per JSON path in `config.regex.pattern_fields` (the sole authority — never inferred
from a field's name or its apparent content):

- **`text`** kind (command text, brief text, tool names, process command lines,
  branch names) — always case-insensitive, on every platform.
- **`path`** kind (filesystem paths) — case-insensitive only on Windows; case
  sensitive on POSIX, so a delete-guard rule cannot be tricked by a differently
  cased disposable-directory name that would only coincide on a case-insensitive
  filesystem.

`re.ASCII` is deliberately never added (Unicode-aware matching is required for
non-ASCII text in guard patterns). Compilation is **per guard**, never global: an
invalid pattern in one guard's config section disables only that guard (fails
open, logged), never the whole runtime.

## 3. `config.json` schema (summary)

One JSON file at the skill root. Comments are `_note` / `_note_<key>` keys, ignored
by every consumer. Top-level sections: `paths`, `thresholds`, `dependencies`,
`logging`, `dispatch`, `command_rules`, `git_policy`, `brief_rules`,
`design_cases`, `commander`, `skill_anchor`, `deploy_guard`, `artifact_dirs`,
`dev_processes`, `viewport_matrix`, `regex`, `grants`, `overlays`.

- **`paths`** holds cross-cutting paths only (used by `lib/` or by more than one
  guard); a path used by exactly one guard lives in that guard's own section.
  `python_exe` ships **empty** (`""`), meaning "detect the running interpreter"
  (`lib/paths.resolve_python_exe` falls back to `sys.executable`, resolved fresh
  at each call site) — the correct default for a cloned copy, which has no reason
  to know any one machine's interpreter path. Set it to an absolute path only to
  pin a specific interpreter for `settings.json` registration and dependency
  checks; that pin belongs in a machine's own user overlay
  (`paths.python_exe` under `~/.claude/ulthar-overlay.json`), never edited into
  this shipped file directly. There is no `deploy_dir` key here: nothing ever
  read it (the actual deploy directory always comes from where
  `dispatch.py`/`install.py` are actually running), so it was removed outright
  rather than kept as an unused override.
- **`grants`** — owner grants: a **one-shot** exemption from **one named rule**,
  addressed exactly the way an overlay `disable` is
  (`<dotted config path>/<rule id>`). Same syntax, two lifetimes:

  |                          | scope             | lifetime                          |
  |--------------------------|-------------------|-----------------------------------|
  | project overlay `disable`| that repository   | standing                          |
  | owner grant              | this machine      | consumed on first use             |

  A grant **is a file**: an empty file at `<grants.dir>/<dotted path>/<rule id>`,
  e.g. `~/.claude/state/ulthar-grants/git_policy.force_push/push-force`. A guard
  that has decided to block consults `lib/grant.py:check()` with the identifier
  of the rule that decided; a matching grant is taken and the guard **continues
  its own rule loop** (never returns "allow" — the rules it had not reached yet
  must still run, or one granted rule would wave through everything the guard
  checks after it). The consult therefore lives in the guard, not in the
  dispatcher: the dispatcher only sees "this guard blocked", by which point the
  remaining rules are already skipped.

  `main_chain_only` is not per-grant policy: a grant never applies to a subagent,
  whatever it names. Consumption is the only lifetime offered — a rule that
  should stay off is a `disable` in the user overlay, which already exists and
  says exactly that; a "standing grant" would be the same thing wearing a name
  that suggests it expires. Every consumption is logged (`grants.log`) with the
  rule id and what it permitted; a grant that was never used is observable as the
  file still sitting there, and a grant that was deliberately ignored (a subagent
  hit the rule) is logged too, so "I wrote a grant and nothing happened" is always
  answerable. A grant naming a rule that does not exist, one that is not
  `<path>/<rule id>`, or one naming a rule in `grants.never_exempt` **blocks the
  next tool call**, naming the file — for the same reason a `disable` matching
  nothing does.

  **Who may create one.** Nobody who is running inside the tool. Every file that
  carries the owner's permission — the grant directory, the delete rule's
  `pm-delete-allow` ledger, and **both overlay files** — is listed in
  `grants.protected` (by reference to the one place its path already lives, never
  restated) and is refused to every agent, subagent and commander alike:
  `guards/grant_files.py` blocks the Write/Edit tools,
  `command_rules.tool_discipline/authority-file-in-shell` blocks the shell routes
  that guard cannot see (`python -c "open(...)"`, `sed -i`, `cp`, redirects).
  Without that, the mechanism is decorative: an agent that writes its own
  permission is not being permitted by anyone. See "What owner grants do not
  guarantee" below.
- **`overlays`** — the plugin layer for policy that lives outside this skill.
  `overlays.user_path` (default `~/.claude/ulthar-overlay.json`) is deep-merged
  with no *allowlist* restriction — the user owns both files — but "no allowlist"
  is not "anything anywhere", and the difference matters per kind of path:

  - a **declared** list (one with a `list_merge` strategy — every rule list) is
    **append-only** from either layer. Entries are added, a duplicate `id` is
    refused, and the only way one leaves is an explicit `disable`;
  - an **undeclared** list (`command_rules.redirect_noise_strips`,
    `quote_strip_patterns` — the ones holding no rules) may be **replaced**
    wholesale by the user layer, subject to the element shape the base entries
    already have;
  - a **scalar** may be overridden, and a **section** deep-merged;
  - the four **sections** in `overlays.pinned_paths` (`dispatch`, `grants`,
    `regex`, `overlays`) may be none of the above, from either layer — see "What
    owner grants do not guarantee".

  One further rule binds both layers and is not about authority at all: a path's
  CONTAINER KIND cannot change. A list merges only with a list, a section only
  with a section, a scalar only with a scalar (`_refuse_container_swap`).
  Swapping the container replaced the whole value — which is how every
  `tool_discipline` rule was once removed by a two-line overlay, and, for a
  scalar, how a container where `skill_anchor.envelope_key` lives crashed the
  dispatcher outright.
  `overlays.project_path` (default
  `<cwd>/.claude/ulthar-overlay.json`) carries the repo's own policy, and that
  policy takes precedence while work happens in that repo: it may append rules at
  the paths in `overlays.project_allowed_paths` (per the `list_merge` strategy
  declared for each — `append` for a plain-scalar list, `append_by_id` for a list
  of `{id: ...}` objects) and may switch named rules off through a top-level
  `disable` list of `<dotted config path>/<rule id>` identifiers. That allowlist
  is no longer a non-weakening boundary; it is the line between the **rule
  lists** a project has an opinion about and ULTHAR's own **wiring**
  (`paths.python_exe`, `dispatch.events`, `logging.*`, `deploy_guard`,
  `overlays.*`), which a cloned repo repointing would not be policy at all.
  What the merge still refuses is *accidental* weakening: an entry whose shape
  the consuming guard could not read is refused at merge time wherever the merge
  can derive that shape — which is every declared list, every section, and, since
  round 4, an undeclared list replaced wholesale by the user layer. Wherever it
  cannot, the dispatcher catches the consequence instead: **with any overlay
  merged, a guard that raises BLOCKS**, naming the overlay, rather than being
  turned into ALLOW by the fail-open. Alongside that, a `disable` matching no
  rule is an error rather than a silent no-op,
  and anything a project overlay disables is logged and stated once per session
  in the operator's context. Both layers are read once per dispatch, in
  `dispatch.py`'s `main()` immediately after `build_ctx` (not immediately after
  `load_config()` — `Ctx` must exist first), inside their **own**
  try/except: a malformed overlay (bad JSON, an out-of-allowlist path, a
  colliding `id`, a structurally broken entry, a `disable` naming nothing) is a
  `ConfigError` that **blocks** the whole dispatch, exactly like an invalid
  `config.json` — never a silent "no rules loaded" degradation. The try/except is
  separate from config loading's because the *recovery* differs: an overlay is
  neither under `hooks/` nor in git, so "redeploy the master" is the wrong
  instruction, and `dispatch.infra_recovery.overlay` supplies the right one.
  `install.py` merges only the **user** overlay layer (never the project one)
  before its own deploy/verify/register-settings checks that read `paths.*`, so
  a machine's pinned interpreter is honoured at install time without letting an
  arbitrary cloned repo's project overlay influence what gets installed. Two
  worked examples ship in `examples/`: `user-overlay.example.json` and
  `project-overlay.example.json`.
- **`thresholds`** holds cross-cutting numbers only (subprocess timeout budgets for
  local vs. remote external commands, etc.); every other numeric value lives with
  its guard.
- **`dispatch.events`** maps each hook event to an ordered list of
  `{tools, guards[]}` rules; `tools` is a regex over the tool name (or a
  `tools_named` reference into `regex.named_alternations`, to avoid the same
  tool-name alternation being duplicated at multiple call sites). Adding a guard to
  an event is a config edit plus one new module file — `settings.json` never
  changes.
- **`design_cases`** — the mechanism for "add a brief-gate enforcement rule as a
  pure config edit". Each case has `id`, `enabled`, optional `applies_to` (brief
  kinds), `triggers[]` (block if any matches), `satisfied_by[]` (block only if none
  of these also matches), and `message`. Two preconditions to know before adding a
  case: (1) the target brief *kind* must actually run a `design_cases`-type rule in
  its own `rules[]` (today only the `creator` kind does — `applies_to` narrows, it
  cannot extend which kinds evaluate the list at all); (2) the brief text must
  already have been classified as a brief at all (it must match the detection
  pattern and meet a minimum length).
- **`brief_rules`** — `detect` (how text is recognised as a brief at all) plus an
  ordered list of `kinds` (forwarder/committer/reviewer/creator today), each with
  its own ordered `rules[]`. Rule types are a closed set (`require_pattern`,
  `design_cases`, `approval_handshake`); adding a new rule *type* is a code change.
  A creator dispatch may satisfy several of these gates at once with a single
  compact header line instead of separate hand-written markers:
  `BRIEF: <file>.md | <kind> | no-ui` (`<kind>` and the `no-ui` tag are both
  optional). The referenced file is resolved the same way a long-form
  `APPROVAL:<file>.md` reference is (existence + a minimum age, or an explicit
  owner-approval marker inside it), and its content can also satisfy the
  acceptance-table and owner-decision-list checks; the `no-ui` tag can stand in for
  naming a design skill on a brief that genuinely does not touch UI. The long form
  keeps working unchanged — the compact header only adds alternatives, it removes
  nothing.
- **`command_rules`** — shell-command guard policy (`tool_discipline`,
  `dev_server`), as ordered rule lists with a small closed set of rule types
  (`regex_block`, `regex_block_any`, `regex_require`, `delete_allowlist`).
- **`git_policy`** — the git-discipline and PR-state policy: protected branches,
  deploy refs, blanket-staging/destroyer/hook-bypass/force-push rule lists, and the
  PR-state tokenisation rules. `enabled: false` on any list entry is a soft
  delete — the entry stays in place with its regex and rationale intact, simply
  skipped.
- **`commander` / `skill_anchor` / `deploy_guard` / `artifact_dirs` /
  `dev_processes` / `viewport_matrix` / `logging`** — one section per guard (or
  cross-cutting concern) holding that guard's own activation markers, thresholds,
  message templates and lists, so that enabling/disabling/tuning a rule is always a
  config edit, never a code change.
- **`regex`** — the pattern-kind/case-sensitivity rules described above, plus the
  authoritative enumeration of every regex location in the file
  (`pattern_fields`) and the shared named alternations used at more than one call
  site (`named_alternations`).

**Single-home rule.** Every value has exactly one key in `config.json`. A value
duplicated at two paths — one a guard reads while an operator edits the other copy
— reintroduces silent drift between config and behaviour, which is the failure
mode this design exists to avoid. `paths.deploy_dir`'s removal (above) is this rule
applied to a key nothing consumed.

## 4. Invariants — what must not change

- **Exit-code protocol.** Block = exit 2 with the reason on stderr. Allow = exit 0.
  No other exit code is ever produced.
- **`skill_anchor` never blocks.** It always returns exit 0; its stdout is either
  raw anchor text (`SessionStart`) or a compact JSON envelope (every other event).
- **Fail-open, but never silent.** A guard's own runtime exception never breaks the
  tool call — it is treated as ALLOW, the chain continues, and one line is
  appended to a dispatch-errors log (guard id, tool, error, a *sanitised* payload
  key list — never the payload contents themselves, since those can contain
  command text and file paths).
- **The dispatcher never trusts a guard's return value.** A non-`Result`, an
  invalid exit code, or unexpected stdout from a non-emitting guard are all
  treated like a raised exception (ALLOW + log). The one exception: a bare
  `exit_code == 2` with empty `stderr` still **blocks**, with a synthesised
  reason — because there the guard's intent (block) is unambiguous and only the
  explanation is missing; downgrading that case to ALLOW would let a config typo
  silently disable enforcement.
- **Infrastructure failures block, guard-logic failures fail open.** A guard
  raising at runtime is a bug in one rule (fails open, per above). A failure to
  *load the runtime itself* — config unreadable/invalid, a guard module failing to
  import, a guard's own config-declared pattern failing to compile, or context
  construction itself raising — is a deployment failure and instead **blocks**
  (exit 2) with an actionable message (what failed, how to diagnose with
  `install.py --verify`, and how to recover: fix and redeploy, restore the master
  tree to a known-good state and redeploy, or disable that event's hook entry until
  it is fixed), because approving a destructive operation when enforcement could
  not even start is the outcome this design exists to avoid.
- **Encoding.** Every `open()` passes an explicit encoding; a block message
  containing non-ASCII text must arrive on stderr as UTF-8 bytes.
- **The deploy-guard covers every deployed file type it protects, at any depth**
  under the deploy directory — not an enumerated subdirectory list, which would
  both narrow coverage and go stale the moment a new subdirectory appeared. This
  includes the deployed `config.json` itself, protected the same way a deployed
  guard module is (direct edits to the deployed copy are blocked; edit the master
  and redeploy).
- **The delete-allowlist ("pm-delete-allow") semantics**: exact absolute paths,
  one per line; every offending path in a command must be listed or nothing is
  granted; on grant, matched entries are consumed (removed from the list) and a
  non-blocking notice is emitted; usable only from the main/commander chain, never
  from a subagent.
- **`config.json` is the only policy file the runtime loads.**
- **Dispatcher I/O order.** Rules run in order; within a rule, guards run in
  order; each guard's stdout is accumulated (not written as it's produced) and
  the accumulated buffer is written once — either when a guard returns
  `exit_code == 2` (written together with that guard's stderr, which terminates
  the run) or, if none does, once at the end of the whole chain. A non-empty
  stderr on `exit_code == 0` is dropped. At most one guard per chain is allowed
  to emit stdout.
- **`settings.json` registration is rewritten only after the full test suite is
  green**, in order: backup → tests pass → rewrite the dispatcher registration
  lines to point at the Python entry point → keep the previous registration
  mechanism available on disk for rollback.
- **`stop_hook_active` is honoured by every Stop-event guard**, checked first,
  before any other work — without it, a guard that blocks on `Stop` can never
  terminate the harness's own continuation loop.
- **The `psutil` dependency is loud and confined.** `install.py --verify` (and the
  default deploy path) imports every declared required dependency and fails with
  an explicit, actionable message if it cannot be imported under the pinned
  interpreter — it must never warn-and-continue, because a silently no-op process
  guard looks identical to "no orphans found". The dependency is imported only from
  the Stop-hook path and the manual CLI, never from the PreToolUse path, which runs
  on every single tool call.

## What owner grants do not guarantee

Stated here rather than implied, because the mechanism is easy to over-read as an
authorisation system. It is not one.

- **Nobody is authenticated.** The agent and the owner share a filesystem and an
  identity. A file's author cannot be established, so "the owner wrote this" is
  never verified — it is made *structurally likely* by removing every route an
  agent has to write it.
- **The shell rule is a regex over command text.** It matches a `.claude` path
  component followed by the specific file name, so ordinary work on ULTHAR itself
  (`grep -rn pm-delete-allow hooks/`) still runs. A command that assembles the
  same path from pieces, or from a variable, is not matched — and no widening of
  that pattern closes it, because the evasion is in the shell, not in the string.
- **Getting the separator wrong is not the same as the gap above, and that one
  was ours.** Until 2026-09-06 the separator was `[\\/]` — exactly one character
  — so `.claude\\state\\ulthar-grants\\x` was allowed. That spelling is not
  evasion; it is what a Windows path looks like inside a non-raw Python string,
  which is precisely how an agent writes one in a `python -c` one-liner, and it
  was measured through the dispatcher at rc=0. `//` and a `/./` segment did the
  same. It is a separator **run** with optional `.` segments now. The lesson
  worth keeping: a pattern over a command string cannot be complete, but the
  spellings a writer would *naturally* produce are not the residue — they are the
  rule not working.

  A third spelling in that family was **not** narrow, and calling it residue was
  the mistake. Win32 discards trailing dots **and trailing spaces** from every
  path component, so `.claude./state/…` and `.claude /state/…` name the same file
  as `.claude/state/…` — but `os.path.normpath` keeps them, so
  `lib/paths.py:norm_for_compare` compared them as different paths. That is not a
  shell-pattern gap; it is the **Write/Edit** half, `guards/grant_files.py`,
  which is the half that was supposed to make the other one's residue narrow.
  Measured `rc=0` for a Write to the grant directory, the user overlay and the
  pm-delete-allow ledger, and the file landed in the real `.claude/state/…`: one
  Write, no overlay, and the agent had written its own permission. Fixed in
  `norm_for_compare` rather than in the guard, because that function is the
  single door every equality and containment check goes through — the same change
  covers `commander`'s outside-cwd test, `skill_anchor`, and the ledger compare —
  and the shell pattern was given the same tolerance. POSIX is deliberately
  untouched: there a trailing dot is an ordinary filename character.

  Windows has more spellings of one file than a string comparison can enumerate,
  and the rest of that class was measured in the round after: `\\?\C:\…` and
  `\\.\C:\…` (extended-length and device prefixes, which `normpath` keeps, so the
  first component compared as `?`), `\\localhost\C$\…` (the admin share),
  `\\?\UNC\localhost\C$\…`, `<file>::$DATA` (an alternate data stream on an
  exact-file target), 8.3 short names, and volume-GUID and `GLOBALROOT` device
  paths. All measured `rc=0` on a Write of a grant path and all really created
  the file. The prefixes and the admin share are rewritten lexically before
  comparison and the stream suffix is truncated; 8.3, volume GUIDs and
  `GLOBALROOT` cannot be reached that way at all — the mapping lives in the
  filesystem — so `is_under` gets **one** `realpath` second chance, as an
  *additional* accept only. That direction matters: every caller treats True as
  the guarding answer, so resolving can tighten a verdict and never loosen one,
  and the module's "judge the literal path, not its target" rule still decides
  the lexical answer. The second chance never runs on a UNC path or a drive that
  does not exist, because `realpath` on an unreachable host took **21.5 s per
  tool call** against a 0.4 s baseline, and a guard that adds twenty seconds to
  unrelated work gets switched off. What that leaves open, and it is the honest
  residue: a UNC spelling whose share is **not** an admin share, and an 8.3 name
  for a path that does not exist yet.

  What genuinely does stay open in that family, stated precisely: a `..` segment
  (`.claude/state/../state/<grants dir>/x`) is not matched by the **shell** rule.
  Normalising a path inside a command string is a different operation from
  matching one — the string need not even be a path on the machine that runs it,
  and `..` resolution would have to assume a base directory the command may never
  use. The Write/Edit half **does** block it, because `normpath` resolves `..`
  before the comparison. So this one is shell-only, and that is the whole of the
  claim.
- **A command that names only the destination directory is invisible to this
  rule.** `cp tmp/overlay.json .claude/`, `tar -xf x.tar -C ~/.claude`, `git
  apply` on a diff the agent wrote, `git checkout <ref>`, `git stash pop`, `git
  clone` — none of them carries an authority path in the command text, so no
  widening of the pattern reaches them. An agent can therefore *materialise* an
  overlay or a grant file without ever naming one.

  **What that is bounded by, and what it is not.** The bound is a bound on what
  a *configuration file* can express, and it holds: a materialised overlay
  cannot switch off the rules that protect the mechanism, in any shape. It is
  **not** a bound on an agent with shell access, and treating it as one would be
  the overstatement this section exists to remove. The same routes land more
  than overlay files: `cp x <deploy dir>/dispatch.py`,
  `tar -xf -C <deploy dir>`, `git apply`, `git clone`,
  `python -c "open('<deploy dir>/dispatch.py','w')"` and
  `cp x <skill>/config.json` all reach the **deployed runtime and the master
  config**, and all measured `rc=0` through a temp deployment's own dispatcher.
  `deploy_guard` protects the deployed copy of every guard through the
  **Write and Edit tools only** — there is no shell half, and one was considered
  and deliberately not added, because a rule over that directory would carry
  exactly the same destination-directory residue this bullet is about: it would
  move the line without closing it, while reading like a guarantee. So the
  configuration bound below is worth having on its own terms — an owner's slip
  or a cloned repo's overlay cannot silently hollow the mechanism out, and every
  attempt is a loud refusal naming the file — and it is not more than that.

  The configuration bound itself is `grants.never_exempt`, which since 2026-09-06 is
  enforced against an overlay `disable` as well as against a grant file: a
  materialised overlay may weaken ordinary rules — which a cloned repo may do
  deliberately anyway — and may not switch off `authority-file-in-shell` or
  `delete-outside-disposable`. Enforced on **both** layers, the user's own
  overlay included: "it is the owner's own file" is not a structural property
  when `cp x ~/.claude/` reaches it exactly as easily as a project one. The list
  is pinned along with the rules it names — `grants.never_exempt` is not in
  `overlays.list_merge`, so it merged by replacement, and a user overlay saying
  `{"grants": {"never_exempt": []}}` would have handed back both the `disable`
  route and the grant route (`lib/grant.py:validate` reads the merged config).
  An overlay that rewrites it is refused. Removing a rule from a rule list is
  only ever possible through `disable` — and that is a statement about what the
  merge REFUSES, not a property that fell out of the shape of the code. It took
  three rounds and is now enforced in three places: a declared rule list is
  append-only (a duplicate id is refused, so nothing can be shadowed out); the
  container mirror refuses a list merged with anything but a list and a section
  merged with anything but a section, in both directions, with an empty object
  expressing nothing rather than erasing; and, behind both, an invariant on the
  merge OUTPUT — after every layer has merged, each rule named in
  `grants.never_exempt` must still be present and each `grants.protected`
  reference must still resolve through the same resolver `grant_files` uses.
  The last one exists precisely because the first two are shape checks, and
  every round found one more shape they did not cover. What
  remains reachable is a materialised **grant** for an ordinary rule; that is
  accepted knowingly, because `~/.claude/state` is deliberately disposable and
  blocking traffic to it would break what the directory is for. Such a grant is
  still one rule, one call, main chain, logged. Note also that the **user** layer
  is otherwise unrestricted — `overlays.project_allowed_paths` fences the
  project layer only — so a materialised user overlay can repoint ULTHAR's own
  wiring (`paths.*`, `logging.*`) in ways a project overlay cannot. (`grants.dir`
  was in that list and is not any more: it sits inside the pinned `grants`
  section, so neither layer can repoint it.)
  That is the layer model working as designed for a file the owner owns; it is
  listed here because materialisation is what makes "a file the owner owns" less
  than a guarantee.

  That freedom stops at `overlays.pinned_paths`, which the same round's second
  sweep grew from one path to four. The disclosure above named `grants.dir`,
  `paths.*` and `logging.*` and omitted the path that switches everything off:
  `dispatch.events` is not in `overlays.list_merge`, so the user layer replaces
  a list there wholesale, and `{"dispatch": {"events": {"PreToolUse": []}}}`
  measured `rc=0` on a command the delete rule blocks — every `PreToolUse` guard
  gone. Nothing was *disabled*, so the operator notice (which covers a project
  overlay's `disable`) did not fire either. Two more of the same class:
  `{"grants": {"protected": []}}` emptied the list `guards/grant_files.py`
  protects, so the Write tool could create a grant file; and a `disable` whose
  left half is a pinned list — `{"disable": ["grants.never_exempt/<entry>"]}` —
  reached the carve-out through the other door, because an identifier splits at
  its first `/` and the left half is just a dotted list path like any other,
  removed by `_apply_disables` *after* the merge.

  Pinning those four **leaf paths** was the first answer and it was not enough.
  The next review measured five more single-overlay switch-offs the list did not
  reach, each `rc=0` on a Write of a grant file that is `rc=2` with no overlay:

  - `{"disable": ["dispatch.events.PreToolUse/None"]}` — a **child** of a pinned
    path. The routing entries carry no `id`, so `_entry_id` yields the string
    `"None"`, `_apply_disables` matches one, and the whole chain goes. Equality
    on the identifier's left half never saw it.
  - `{"regex": {"named_alternations": {"write_edit_tools": …}}}` — the Write/Edit
    chain is routed through that **name**, so repointing it unroutes every guard
    on it without touching `dispatch` at all.
  - `{"grants": {"enabled": false}}` — `guards/grant_files.py` returns ALLOW at
    its first line, so all four protected files become writable *including the
    user overlay*, which makes the change permanent.
  - `{"grants": {"dir": …}}` — the Write half follows `grants.dir` while the
    shell rule holds the directory name as a literal, so a repointed directory
    was reachable through the shell and the grant honoured end to end.
  - `{"grants": {"protected_message": 5}}` — a **type**. `grant_files` formats it
    with `.replace()`, a non-string is an `AttributeError`, and the dispatcher's
    fail-open turns that into ALLOW. Shape validation does not cover it: it
    validates *list entries*, and every scalar a guard reads with a type
    assumption is the same shape of bug.

  So `overlays.pinned_paths` holds **sections, not paths**: `dispatch`, `grants`,
  `regex`, `overlays`. Enumerating leaves is chasing instances of a class, and
  the class is "a path that is not a rule but decides what a rule can do".
  `lib/ctx.py:_refuse_pinned_paths` reads the list from the **base** config and
  refuses, from either layer, any rewrite of a pinned subtree and any `disable`
  whose left half is a pinned prefix or starts with `<prefix>.`.

  The argument is the one that moved `never_exempt` to both layers, and it is
  about materialisation rather than trust. **What an owner gives up:** rerouting
  guards, re-listing the protected files, changing either carve-out, moving the
  grant directory, turning the grant mechanism off, and extending
  `regex.named_alternations` — that last one is a legitimate use, and losing it
  from an overlay is a real cost, not a rounding error. **What they keep:** doing
  all of it in `config.json`, which no overlay can reach and which `install.py`
  re-registers the hooks from — the same recovery already published for the two
  `never_exempt` rules. "No overlay can reach it" is exactly that and no more:
  `config.json` is an ordinary file, writable by the Write tool and from the
  shell, and both were measured. What the pins protect it from is a
  *configuration file*, not an agent. Everything outside the four sections stays overlayable,
  `logging.*` and `paths.*` included; every rule list stays appendable and
  disableable from both layers, and the owner's standing decision that a project
  overlay may weaken ordinary rules is untouched.

  One consequence worth naming rather than leaving implicit: the shell rule keeps
  the grant directory's name as a **literal** instead of deriving it from
  `grants.dir`. Deriving would be new machinery (`regex.named_alternations`
  resolves `*_named` *references* at routing and guard sites, not interpolations
  inside a rule's pattern), and it is no longer needed — `grants.dir` sits inside
  a pinned section, so the literal cannot drift away from the value the Write
  half follows.
- **A rule list could be replaced by something that is not a list**, and no
  pinned path was involved. `{"command_rules": {"tool_discipline": {"zz": 1}}}`
  in a user overlay put a dict where the base holds a rule list, so `_merge_walk`
  recursed into it as a section and `_set_dotted` — finding a list where it
  wanted to descend — replaced the whole list with `{}`. Every `tool_discipline`
  rule vanished, `authority-file-in-shell` included, which is the rule
  `grants.never_exempt` exists to keep alive; measured `rc=0` on a shell `touch`
  of a grant path. A scalar did the same. `_merge_list` already refused the
  mismatch the other way round ("base config value at this path is not a list");
  neither spelling ever reached it, because the value was not a list to begin
  with. `_refuse_container_swap` is the missing mirror, and an ordinary append to
  the same list still merges.

  The next round found the same hole one level up, which is why there are now two
  defences and not one. `{"command_rules": 0}` — and `null`, `""`, `false`, and
  `{}`, which the walk was sending to the leaf branch because it tested
  `isinstance(value, dict) and value` — replaced the whole SECTION. Both
  `never_exempt` rules went with it, and the guard's `KeyError` became ALLOW
  through the fail-open: `rc=0` on a shell write to a grant path, on a delete
  outside the allowlist, and on a Write of the `pm-delete-allow` ledger, whose
  `grants.protected` reference then resolved to nothing. `{"deploy_guard": 0}`
  did the same to the deploy guard. The mirror is complete in both directions now
  (a section merges only with a section, and an empty object expresses nothing
  rather than erasing), and behind it `_refuse_authority_erasure` states the
  thing the shapes were only ever a proxy for, as an invariant on the merge
  OUTPUT: every `grants.never_exempt` rule must still resolve in the merged
  config, and every `grants.protected` reference must still resolve through the
  same resolver `grant_files` goes through. Reverting each in turn shows they are
  independent — the invariant alone catches all four `command_rules` spellings,
  the mirror alone catches all five, and with both reverted every one is `rc=0`
  again. The point of keeping both is that the mirror is a shape check, and every
  round so far has found one more shape.
- **Every rule written in terms of shell verbs has the same shape of gap, and the
  delete rule's version of it is closed only for the natural spelling.** An
  interpreter invoked from the shell is a verb no shell-verb pattern recognises:
  `python -c "shutil.rmtree(...)"` reached past the delete rule until the pattern
  was taught to see a deletion-API call next to an interpreter word (2026-09-06,
  after an agent used it and disclosed it). What that closes is the reflex — the
  command an agent reaches for the moment `rm -rf` is refused. What it cannot
  close: a deletion inside a script file (`python cleanup.py`), a path built from
  a variable or `os.environ`, and shelling back out (`subprocess.run(["rm", ...])`),
  which re-enters past *every* shell rule, git included. A second spelling of the
  same shape was found in the final review and closed with it: a **quoted shell
  wrapper** — `bash -c "rm -rf ..."`, `sh -c '...'`, `powershell -Command
  "Remove-Item -Recurse -Force ..."`, `cmd /c "del ..."` — is not a segment
  start, so the verb inside it was never looked at. The PowerShell one mattered
  most: it is the native Windows spelling and the rule's own block message names
  `Remove-Item` as covered. The fix requires a wrapper word near the verb rather
  than just a quoted verb, because a bare quoted verb turned `git commit -m "rm
  docs/old.md"` into a false block — and a rule that blocks routine work gets
  switched off, which is the failure this whole section is about.

  A fixture sweep in the same review found that fix incomplete and the residue
  list below reading as exhaustive when it named only the piped case. Seven more
  spellings measured **allowed** through the dispatcher, three of them inside the
  wrapper fix's own scope: `bash -c "sudo rm -rf …"`, `bash -c "exec rm -rf …"`
  and `bash -c "/bin/rm -rf …"` (the wrapper branch required the verb
  immediately after the quote), plus `/usr/bin/rm -rf …`, `env rm -rf …`,
  `find <dir> -name x -delete` and `find . -name '*.db' -exec rm {} \;`. By this
  section's own standard those are defects, not residue — they are what an agent
  reaches for the moment `rm -rf` is refused. Three narrow additions close them:
  an optional **path prefix** before the verb (it must end in a separator, so
  `git rm docs/old.md` and `cp a/rm b` are untouched — `git` is deliberately not
  a prefix word and must never become one); `sudo|env|exec` as wrapper words
  **and** as an optional run after the wrapper's quote; and a `find` branch
  matching `-delete` and `-exec <verb>`. The false-positive cases that shaped
  the earlier fixes were re-run and a fourth added (`git commit -m "sudo broke,
  rm tests/x"`), because segment-start anchoring is the only thing keeping those
  allowed.

  **What stays open, each pinned by a fixture so none is mistaken for coverage:**

  - **Piped** deletion **where the path is in the other segment** —
    `find … | xargs rm -rf`, `echo <path> | xargs rm -rf`. The literal path is
    `find`'s or `echo`'s output and the verb is after the pipe, so no single
    segment holds both and the token check has nothing to judge. That half is out
    of reach of a segment-based rule by construction. The earlier claim that
    `xargs` was out of reach *as such* was false and is closed: when the path is
    a literal **argument** in the same segment (`xargs -I{} rm -rf <path>`,
    `echo x | xargs rm -rf <path>`), the verb and the path do meet, so `xargs` is
    a wrapper word now and both spellings block.
  - **The wrapper set is an enumeration**, so any prefix command not on the list
    passes: `setsid`, `stdbuf -o0`, `watch -n1`, `ionice` were all measured
    `rc=0`. Three were added when they were found (`time`, `doas`, `busybox`)
    because they are what an agent reaches for next; the next one is always
    unlisted. A fixture pins `setsid` as ALLOWED so the shape of this residue
    stays visible rather than being rediscovered.
  - **A file whose basename IS a delete verb is not judged** — `rm -rf tools/rm`,
    `tools/rm.exe`, `src/del`. That is the price of excluding the verb token from
    path judgement, and the exclusion was not optional: without it the rule
    reported its own verb as a bad path and BLOCKED `/usr/bin/rm -rf dist/x`, a
    delete the allowlist permits, while the same delete spelled `rm -rf dist/x`
    was allowed. Refusing permitted work is the failure this rule is organised
    against, so the narrower hole was taken over the wider false block. The
    single-letter `/x` switch exclusion (added for `cmd /c rd /s /q dist`) has
    the same shape: a one-character absolute path such as `/s` is no longer
    judged either.
  - **The verb set is an enumeration too, in the other direction**: it contains
    two-letter words that are whole commands elsewhere. `ri` is Ruby's
    documentation browser and `rd` a directory-removal builtin, so
    `ri lib/foo.rb` reads a file and is refused as though it deleted one.
    Dropping either would open the `cmd` and PowerShell spellings the set exists
    for, so this is named rather than fixed.
  - **A target with no separator and no dot** — `rm -rf owner-files`, and
    therefore `find owner-files -delete` too. `path_token_pattern` requires a
    `/`, `\` or `.` in the token, so a bare word yields nothing to judge.
    Pre-existing, not introduced by the `find` branch; widening the token test
    to bare words would make every flag and subcommand a candidate path.
  - **Quote nesting**, of which this rule has no notion: a command whose *string
    content* contains a delete verb is judged as a delete when a wrapper word
    starts the segment. It is why `git commit -m "x; rm docs/old.md"` blocks, the
    `;` being a segment separator. An earlier version of this bullet said the
    error could only ever go that way — that quote-blindness "over-blocks, never
    under-blocks". That was false: a **quoted verb at segment start**,
    `"rm" -rf <path>`, measured `rc=0`, and it is a spelling an agent types
    straight after a block. Closed with an optional quote around the verb in both
    patterns; the anchoring is what keeps `git commit -m "rm docs/old.md"`
    allowed, since that segment starts with `git`. The remaining direction really
    is over-blocking, and it is stated as that rather than as a guarantee. The
    same blindness applies to `|`: `grep -rn "cmd /c|/usr/bin/rm" fixtures/`
    splits at a pipe that is *inside quotes*, and the second half starts with
    `/usr/bin/rm"` — so searching the source for a blocked spelling is refused as
    though it were one. Without the pipe the identical command runs. Both are the
    segment splitter, not the patterns, and both wait on the same
    segment-extraction rework.
  - **What the `find` branch costs**, accepted knowingly: `find . -name '*.pyc'
    -delete` now blocks on the `.` token, exactly as `rm -rf .` always has. It
    is a recursive delete rooted at cwd rather than at a disposable directory,
    so blocking is the safe direction; the routine bytecode path an agent
    actually needs, `rm -rf <dir>/__pycache__`, stays allowed, which is what
    keeps this from manufacturing the incentive to route around the rule. The
    second cost is more common: `find`'s `-name` argument is a path-shaped token
    like any other, so `find dist/ -name '*.map' -delete` blocks on `*.map` even
    though `dist/` is allowlisted. Rejecting `*`-leading tokens would allow
    `rm -rf *.db`, so it was not done; the escape hatch is the shell glob
    (`rm -rf dist/*.map`), which is allowed because the token carries its
    directory.

  The `git_policy` rules are
  further out of reach still: they match a quote-stripped probe by design (so that
  `git commit -m "merge main later"` is not read as a merge), which means any git
  command inside an interpreter's quoted string is invisible to them by
  construction.
- **Social engineering is out of reach entirely.** An agent that asks the owner to
  run a command whose effect the owner does not understand gets the grant, and
  nothing in a hook can tell that apart from the owner deciding for themselves.
- **`settings.json` is above all of it — and so, in practice, is the deployed
  tree.** An agent able to edit the harness's own hook registration removes
  enforcement wholesale; no guard can prevent the removal of the mechanism that
  runs the guards. One level down is the same story and was measured rather than
  assumed: the deployed `hooks/` tree and the master `config.json` are ordinary
  files, and `cp`, `tar -xf -C`, `git apply`, `git clone` and
  `python -c "open(...)"` all reach them at `rc=0`. `deploy_guard` covers the
  deployed copy through the **Write and Edit tools only**; a shell half for that
  directory was considered and not added, because it would carry the same
  destination-directory residue as the authority-file rule — a rule that reads
  like a guarantee and is not one is worse than a stated gap. This is the reason
  the overlay pins are described as a configuration bound throughout: they stop
  a config file from hollowing the mechanism out, not an agent from replacing it.
- **A `null`/`""` scalar over an existing scalar is accepted by the
  container-swap mirror.** `_refuse_container_swap` refuses a container (list
  or dict) swapping in for a scalar, or vice versa, but it never checks a
  scalar's *type* against the scalar it replaces — `null` and `""` are both
  scalars, so either passes. For most keys that lands as a wrong value read at
  the point of use. For `skill_anchor.envelope_key`, which becomes a `dict`
  **key** in `dispatch.py`'s notice envelope, `json.dumps` stringifies
  whatever key it is handed — `None` becomes the string `"null"` — so the
  notice envelope's key silently changes; `rc` is unchanged and the log line
  for that dispatch is written exactly as if nothing happened.
- **Visibility is not permission, which is why logging alone was rejected as the
  answer.** A log line is written after the grant file is gone and the force-push
  has already been pushed. It tells the owner what was done to them; it never lets
  them decide. The record is worth having as a record — not as a substitute for
  the owner's part in the exchange.

What the design does claim: through the tools ULTHAR guards, self-granting stops
being the obvious next tool call and becomes deliberate evasion, every consumption
is on the record, and a grant can never widen past the one rule it names, the one
call it is spent on, or the main chain.

## Known misreadings

Failure modes that are easy to introduce while still passing a naive test, ordered
by how badly they fail:

1. Expanding every message placeholder to an absolute path "for consistency" — some
   placeholders are the raw config value, some are a basename, some are an
   expanded absolute path; each is documented individually next to its rule.
2. Applying one "probe" transform (e.g. quote-stripping) as a uniform default for
   every phase of git-command matching — one phase deliberately sees the raw
   command, one deliberately matches nothing and shells out instead, and the rest
   see the transformed probe; collapsing this to a single default silently changes
   which commands trip which rule.
3. Collapsing "a guard returned something malformed" into a single "always ALLOW"
   rule — a bare block with no reason given is the one case that still blocks, with
   a synthesised reason, precisely so a config typo cannot silently turn off
   enforcement.
4. Inferring a regex pattern's case-sensitivity "kind" from its field name instead
   of the authoritative declared list — names are actively misleading in both
   directions.
5. Compiling the `"*"` sentinel value as if it were a real pattern — it means
   "matches anything" and must be checked by identity before any compile attempt.
6. Treating a `null` config value as "unset, use a default" when it is
   documented to mean a specific alternative behaviour (e.g. "this phase matches
   nothing and shells out instead").
7. Testing "is this the main/commander chain" by checking only whether one field is
   present — both an absent value and an explicitly empty one mean the same thing.
8. "Fixing" a regex split that appears to leak its own delimiters into the result —
   several call sites deliberately rely on that behaviour.
9. Migrating or writing a test fixture without every field a test runner expects —
   an incomplete fixture can silently pass by testing nothing at all.
10. Reading "adding a policy case is one config edit" without its stated
    preconditions — narrowing fields cannot extend evaluation to a kind that never
    ran that rule type in the first place, and the input must already have been
    classified as eligible before any case in the list gets a chance to fire.
11. Assuming `os.remove()` decides a race — "only one caller can delete a file".
    Measured on Windows, two threads racing `os.remove()` on one path **both
    report success in roughly half of 300 trials**: deleting sets a disposition on
    a directory entry that is still open, and a second delete arriving inside that
    window succeeds too. Anything that consumes a one-shot token by deleting it
    must decide the winner with `os.open(..., O_CREAT | O_EXCL)` (exactly one
    winner, 300/300 in the same harness) and delete only afterwards. The same
    surprise applies to `os.replace`/rename, which is documented at its own call
    site.
12. Applying a general "fail open on any error" rule to a failure of the *runtime
    itself* (config unreadable, a guard module failing to import) — that specific
    failure class blocks instead, deliberately, because approving a destructive
    operation with enforcement unable to even start is worse than blocking it.

## Parity exceptions

`docs/PARITY-EXCEPTIONS.md` documents every `PE-<n>` id: a deliberate design
decision recorded at the point a Python-runtime guard's behaviour was chosen to
differ from (or, for the newest entries, simply has no precedent in) an earlier
implementation of this hook runtime that is not part of this distribution. Guard
fixtures under `hooks/tests/fixtures/` cite these ids on any test case whose
expected result is intentionally one of these documented exceptions; the test
suite cross-checks that every declared id is cited by at least one fixture and
vice versa. `hooks/tests/parity.py` is a self-contained harness that historically
also ran that earlier implementation side-by-side for a live comparison; since that
earlier implementation is not present here, running it always reports retirement
(no comparison performed) rather than a pass or fail — the citation and
fixture-count checks it still performs alongside that report are real, exercised
by `python hooks/tests/run_all.py`.
