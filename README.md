# ULTHAR

A hook runtime for Claude Code that turns an orchestration discipline into something a
PreToolUse/Stop hook can actually block, instead of a paragraph an agent is asked to
follow. If you don't run multi-agent Claude Code sessions where one agent (the
"commander") delegates to subagents and reviews their work, this repo has nothing for
you — feel free to leave now.

If you do: the commander here is not trusted to behave. It is mechanically blocked
from touching deliverables itself, from running unsafe shell commands, from destructive
or hook-bypassing git operations, and from dispatching a brief that skips a required
element — and the block applies to the commander's own actions, not just a subagent's.

The name: **U**nified **L**ead, **T**ask **H**andoff & **A**cceptance **R**eview, after
the Dreamlands town where a law — "no man may kill a cat" — is inviolable. That's the
thesis: the discipline is enforced, not requested.

## What it actually enforces, and what it doesn't (yet)

Two different things live in this repo, and they activate differently:

- **Guards that work immediately, no setup beyond install**: `tool_discipline` (blocks
  `cd a && b` compounds, shell-based file creation, deletes outside a disposable-path
  allowlist — by `rm`/`Remove-Item`, by an interpreter one-liner such as
  `python -c "shutil.rmtree(...)"`, by a quoted shell wrapper (`bash -c "…"`,
  `powershell -Command "…"`), by a prefixed or absolute-path verb (`sudo rm`, `env rm`,
  `/usr/bin/rm`) and by `find … -delete` / `find … -exec rm`; what still gets past it is
  listed in `docs/DESIGN.md`), `git_discipline` (blanket staging, `git clean`/`reset --hard`/`restore`,
  `--no-verify`/`--no-gpg-sign`, force-push, protected-branch pushes), `pr_state`,
  `dev_server` (mechanism only — it ships with an empty rule list, so it blocks
  nothing until a project or user overlay adds a command rule; see "Overlays" below),
  `deploy` (config section `deploy_guard`; blocks direct edits to the deployed hook
  files), `grant_files` (blocks every agent from writing the files that carry *your*
  permission — see "Owner grants" below), `brief_gate` (required elements in a subagent dispatch), `artifact_hygiene`
  and `dev_process_hygiene` (Stop-hook cleanup checks). These read `config.json` and
  run on every matching tool call regardless of any marker file.
- **The "commander may not implement" rule itself** (`guards/commander.py`) and the
  skill-context injector (`guards/skill_anchor.py`) are **inert until you create a
  marker file** — `~/.claude/state/commander-mode` containing the single line `enforce`
  (or a per-repo `.claude/state/commander-mode` override) and `~/.claude/state/active-skills`
  containing `ulthar`. With neither file present, `commander.py` returns allow before it
  even logs — there is no ambient "block by default" on a fresh install. `SKILL.md`
  describes arming both markers at session start; do that if you want the
  self-implementation ban enforced, not just documented.

Full guard-by-guard detail: [`SKILL.md`](SKILL.md) (the discipline and when to invoke
it), [`docs/DESIGN.md`](docs/DESIGN.md) (architecture, guard interface, invariants),
[`references/`](references) (the operating protocol: gate rules, briefing templates,
task playbooks), [`hooks/README.md`](hooks/README.md) (runtime internals — Japanese,
written for a maintainer of the hook code, not a first-time reader).

## Install

Requirements: **Python 3.11+**, and **`psutil`** (used only by the Stop-hook
`dev_process_hygiene` guard and the `stop_dev_orphans.py` CLI; nothing on the
PreToolUse path imports it).

```bash
python -m pip install psutil
python hooks/install.py
```

Use `python -m pip`, not a bare `pip`, for the first line: on a multi-interpreter
machine `pip` and `python` can resolve to different installs (a virtualenv `python`
with no `pip` on its own `PATH`, while `pip` itself is some other interpreter's
`Scripts/pip.exe`), and then the dependency lands somewhere the second line's
`python` never looks. `python -m pip` runs pip as a module of that exact
interpreter, so the two commands are bound together by construction.

This copies `config.json`, `dispatch.py`, `install.py`, `stop_dev_orphans.py`, `lib/`
and `guards/` into `~/.claude/hooks` (or `--deploy-dir <path>`), refuses to deploy an
invalid `config.json`, and checks the `psutil` import under the effective interpreter.

