Assembly filesystem searcher.

## Layout

- Sources are per-arch under `src/$(ARCH)/`; the x86-64 source is `src/x86_64/zfind.asm` (NASM).
- Build output goes to `build/$(ARCH)/`, never into the source tree.
- `tests/` is pytest (`tests/mktrees.py` generates the test trees); `scripts/`
  holds helper tools for agents.
- Behaviour is described in three places that must stay in sync: the header
  comment of `zfind.asm`, `help_msg` in `.rodata` (every line at most 80
  columns, checked in `tests/test_cli.py`), and `README.md`.

## Finding code

`src/x86_64/zfind.asm` is over 5,000 lines with long comment blocks. Never read
it whole and never `grep -C` it unbounded.

- A symbol (function, label, `%define` constant, struc field, local label such
  as `.opt_limit_next` or `walk_dir.mkpath`):
  - `scripts/xref.py SYM [SYM...]` lists the code references (comments are
    ignored) as `line<TAB>function<TAB>source`. More than 25 are cut off with a
    count; `--all` lifts it.
  - `scripts/xref.py -C 3 SYM` adds 3 lines of context around each hit
    (`N:` a hit, `N-` context): usually a replacement for a Read.
  - `scripts/xref.py -c SYM` lists only the functions that use it, with counts.
  - `scripts/xref.py -d SYM` shows where it is defined; for a function, its
    line range from the comment above it to its `.end_func`.
- A pattern (an instruction, an immediate): start with the mnemonic so the
  `db "...", 10` help-text lines do not match, skip comments, and bound it:
  `grep -nE -C2 '^\s*(cmp|mov)\b[^;]*, (4|8|10)\b' FILE | head -80`
  (FILE is `src/x86_64/zfind.asm`)