`config.json` ships `paths.python_exe` **empty**, which just works: an empty value
means "resolve from whichever interpreter is currently running"
(`lib/paths.resolve_python_exe` falls back to `sys.executable`), so a fresh clone
needs no interpreter setup at all. Pin a specific interpreter only if you want one —
e.g. for `install.py --register-settings`, which writes the resolved path verbatim
into `settings.json` and re-verifies it matches. A pin belongs in your own user
overlay (`paths.python_exe` under `~/.claude/ulthar-overlay.json`, see "Overlays"
below), never edited into this shipped `config.json` directly.

**It does not touch `settings.json` for you.** `install.py --register-settings` exists,
but it is a one-time migration helper for an earlier PowerShell build of this runtime —
its pre-check requires every hook event to already hold exactly one `dispatch.ps1`
registration, which is never true on a fresh clone, so it will refuse to run for you.
Wiring a fresh install in means adding the three hook entries yourself to
`~/.claude/settings.json` under `hooks`, one per event key in `config.json`'s
`dispatch.events` (`PreToolUse`, `Stop`, `SessionStart`). This is a complete,
copy-pasteable example for a fresh install with no other hooks registered yet
(if `~/.claude/settings.json` already has a `hooks` block, add these three array
entries to their matching event keys instead of replacing the file):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "\"C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python311\\python.exe\" \"C:\\Users\\you\\.claude\\hooks\\dispatch.py\" --event PreToolUse"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "\"C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python311\\python.exe\" \"C:\\Users\\you\\.claude\\hooks\\dispatch.py\" --event Stop"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "\"C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python311\\python.exe\" \"C:\\Users\\you\\.claude\\hooks\\dispatch.py\" --event SessionStart"
          }
        ]
      }
    ]
  }
}
```

Replace the two quoted paths with your own interpreter and deploy directory (the
default deploy directory is `~/.claude/hooks`; `--deploy-dir` changes it). The
interpreter path **must be absolute and quoted**, not a bare `python`: with no pin
configured, `install.py --verify` still requires the registered command to match
`"<python_exe>" "<dispatch.py>" --event <Event>` exactly (both operands quoted) and
checks that `<python_exe>` resolves to a real, executable file on disk — a bare
`python` relying on `PATH` resolution does not resolve to a file at that literal
path and fails that check. If you pin an interpreter via `paths.python_exe` (in your
own user overlay, not this shipped `config.json`), `--verify` instead requires the
registered path to match that pin. This is a direct edit of your own `settings.json`;
back it up first if you want to undo it. **To undo:
remove those three entries by hand, then run `python hooks/install.py --uninstall`** to
remove the deployed files (it deletes exactly the files it deployed, one by one, and
names anything else it finds — so a file you or another tool keeps inside `lib/` or
`guards/` is left in place and listed as `KEPT:`). The same goes for a deploy: it
overlays its own files rather than replacing those directories, and lists what it
left alone as `PRESERVED:`. The one exception is `__pycache__`/`*.pyc` under those
two directories, which is regenerable cache the dispatcher itself writes and which
uninstall removes.

Once `settings.json` is wired, `python hooks/install.py --verify` hash-compares master
vs. deployed files, re-checks `psutil`, smoke-tests one guard, and confirms the
`settings.json` registration — all four must pass for `--verify` to exit 0. A deployed
file with no master counterpart is reported as `EXTRA:` but does not fail the run:
nothing on disk distinguishes a file ULTHAR never deployed from a leftover of an older
copy set, and failing would make `--verify` permanently red for anyone keeping their
own helper in `lib/`. The registration check is symmetric: every event in
`config.json`'s `dispatch.events` must have a `settings.json` hook (as before), and
every `settings.json` hook that runs *this deployment's* `dispatch.py` must in turn be
routed by `dispatch.events` — an orphaned entry left behind after a route is removed
fails with `SETTINGS: <event> is registered in settings.json but config.dispatch.events
does not route it`. A foreign hook under the same event name, or one pointing at some
other deployment's `dispatch.py`, is not ULTHAR's to report and is ignored.

## Worked example: add an enforcement case with a config edit, no code change

Say you want to block force-deleting a git branch (`git branch -D`), which
`git_discipline` doesn't cover today. Add one object to `config.json`'s
`git_policy.destroyers` list:

```json
{
  "id": "branch-force-delete",
  "enabled": true,
  "suffix": "branch\\s+(-D\\b|--delete\\s+--force\\b)",
  "why": "git branch -D (force-deletes a branch, discarding unmerged commits)"
}
```

Redeploy (`python hooks/install.py`) and the same command that previously passed now
blocks:

```
$ git branch -D feature/old
BLOCKED (git discipline): git branch -D (force-deletes a branch, discarding unmerged
commits) is not allowed.
Untracked and modified files in the working tree belong to the owner and may be
unrecoverable. ...
```

No guard module was touched. `install.py` also refuses to deploy if the pattern you add
doesn't compile as a regex, so a typo here fails loudly at deploy time, not silently at
run time. The same config-only mechanism (`design_cases` in `config.json`) is how
`brief_gate` enforcement rules — e.g. "a UI-shaped brief must name a design skill" —
are added or removed; see `docs/DESIGN.md` §3 for its two preconditions before you add
one there.

## Overlays: letting a user or project add and disable rules without editing this repo

`config.json` ships with two owner-specific rule lists shipped **empty** on purpose
(`git_policy.deploy_refs`, `command_rules.dev_server`) — a hosting provider's preview-branch
names and a project's dev-server heap flag are not facts about git or about ULTHAR, they're one
owner's convention, and baking them into a shared config would misfire the moment a stranger's
repo used a different branch name or script. The overlay mechanism (`lib/ctx.py:merge_overlays`)
is how you restore or add rules like these without forking this file.

Two overlay files, read once per dispatch, immediately after `config.json` loads:

- **`~/.claude/ulthar-overlay.json`** (`overlays.user_path`) — yours, same as editing
  `config.json` itself — with two qualifications that are easy to state wrong, so here they are
  precisely. **What "replace" applies to:** a *declared* list (anything with a `list_merge`
  strategy, which is every rule list) is **append-only** — you add entries, and the only way one
  leaves is an explicit `disable`. Wholesale replacement is for the *undeclared* lists only, and
  a replacement has to keep the shape the base value already has. A **section** is instead
  **deep-merged** — keys you don't mention survive — and a **scalar** is overridden outright, with
  NO type check beyond the container-kind mirror (`_refuse_container_swap` only refuses a
  container swapping in for a scalar or vice versa, never a scalar of the wrong type replacing
  another scalar). A mistyped scalar — the string `"2MB"` landing where a byte-count config value
  expects an int — merges without complaint and only shows up later, as a guard error or a
  silently stopped log (see `docs/DESIGN.md:276`). **What is
  off-limits:** the four **sections** in `overlays.pinned_paths` — `dispatch`, `grants`, `regex`
  and `overlays`. Nothing in those four is a rule — each decides what a rule *can do*: which
  guards are routed at all, the owner-authority mechanism, the named alternations routing
  resolves through, and the merge itself. Every one of them was measured as a single-overlay
  way to switch the whole mechanism off (see "What owner grants do not buy" below). They are
  refused from either layer, against a rewrite and against a `disable` naming them or anything
  inside them. Everything else stays yours to override, `logging.*` and `paths.*` included, and
  every rule list stays appendable and disableable. Change the four in `config.json`, which no
  overlay can reach.

  The cost is real and worth stating: extending `regex.named_alternations` — adding a new
  edit-style tool name so it routes like `Write` — is a legitimate thing an owner might want
  from an overlay, and it is now refused there. It has to go in `config.json`.

  One consequence of "undeclared lists may be replaced" is worth naming rather than leaving to
  be discovered. `command_rules.redirect_noise_strips` and `quote_strip_patterns` hold no rules;
  they build the *probes* the rules match against (a copy with harmless redirects stripped, and
  one with quoted strings blanked). Replacing them with a shape-valid value therefore changes
  what **ordinary** rules see — which is the owner-permitted equivalent of a `disable`, and in
  the same class as the standing decision that an overlay may switch ordinary rules off. It
  cannot reach the two authority rules: those match `target: raw`, the unmodified command text,
  which no probe transform touches. And it cannot do it by *crashing*, which is what the shape
  check is for.
- **`<cwd>/.claude/ulthar-overlay.json`** (`overlays.project_path`) — the repo's own policy,
  which takes precedence while you work in that repo. It may **add** rules at the paths listed
  in `overlays.project_allowed_paths`, and it may **disable** named ones there:

  ```json
  { "disable": ["git_policy.destroyers/clean", "git_policy.force_push/push-force"] }
  ```

  An identifier is `<dotted config path>/<rule id>` — the same dotted path used by
  `overlays.list_merge`, then the entry's `id` (or, for a plain scalar list, the scalar itself:
  `git_policy.protected_branches/main`). A dotted path never contains `/`, so the first `/`
  splits it and a ref-shaped id (`release/1.0`) stays addressable.

  Yes, that means a cloned repo can switch off rules that were protecting you. That is the
  point: project configuration takes precedence over personal configuration, the way
  `.editorconfig`, linter configs and CI settings all do, and a project that weakens a guard
  deliberately is stating its own policy — not something this tool should overrule.

  It stops short of two rules. `grants.never_exempt` names
  `command_rules.tool_discipline/authority-file-in-shell` and
  `command_rules.tool_discipline/delete-outside-disposable`, and a `disable` naming either
  is refused at config-load time — loudly, from **both** the project and the user overlay,
  the same refusal a grant file naming them already got. Those two are not policy about a
  repository; they are what decides what a repository may do. A repo able to switch off
  `authority-file-in-shell` could then write its own grants, which is the mechanism closing
  over itself — the same argument that already carved out `deploy_guard` and
  `dispatch.events`. If *you* want them off, they are `enabled: false` in `config.json`,
  which is yours and which **no overlay** can reach. That last clause is about overlays and
  nothing else: `config.json` itself is an ordinary file, writable by the Write tool and
  from the shell like any other. What it is protected from is being changed *by a
  configuration file*, not from being changed.

  What the mechanism otherwise insists on is that nothing is weakened **by accident** or
  **invisibly**:

  - **One rule, not one guard.** A `disable` removes exactly the named entry. Its siblings in
    the same list, and every other rule in the same guard, keep enforcing. This is what the
    operation replaces: a malformed entry used to make a guard *raise*, and the dispatcher's
    deliberate fail-open (a guard bug must never brick every tool call) turned that raise into
    "allowed" — removing everything that guard enforced, with nothing in any log to say so.
    Intending to allow `git push --force` also removed `git clean`, `reset --hard` and the
    protected-branch check.
  - **Disable-by-crash is closed, in two steps rather than one.** The merge derives each
    list's shape from `config.json`'s own entries at that path (the keys every entry carries,
    with their types; a list that ships empty declares its shape beside its `list_merge`
    strategy) and refuses anything that does not conform — naming the offending file and
    dotted path. That is the cheap step and it is not complete: it can only refuse what it
    knows the shape of, and four review rounds each found one more thing it did not. So the
    second step is structural — **while any overlay is merged, a guard that raises BLOCKS
    instead of failing open**, naming the overlay file. Whatever shape gets past the first
    step cannot end as a silent ALLOW. With no overlay merged, a guard crash still fails open,
    because then it is unambiguously an ULTHAR bug and bricking every tool call over one is
    the worse failure.
  - **A `disable` that matches nothing is an error**, naming the file, the identifier and the
    ids that *are* there. A typo that silently switched nothing off would be the same invisible
    failure.
  - **You are told.** When a project overlay disables anything, `dispatch.py` logs it to
    `logging.config_error_log` and states it in the session context — the overlay's path and
    every disabled id. Both fire on the **first dispatch that encounters those disables**,
    whatever event that is, and then once per session, not on every tool call. (Gating them on
    `SessionStart` — as this did until 2026-09-06 — meant a session that started outside the
    repository and later entered it, or one whose overlay appeared after startup, obeyed the
    disables from the next tool call while saying nothing anywhere.) "Once" is remembered in a
    marker beside the logs; if that marker cannot be written the notice repeats rather than
    disappearing.
  - **It cannot repoint ULTHAR's own wiring.** `paths.python_exe`, `dispatch.*`,
    `logging.*`, `deploy_guard`, `overlays.*`, `grants.*` and `regex.*` are not in
    `project_allowed_paths` and cannot be added to or disabled. A repo choosing which
    interpreter the hooks run under, which guards are routed, or whether the deployed runtime
    can be edited is not policy about that repo's development — it is the repo rewriting the
    guards that judge it. `deploy_guard` in particular protects the deployed copy of every
    guard, including this restriction — **through the Write and Edit tools; the shell is not
    covered**, so `cp x <deploy dir>/dispatch.py` reaches the runtime that `deploy_guard`
    refuses to let you edit. Four of those sections — `dispatch`, `grants`, `regex`,
    `overlays` — are refused from the **user** overlay too; see `pinned_paths` above.

  The residual, stated plainly: a repo shipping a **broken** overlay can cause a **denial** —
  tool calls in that repo block, loudly, with its path in the message. And a repo shipping a
  **working** one can leave you with fewer rules than your own config gives you. Both are
  visible; neither is silent.

An absent overlay costs two `Path.exists()` calls and changes nothing. A malformed one (bad JSON,
an out-of-allowlist path, a colliding id, a `disable` naming a rule that does not exist) is a loud
`ConfigError` that blocks the whole dispatch, the same as an invalid `config.json` — never a silent
"no rules loaded" degradation.

Two worked examples ship in [`examples/`](examples): `user-overlay.example.json` (both
owner-specific lists mentioned above, restored as a user overlay) and
`project-overlay.example.json` (a project adding a rule of its own and disabling one of
ULTHAR's). Copy the relevant one to the path named above and edit it for your setup.

## Owner grants: permitting one call without turning a rule off

An overlay `disable` is the right tool when a rule is wrong for a repository. It is the
wrong tool when a rule is right and you want *this one call* through anyway — the usual
case being that you, the human, have looked at it and decided. That is a **grant**.

|                           | scope           | lifetime                |
|---------------------------|-----------------|-------------------------|
| project overlay `disable` | that repository | standing                |
| owner grant               | this machine    | consumed on first use   |

Same addressing as `disable`, deliberately: `<dotted config path>/<rule id>`. A grant **is
a file** — an empty one, whose path under `grants.dir` *is* the identifier:

```
~/.claude/state/ulthar-grants/git_policy.force_push/push-force
~/.claude/state/ulthar-grants/command_rules.tool_discipline/cd-compound
~/.claude/state/ulthar-grants/git_policy.protected_branches/main
```

The next matching call from the **main chain** goes through; the file is gone; the call
after it blocks again. A grant never applies to a subagent, whatever it names. It exempts
exactly the rule it names — its siblings in the same list, every other rule in the same
guard, and every guard after it still run, including on the very command it let through.
Every use is logged to `~/.claude/hooks/logs/grants.jsonl` with the rule id and what it
permitted; a grant nobody used is still sitting there, and one that was ignored because a
subagent hit the rule is logged as ignored — so "I wrote a grant and nothing happened" is
always answerable. A grant naming a rule that does not exist blocks the next tool call
with a message naming the file, exactly as a `disable` matching nothing does.

Every block that a grant *could* have waived tells you the path to create, so you never
have to derive the identifier by hand.

**You have to create it yourself, and no agent can name the path to do it for you.** The
grant directory, the delete rule's `pm-delete-allow` ledger and both overlay files are
refused to every agent — commander included — through the Write/Edit tools
(`guards/grant_files.py`) and through the shell
(`command_rules.tool_discipline/authority-file-in-shell`, which blocks `python -c
"open(...)"`, `sed -i`, `cp` and redirects at those paths while leaving ordinary work on
ULTHAR's own source alone). The predecessor of this mechanism — `pm-delete-allow` — lived
in a directory the guards exempt, so the commander could write its own permission and
consume it in the same turn, which is not permission at all.

"Cannot name the path" is the whole of the claim, and the rest of this section is what it
leaves out. An earlier draft said flatly that the agent cannot create these files. Two
findings in the final review showed that was too strong, and a third came out of the sweep
that followed them. All three are fixed rather than merely disclosed:

- The separator class was `[\\/]` — **exactly one character**. So
  `.claude\\state\\ulthar-grants\\x`, which is how a Windows path is spelled inside a
  non-raw Python string and therefore how an agent naturally writes one in a `python -c`
  one-liner, went straight past, as did `//` and a `/./` segment. It is a separator **run**
  now, with `.` segments allowed, and the regression test builds every spelling from the
  config values rather than from literals.
- `grants.never_exempt` was enforced against grant files but **not** against an overlay
  `disable` — the same power with a longer lifetime. A project overlay could switch off
  `authority-file-in-shell` permanently. It is now refused from both layers (see the
  overlay section above), and the list itself is pinned: an overlay that rewrites
  `grants.never_exempt` is refused too, because an overlay able to rewrite the carve-out
  would have the carve-out for free.
- **A user overlay could switch the mechanism off without disabling anything.** The fix
  above pinned one list; a second sweep measured three more paths that were not rules at
  all but decided what rules could do, and each was a one-file switch-off through the real
  dispatcher. `{"dispatch": {"events": {"PreToolUse": []}}}` gave `rc=0` on a command the
  delete rule blocks — every `PreToolUse` guard gone, nothing *disabled*, and therefore no
  operator notice, since that notice covers a project overlay's `disable`.
  `{"grants": {"protected": []}}` let the Write tool create a grant file. And a `disable`
  whose left half is a pinned list (`{"disable": ["grants.never_exempt/<entry>"]}`) reached
  the carve-out itself, through the other door.

  Pinning those four *leaf paths* was the first answer, and the review then measured five
  more the list did not reach — each `rc=0` on a Write of a grant file that is `rc=2` with
  no overlay. `{"disable": ["dispatch.events.PreToolUse/None"]}` named a **child** of a
  pinned path (the routing entries carry no `id`, so the identifier `"None"` matches one)
  and emptied the chain. `{"regex": {"named_alternations": {"write_edit_tools": …}}}`
  unrouted the Write/Edit chain by repointing the *name* it routes through.
  `{"grants": {"enabled": false}}` made `grant_files` return ALLOW at its first line — all
  four protected files writable, the user overlay among them, so the change is permanent.
  `{"grants": {"dir": …}}` moved the grant directory out from under the shell rule's
  literal. And `{"grants": {"protected_message": 5}}` crashed the guard on a `.replace()`
  into the dispatcher's fail-open — a *type*, which shape validation does not cover
  because it validates list entries.

  So the pin is **sections, not paths**: `dispatch`, `grants`, `regex`, `overlays`.
  Enumerating leaves was chasing instances of a class, and the class is "a path that is not
  a rule but decides what a rule can do". Refused from **either** layer, against a rewrite
  and against a `disable` naming them or anything inside them. What an owner gives up is
  doing any of it *from an overlay* — including the one legitimate use in the set,
  extending `regex.named_alternations`; what they keep is doing it in `config.json`, which
  no overlay can reach and which `install.py` re-registers the hooks from. (`config.json` is
  out of reach of an *overlay*; it is not itself protected from an agent — see below.) Every
  rule list stays appendable and disableable from both layers, which was never in question.

  **What kind of bound this is, stated once and plainly.** The pins, the container mirror
  and the merge invariant are an **accident-and-configuration bound**: a well-formed config
  file — a cloned repo's overlay, an owner's slip, a generated file — cannot silently hollow
  out the mechanism. "Silently" is the load-bearing word and it is what the two steps above
  buy jointly: shape validation refuses at merge time what it can see, and anything it cannot
  see ends as a guard crash, which blocks and names the overlay rather than allowing. So the
  outcome is always a refusal you can read, never a session that quietly stopped enforcing.
  They are **not** an
  adversarial bound against an agent that has shell access. The same routes the pins are
  premised on (`cp`, `tar -xf -C`, `git apply`, `git clone`, `python -c "open(...)"`) reach
  the deployed runtime and the master `config.json` directly, and those were measured at
  `rc=0`. A shell-half rule for the deploy directory was considered and deliberately not
  added: it would carry exactly the same destination-directory residue as the authority-file
  rule, so it would move the line without closing it.