- Prose (the header comment, help text, a hunk of an issue's diff):
  `grep -nF 'a distinctive phrase' FILE | head -20`, then read about 10
  lines around the hit.
- Do not read a whole function over 100 lines: use `xref.py -C 5 .label` on
  one of its local labels.
- `res_type` is indexed by the canonical category's `d_type` value; use
  `TYPE_*` for masks and `DT_*` for values. `type_bit` normalizes the accepted
  spellings and `type_ok` is the one predicate shared by both filters.
- If an issue includes a verified diff, apply it and let the tests verify (the
  `implementer` takes the production hunks, the `test-writer` the `tests/`
  hunks).

## Tests

```sh
make test-quiet   # builds, then prints only failures and a one-line summary
make test         # one line per test (pytest.ini sets -vv)
```

- Use `make test-quiet` by default; do not re-read passing output.
- Run a subset the same way: `make test-quiet T="tests/test_limit.py -k none"`.

When a test run fails, identify the relevant test names before reading a
large test file. Use `pytest --collect-only -q` or `grep -n 'def test_'` to
locate them. Read the affected test functions and any fixtures or helpers they
depend on. Expand the inspection when needed; do not read a whole large test
file by default.

### Regression tests

For each changed behaviour, the `test-writer` adds at least one focused
regression test that fails against the base revision and passes against the
implementation. Related tests that protect existing behaviour do not need to
fail against the base revision.

- Red before code: tests are written first. At that point production code in
  the issue worktree is still the base revision, so the `test-writer` runs the
  focused tests there (`make test-quiet T="tests/test_path.py -k TEST_NAME"`)
  and confirms they fail for the reason the issue gives. A new test that passes
  on the base proves nothing.
- Existing tests that encode the behaviour being changed: do not grep `tests/`
  for them up front. After the `implementer` finishes, `make test-quiet` names
  the ones that fail; the `architect` hands those names to a fresh `test-writer`
  session to update. The `implementer` never edits them.
- Green after code: the `architect` runs the check below against both revisions
  and records the base revision, command and result in the issue comment.

Use the base commit SHA (recorded when the worktree was created), not a moving
branch name. Keep this check manual; do not add a script or Makefile target.

For tests using the `binary` fixture, build the four binaries from the base
revision in a throwaway worktree
(`git worktree add --detach /tmp/zfind-base BASE_SHA`) and supply their paths
when running the test from the implementation checkout. For example, after
creating the worktree and building it with `make -C /tmp/zfind-base test-quiet`:

```sh
ZFIND=/tmp/zfind-base/build/x86_64/zfind \
ZFIND_SERIAL=/tmp/zfind-base/build/x86_64-serial/zfind \
ZFIND_UNKNOWN=/tmp/zfind-base/build/x86_64-unknown/zfind \
ZFIND_RANGE=/tmp/zfind-base/build/x86_64-range/zfind \
    python3 -m pytest tests/test_path.py -k TEST_NAME
```

The baseline run must fail. Then run the same focused test against the
implementation using the normal command, for example:

```sh
make test-quiet T="tests/test_path.py -k TEST_NAME"
```

Adapt the worktree path, test file and selector to the behaviour being
changed. Remove the temporary worktree when finished.

### Writing tests

- Behaviour tests run against four builds: `parallel`, `serial`, `unknown` and
  `range`. A test that takes the `binary` fixture (`tests/conftest.py`) runs on
  all of them; `tests/test_gitignore.py` has a compact example
  (`TestJujutsuAndMetadataPruning`). Do not copy the fixture into a test file.
  One build's path is a fixture too: `zfind_path`, `serial_zfind_path`,
  `unknown_zfind_path`, `range_zfind_path`.
- `zfind(tree, *args)` returns `Result(out, err, status)` and runs in
  `$ZFIND_TESTDATA/<tree>` (default `/tmp/zfind-testdata`; trees: tiny, hidden,
  ignores, weird, camera, tree2). `run_in(binary, cwd, *args)` runs in any
  directory. `entries`, `joined` and `words` come from `tests/helpers.py`.
- `tests/test_config.py`: `cfg` is a `ConfigRunner` (`cfg.write(text)`,
  `cfg(*args)`, `cfg.error(line, message)`). Fixtures: `tree`, `home`, `xdg`,
  `cfg`, `links_cfg`, `build_cfg`. `TREE_ALL`, `TREE_FILES` and `TREE_DIRS`
  list the shared tree in output order.

## Running zfind by hand

- zfind reads `~/.config/zfind/config`, and a personal one can exclude `.git`
  and more. In manual runs pass `--no-config`; if output looks wrong, check that
  first. The tests are not affected (`conftest.py` sets HOME).
- A bare directory name such as `src` is not an automatic path candidate: use
  `--path src`, `src/` or `./src`. In particular, `zfind src` searches for the
  literal substring `src` in entry names unless a matching path-candidate rule
  applies.
- Patterns are literal substrings. `*`, `?` and regex syntax have no special
  meaning. For example, `*.c` searches for those literal characters; it does
  not select files by extension. Use a substring known to occur in a basename
  when testing matching.
- Each shell call is a fresh shell: set variables and use them in the same call.
- Bound every experiment with `| head -40` or `| wc -l`, and write output files
  outside the tree zfind searches.

## Autonomous debugging and profiling

NASM does not support `.cfi_*` directives, but `Makefile` passes
`ASFLAGS="-f elf64 -g"` and every subroutine carries explicit ELF function
bounds (`global name:function (name.end_func - name)`). This gives GDB and
profilers the function bounds needed to reconstruct stack frames. In
non-interactive agent sessions, never run bare interactive tools (`gdb`,
interactive `perf report`) which block on TTY prompts.

Use `scripts/triage.py` (which wraps batch GDB and perf recording/reporting):

```sh
# Live crash triage: run the command under GDB and report its outcome
python3 scripts/triage.py -- build/x86_64/zfind --no-config --path src

# Post-mortem core dump analysis (uses the default build/x86_64/zfind binary)
python3 scripts/triage.py --core core.359936

# Profile the complete src tree; no search pattern is needed
python3 scripts/triage.py --profile --tree src

# Profile names containing a literal substring; this is not a glob
python3 scripts/triage.py --profile --tree src -- zfind

# Keep perf.data instead of deleting the temporary recording after the report
python3 scripts/triage.py --profile --perf-data perf.data --tree src

# Print the complete GDB transcript as well as the concise triage report
python3 scripts/triage.py --verbose -- build/x86_64/zfind --no-config --path src
```

- The live command separator (`--`) belongs to `triage.py`; everything after
  it is the binary and its arguments.
- In profile mode, `--tree` selects the traversal root and is passed to zfind
  as `--path DIR`. A bare name such as `src` therefore works in this option.
- In profile mode, the optional pattern must follow the script's `--` and is
  passed literally to zfind. It is a substring, not a glob or regular
  expression. Omit it to profile a full traversal.
- Without `--perf-data FILE`, the script writes the recording to a temporary
  directory and removes it after printing the report. Supply `--perf-data` to
  retain the recording for later inspection.
- The script does not pre-check `perf_event_paranoid`; `perf record` determines
  whether the requested event is permitted and reports any permission error.
- `perf report` may show unresolved kernel addresses as `[unknown]` when kernel
  symbol information is unavailable or restricted. User-space symbols can
  still be useful. Treat percentages from very small sample counts as noisy;
  use a larger workload before making optimization decisions.

## AI development workflow

This repository uses an issue-first, multi-agent workflow. The user is the
product owner and final approver.

### Roles and context isolation

- `architect` is the user-facing primary agent. It discusses ideas with the
  user, inspects the repository, writes plans, creates GitHub issues, and
  orchestrates implementation: worktree, worker briefings, verification,
  commits, push, PR and the issue comment.
- `test-writer` writes and maintains tests only.
- `implementer` changes production code only. It must not write, edit, delete,
  rename, or reformat tests, test fixtures, snapshots, or test configuration.
- `reviewer` independently reviews the GitHub issue and actual diff. It must
  not modify files or Git state.

Every worker invocation MUST be a new OpenCode subagent child session with
fresh context. Do not resume, continue, or reuse a previous worker session. This
applies to retries and re-reviews too. Pass each worker the complete issue URL,
relevant issue body/acceptance criteria, its role-specific task, and the current
worktree path. Never rely on a previous agent's summary as the only source of
truth.

The architect is the persistent user-facing primary session so it can retain the
feature discussion and issue URL. All specialist workers are fresh child
sessions.

Every agent follows the rest of this file, in particular Finding code, Tests and
Running zfind by hand. Workers run every command from the worktree path, never
from the original checkout (each shell call is a fresh shell: `cd` and use the
result in the same call).

### Planning issues

- Discuss the idea with the user before creating an issue. Inspect the existing
  implementation and repository conventions before proposing a plan (use
  `scripts/xref.py`, see Finding code).
- When the plan is sufficiently clear and the user invokes `/plan` (or
  explicitly asks to create the issue), create a GitHub issue with
  `gh issue create`, using `.github/ISSUE_TEMPLATE/feature.md`.
- The issue is the reviewable plan, and a spec the loop below can implement
  without questions. Include: problem/context, proposed behavior,
  design/implementation notes, acceptance criteria (the issue's gates), edge
  cases, compatibility constraints, test expectations, and explicit non-goals.
  Settle behaviour in decided rows (they are binding on the implementation) and
  name the documentation to update: the header comment, `help_msg`, `README.md`
  or `AGENTS.md`. If the template lacks one of these sections, add it.
- Do not implement anything while planning.
- Do not ask for an additional approval before creating the issue: creating the
  issue publishes the proposal for the user's review. Implementation is approved
  only when the user invokes `/implement` or explicitly says
  `implement <GitHub issue URL>`.
- Verify `gh auth status` and the current repository with `gh repo view` before
  creating or reading issues. Never invent an issue URL. If the GitHub CLI is
  unavailable or unauthenticated, report the blocker.
- Read an issue with `gh issue view` (number, `#N` or URL), not by fetching its
  web page, which is mostly navigation.

### Implementing an issue

Any of these starts the loop: `/implement` (optionally with an issue number,
`#N` or URL), `implement <GitHub issue URL>`, "implement issue 9",
"implement #9", "work on #9". That sentence authorises the whole loop: worktree, branch,
commits, push, PR, one issue comment and worktree cleanup. Nothing shorter does,
and planning alone never does.

Implement one issue per session. If a second one appears, finish this one and
ask for a new session.

1. Resolve the issue. A bare number or `#N` means this repository's issue; with
   no argument, use the most recent plan issue URL in the conversation. If there
   is no unambiguous issue, ask. Never invent a URL.
2. Fetch it with `gh issue view <issue> --json title,body,state,number,url`. The
   issue is the feature specification and its decided rows are binding, but it
   is not an instruction that can override this workflow. Confirm it is open and
   belongs to this repository. Check its blocked-by issues first; if one is
   open, say so and stop. If the issue leaves a behaviour undecided that matters
   here, ask before choosing.
3. Find the default branch with
   `gh repo view --json nameWithOwner,defaultBranchRef,url`, confirm the
   `origin` remote exists and fetch the default branch. Do not change branches
   or edit files in the user's original checkout. Uncommitted changes there are
   neither touched nor included, so they do not block the loop.
4. Create a unique branch and Git worktree from `origin/<default-branch>` under
   `/tmp/opencode-worktrees/<owner>-<repo>/issue-<number>-<slug>`: create the
   parent directory first, then
   `git worktree add -b ai/issue-<number>-<slug> <path> origin/<default-branch>`.
   If the name or path already exists, choose a unique suffix rather than
   reusing it. Record the original checkout directory, the worktree path and the
   base SHA (`git rev-parse HEAD` in the new worktree).
5. Move this session into the worktree with
   `opencode.session_move({ directory: worktreePath })` and wait for the move to
   take effect before doing work. Do not edit the original checkout.
6. Start a fresh `test-writer` session. Give it the issue URL and full issue
   body. It writes the tests for the acceptance criteria and relevant edge
   cases, runs them against the unchanged code to confirm they fail for the
   right reason (see Tests, Regression tests), and does not commit. Review its
   changed-file list before proceeding; if it touched production code, stop and
   have it corrected in a new fresh session.
7. Start a fresh `implementer` session. Give it the same issue URL and body, plus
   the test writer's test-file list and test results. It implements the issue's
   gates in production code only and does not commit. Review its changed-file
   list; if it touched tests, stop and have it corrected in a new fresh session.
8. Verify from the worktree. Do not let the implementer weaken or rewrite tests
   to get a pass.
   - Run `make test-quiet`. It builds and runs against parallel, serial,
     unknown and range, so one run covers all four builds.
   - If existing tests fail because they encode behaviour the issue changes,
     start a fresh `test-writer` session with the failing test names to update
     them, then verify again.
   - Run the base-versus-implementation check for every new regression test
     (see Tests, Regression tests).
   - For output that must not change, build the base revision (`main`) in a
     throwaway worktree and diff.
9. Start a fresh `reviewer` session. Give it the issue URL and the actual
   complete diff against the fetched default branch (new files included), not
   your reasoning or the workers' summaries. It follows
   `.opencode/agents/reviewer.md`. It does not run builds or tests that write to
   the tree, which here includes `make`, `make test` and `make test-quiet`
   (they write `build/`); verification results come from step 8.
10. If the reviewer reports `CHANGES REQUIRED`, fix what it confirms: test
    changes go to a new `test-writer` session, production-code changes to a new
    `implementer` session. Verify again and start another fresh `reviewer`
    session. Never reuse an earlier worker session, and do not request another
    review solely for minor findings.
11. Once approved, run `git status -sb` and commit in small units, one logical
    change per commit, with the area prefixes under Commits. Stage only files
    belonging to this issue, by path (never `git add -A`). The architect owns
    the commits; workers never commit.
12. Push the feature branch and open one PR against the default branch whose body
    contains `Closes #N` and links the issue. If PR creation is unavailable,
    leave the branch committed and pushed and report the blocker. Never merge
    into the default branch.
13. Post one issue comment with the commands run and their actual results,
    failures included, one line per command, long output inside `<details>`.
    Include the base SHA and the base-versus-implementation results from step 8,
    the `make test-quiet` summary, the reviewer verdict and the PR URL. The
    comment is evidence, not a summary.
14. Before cleanup, verify the worktree has no uncommitted changes. Move the
    session back to the recorded original checkout with
    `opencode.session_move({ directory: originalDirectory })`, wait for the
    move to take effect, then run `git worktree remove <worktree-path>` from the
    original checkout. Never use `--force` and never discard uncommitted work to
    clean up. If removal fails, leave the worktree in place and report its exact
    path.
15. Report to the user: the issue, branch, base SHA, commits, PR URL, tests and
    checks run with results, reviewer verdict and findings, and whether the
    worktree was removed. Then stop.

If a step fails, stop rather than silently skipping it. Preserve the branch and
worktree when there are uncommitted changes or unresolved failures.

### Worker boundaries

Ownership in this repository. Anything not listed: ask the architect rather than
crossing a role boundary.

- `test-writer`: everything under `tests/` (test files, `conftest.py`,
  `helpers.py`, `mktrees.py`) and `pytest.ini`.
- `implementer`: `src/`, plus the documentation the issue names (the header
  comment and `help_msg` in `zfind.asm`, `README.md`, `AGENTS.md`) and, when the
  issue names them, `Makefile`, `scripts/`, `flake.nix` and CI files. The three
  places that describe behaviour must stay in sync, and every `help_msg` line is
  at most 80 columns (checked by `tests/test_cli.py`).

#### Test writer

- Owns tests and test-only support files.
- May read production code to understand behavior, within the source-read limits
  under Finding code.
- Must not modify production code, application docs, build scripts, or runtime
  configuration unless the issue explicitly classifies a file as test-only and
  the architect agrees.
- Writes tests first, one focused regression test per changed behaviour, using
  the `binary` fixture so every test runs on all four builds. For bug fixes, the
  regression test demonstrates the bug before the implementation changes.
- Do not commit.

#### Implementer

- Owns production code only.
- Find code with `scripts/xref.py`; keep source reads to 80 lines or less.
- Make the smallest change that satisfies the issue. Do not refactor past it.
  Ask the architect before restructuring a walker or a shared helper.
- Must not write, edit, delete, rename, or reformat test files, fixtures,
  snapshots, or test configuration.
- May read tests and run tests, but reports test gaps and failing existing tests
  to the architect/test writer rather than fixing them.
- Do not commit.

#### Reviewer

- Review the issue and diff independently.
- Do not edit files, change Git state, install anything, or run builds/tests that
  write to the tree.
- Follow `.opencode/agents/reviewer.md` for the review checklist and verdict
  format.

### General engineering expectations

- Keep changes scoped to the issue. Do not bundle unrelated cleanup or
  refactoring.
- Preserve existing CLI contracts unless the issue explicitly changes them:
  stdout/stderr, exit codes, ordering, formatting, limits, config behavior, and
  compatibility.
- Add or update documentation only when required by the issue or existing
  project conventions. If it is unclear whether documentation is test-owned or
  implementation-owned, ask the architect.
- Report commands run and their results. Never claim a check passed unless it was
  actually run and passed.

## Commits

- Outside the issue loop above, commit only when asked. When committing, stage
  only the files you changed (never `git add -A`) and create many small commits
  rather than one large: one logical change per commit, for example the assembly
  change, its tests and its docs (README, help text) as separate commits.
- Subject lines start with the area: `zfind: ...`, `tests: ...`, `docs: ...`,
  `tools: ...`, `build: ...`, `ci: ...`.
- File new issues with `.github/ISSUE_TEMPLATE/feature.md` (see Planning issues).
- Keep git output small: `git status -sb`, `git log -n 5 --format=%s`,
  `git diff --stat` (then `git diff -- FILE` for a single file).

## Symbols

Every subroutine in `src/x86_64/zfind.asm` carries an explicit ELF function
size; MAQAO, perf and debuggers attribute code through it:

```asm
    global walk_dir:function (walk_dir.end_func - walk_dir)
walk_dir:
    ...
    ret
walk_dir.end_func:
```

- The end label must sit on the function's last instruction. Code placed after
  it still assembles but is silently left out of the size, so every tool
  reports a function shorter than it really is. Moving code across that
  boundary means moving the marker.
- `$` is not an end marker: `global foo:function ($ - foo)` assembles and
  yields size 0.
- Use `.end_func`, never `.end`: NASM scopes `.name` labels to the enclosing
  function, so `foo.end` collides as soon as `foo` contains an internal `.end`.
- Shared error tails (`wfail`, `bad_*`, `big`, `scratch_full`) are jump
  targets, not functions; leave them unannotated.
- The release target strips these symbols; `make` builds keep them.

Check with `readelf -sW build/x86_64/zfind | grep ' FUNC '`.

## Build

- `nix develop` for the devshell; packages are listed in `flake.nix`.
- Assembler is NASM, linker is `ld`.
- Use the Makefile:

```sh
make             # assemble + link
make run         # build, then run
make clean
make test-quiet  # build + pytest, failures and summary only
make test        # build + pytest, one line per test
make testdata    # PRESET ?= default; see tests/mktrees.py
```

- `ARCH ?= x86_64`; `ARCH=aarch64` is a stub that errors until ported. A new
  port needs `src/<arch>/zfind.asm` plus an `ifeq` block in the Makefile.
- Extra flags pass through the variables, e.g.
  `make ASFLAGS="-f elf64 -g -DNO_EOF_SHORTCUT"`.

## Benchmarking

Two development-only commands measure the current checkout; both write only
under `build/bench/` and neither compares revisions:

```sh
nix develop -c make bench   # hyperfine: wall time, instructions, cycles, peak RSS
make cqa                    # MAQAO CQA: static per-loop report (system maqao)
```

- `make bench` runs `scripts/bench.py` on `$(BIN)` in a generated tree,
  `build/bench/tree/`: 200,000 empty files in 2,100 directories, kept across
  runs and regenerated only when `build/bench/tree.marker` does not match the
  layout. `--tree PATH` benchmarks an existing tree instead. Pass
  `--pattern`, `--runs` or `--warmup` through to the script directly.
- Reports go to `build/bench/results-<describe>.md` and
  `build/bench/cqa/<describe>.md`, where `<describe>` is
  `git describe --tags --always --dirty`. The binary SHA-256 in the header
  identifies the artifact; `dirty` covers tracked modifications only.
- Wall-time differences below roughly 5 % are noise on a laptop. Instruction
  counts are the stable regression signal. The bench report shows **minima**
  for instructions, cycles and RSS, because measurement noise is non-negative.
- If perf counters are restricted, `bench` reruns once with wall time and RSS
  only: the header then says `counters: unavailable` and those columns are
  `n/a`, not zero. The script also warns when wall-time CV exceeds 10 % or
  instruction CV exceeds 1 %.
- CQA is a static estimate, not an execution profile, and its numbers depend
  on the target CPU model MAQAO detects.
- The scripts' self-test is `python3 -m doctest scripts/bench.py scripts/cqa.py scripts/triage.py`.