- **A rule list, or a whole section, could be replaced by something that is not one.** No
  pinned path involved. `{"command_rules": {"tool_discipline": {"zz": 1}}}` put a dict
  where a rule list lives; `{"command_rules": 0}` — also `null`, `""`, `false`, `{}` — put
  a scalar where the whole section lives. Either way the merge replaced it, every
  `tool_discipline` rule went, `authority-file-in-shell` and `delete-outside-disposable`
  among them — the two rules `grants.never_exempt` says nothing may switch off — and the
  consuming guard's `KeyError` became ALLOW through the dispatcher's fail-open. Measured
  `rc=0` on a shell `touch` of a grant path, on a delete outside the allowlist, and on a
  Write of the `pm-delete-allow` ledger.

  Two defences now, deliberately overlapping, because each of these was found by someone
  trying one more shape. The **container mirror** is complete in both directions: a list
  merges only with a list, a section only with a section, and an empty object expresses
  nothing rather than erasing. Behind it is an **invariant on the merge output** rather
  than on its input — after everything has merged, every rule named in
  `grants.never_exempt` must still be present and every `grants.protected` reference must
  still resolve, through the same resolver the guard itself uses. Measured with each defence
  reverted in turn: the invariant alone catches all four `command_rules` spellings, the
  mirror alone catches all five, and with both gone every one is `rc=0` again.

  The round after found a third way in that neither covers — a list which declares no merge
  strategy, replaced wholesale with the wrong element shape, so the guard raised in its own
  setup before it reached a rule, and the fail-open returned `rc=0` with empty stderr. That
  one is now shape-checked at merge like every other list. But the lesson had repeated often
  enough to stop patching shapes one at a time: **while any overlay is merged, a guard that
  raises blocks rather than allowing.** The shape checks are still worth having — they refuse
  early, precisely, and name the path — but they are the optimisation, and the crash rule is
  the guarantee.

What is still **not** bought, stated plainly rather than implied:

- Nothing authenticates a file's author. Agent and owner share a filesystem and an
  identity; the design removes routes, it does not verify anyone.
- A shell command that assembles the path from pieces or from a variable is not matched by
  any regex over the command text, and widening the pattern cannot fix that.
- A `..` segment (`.claude/state/../state/…`) is not matched by the **shell** rule. Win32
  resolves it to the protected file; the pattern does not, because normalising a path inside
  a command string is a different operation from matching one — the string need not even be a
  path on the machine that runs it. The Write/Edit half *does* block it, because `normpath`
  resolves `..` before the comparison, so this is a shell-only residue.
  (A **trailing-dot or trailing-space** component — `.claude./state/…`, `.claude /state/…` —
  was the same shape and was **not** narrow: Win32 discards both, `normpath` keeps them, so
  the Write/Edit half compared it as a different path and let it through. One Write, no
  overlay, and the agent had written its own grant. Both halves handle it now: the comparison
  strips the padding, and the shell pattern tolerates it.)
- **Windows spells one file many ways**, and the rest of that class was measured the round
  after: `\\?\C:\…`, `\\.\C:\…`, `\\localhost\C$\…`, `\\?\UNC\localhost\C$\…`,
  `<file>::$DATA`, 8.3 short names, volume-GUID and `GLOBALROOT` device paths — every one
  `rc=0` on a Write of a grant path, every one really creating the file. The prefixes and
  the admin share are rewritten before comparison and the `::$DATA` suffix truncated; the
  rest are unreachable by string work, so the containment test gets one `realpath` second
  chance, as an *additional* accept only (it can tighten a verdict, never loosen one). It
  never runs on a UNC path or a missing drive, because `realpath` on an unreachable host
  cost 21.5 s per tool call. **Still open:** a UNC spelling whose share is not an admin
  share, and an 8.3 name for a path that does not exist yet.
- **A command that names only the destination *directory* carries no authority path at
  all**, so this rule cannot see it: `cp tmp/overlay.json .claude/`, `tar -xf x.tar -C
  ~/.claude`, `git apply` on a diff the agent wrote, `git checkout <ref>`, `git stash pop`,
  `git clone`. An agent can therefore *materialise* an overlay or a grant file even though
  it cannot write one by name. What that reaches is bounded **as a configuration file**: a
  materialised overlay may weaken ordinary rules — which a cloned repo may do anyway,
  deliberately — and may **not** switch off the two rules in `grants.never_exempt`, whatever
  shape it is written in.

  The bound stops there, and this is the honest limit of the whole design: those same
  routes do not only land overlay files. `cp x <deploy dir>/dispatch.py`,
  `tar -xf -C <deploy dir>`, `git apply`, `git clone`, `python -c "open('<deploy
  dir>/dispatch.py','w')"` and `cp x <skill>/config.json` all reach the **deployed runtime
  and the master config** — measured `rc=0` — and `deploy_guard`, which exists to protect
  the deployed copy of every guard, is routed on the Write/Edit tools only. So what the
  pins buy is that a *config file* cannot silently hollow out the mechanism; they buy
  nothing against an agent willing to replace the mechanism itself. A materialised **grant**
  file for an ordinary rule is still reachable, and is accepted knowingly:
  `~/.claude/state` is deliberately disposable, so traffic to it cannot be blocked without
  breaking what the state directory is for. Such a grant is still one rule, one call, main
  chain only, and logged. The **user** layer is otherwise deliberately unrestricted — the
  allowed-paths fence applies to the project layer — so a materialised user overlay can
  repoint wiring (`paths.*`, `logging.*`) that a project overlay cannot. That reach is not
  uniform within `paths.*`, though: `paths.error_log` and `paths.exempt_patterns` are read
  from the merged config at guard-run time, so an overlay value for either takes effect, but
  `paths.state_dir`, `paths.skills_root` and `paths.logs_dir` are resolved once in
  `build_ctx`, *before* `merge_overlays` runs, and stored as `Ctx` fields — an overlay
  cannot move them, materialised or not. The exceptions are
  the four sections in `overlays.pinned_paths` — `dispatch`, `grants`, `regex`, `overlays` —
  which no overlay may rewrite or `disable` into, in either layer. `grants.dir` is inside
  one of them, which is why the shell rule can keep the grant directory's name as a literal:
  it cannot drift away from the value the Write half follows.
- An agent that asks you to run a command you don't understand still gets the grant.
- An agent that can edit `~/.claude/settings.json` removes the hooks entirely; nothing
  inside a hook prevents the removal of hooks. The same is true one level down: the
  deployed `hooks/` tree and the master `config.json` are ordinary files. `deploy_guard`
  refuses to let the **Write and Edit tools** touch the deployed copy, and there is no
  shell half — a shell rule for that directory was considered and not added, because it
  would carry the same destination-directory residue as the authority-file rule and would
  read as a guarantee it could not keep.
- The log is written *after* the fact. Visibility is a record, not a decision — which is
  why "let it self-grant but log it loudly" was rejected as the answer here.

The claim is narrower and worth having: through the tools ULTHAR guards, self-granting
stops being the obvious next tool call and becomes deliberate evasion; every route to the
two rules that protect the mechanism itself that has been *found and measured* is closed
from both overlay layers and from a grant file — `disable` (by name, by a child path, or by
shadowing an id), emptying any of the four sections that decide what a rule can do, turning
the grant mechanism off, moving the grant directory, crashing a guard on a wrong scalar type,
replacing a rule list with something that is not a list, and reaching an authority file by a
spelling Win32 normalises away; every one of those was a working one-file switch-off in an
earlier round and is now a refusal with a regression test. A grant can never widen past one
rule, one call, and the main chain; and every use is on the record. That is a list of closed
routes, not a proof that there is no other one — five review rounds have each found one, and
the honest expectation is that a sixth would too. `config.json` and `settings.json` are still
above all of it, as the bullets above say.

One implementation note worth stealing: the consume is **not** `os.remove()`. Two threads
racing `os.remove()` on one path on Windows both report success in about half of 300
measured trials (deleting sets a disposition on a still-open directory entry), which would
have granted two concurrent calls from one file. The winner is decided by
`os.open(..., O_CREAT | O_EXCL)` on a claim marker — exactly one winner in 300/300 — and
only that winner deletes.

## A comment convention you'll see in the code

`# ponytail:` comments (e.g. in `hooks/guards/tool_discipline.py`, `hooks/lib/gitcmd.py`)
mark a deliberate simplification whose ceiling and upgrade path are named in the comment
itself, following the public `ponytail` skill's convention. It's not a private codename —
just a marker for "this cuts a corner on purpose, here's when to stop cutting it."

## Scope

Developed and tested on Windows. The guard code itself is platform-independent (path
handling, regex case-sensitivity and process discovery all branch on `ctx.is_windows`
rather than assuming one OS), but the POSIX process-group / signal path used by the
Stop-hook process guards has not been exercised on a real POSIX machine — only unit
tests with a stubbed process list back that path today.

## Tests

```bash
python hooks/tests/run_all.py --no-parity
```

`--no-parity` skips a comparison harness against an earlier PowerShell implementation
of this runtime that isn't part of this distribution; without it, that section reports
"retired" rather than pass/fail, which is expected. `hooks/tests/` is never deployed —
it lives only in this source tree.

## License

MIT — see [LICENSE](LICENSE).
