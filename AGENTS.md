Assembly filesystem searcher.

## Layout

- Sources are per-arch under `src/$(ARCH)/`; the x86-64 source is `src/x86_64/zfind.asm` (NASM).
- Build output goes to `build/$(ARCH)/`, never into the source tree.
- `tests/` is pytest (`tests/mktrees.py` generates the test trees); `scripts/`
  holds helper tools for agents.
- Behaviour is described in three places that must stay in sync: the header
  comment of `zfind.asm`, `help_msg` in `.rodata` (every line at most 80
  columns, checked in `tests/test_cli.py`), and `README.md`.

## Reading the source

`src/x86_64/zfind.asm` is over 5,000 lines with long comment blocks. Never read
it whole and never `grep -C` it. Look things up with `scripts/xref.py`, then
read a small window: set the `limit` of a read to 80 lines or less.

- `scripts/xref.py SYMBOL` lists the *code* references to a symbol (comments are
  ignored), one per line as `line<TAB>function<TAB>source`. The line whose
  source is `SYMBOL:` is the definition. It works for functions, data labels,
  `%define` constants, struc fields (`TSTATE.tab_ptr`) and local labels, either
  as written (`.opt_limit_next`) or qualified as nm and objdump print them
  (`walk_dir.mkpath`). The file defaults to `src/x86_64/zfind.asm`.
- More than 25 hits are cut off with a count. Use `-c SYMBOL` for just the
  functions that use it, with counts, or `--all` for every line.
- `scripts/xref.py -d SYMBOL` shows where it is defined. For a function it gives
  the line range, from the comment above it to its `.end_func` label:
  `NAME: lines FIRST-LAST (N lines); label at LINE`.
- Do not read a whole function that is over 100 lines: find the part you need
  through one of its local labels, then read around it.
- xref skips comments. For prose (the header comment, help text), or to locate
  a hunk of an issue's diff, grep a distinctive phrase from it:
  `grep -nF 'which add to it' src/x86_64/zfind.asm | head -20`. Then read about
  10 lines around the hit.
- If an issue includes a verified diff, apply it and let the tests verify; do
  not re-derive what it already states.
- `res_type` is indexed by `d_type`; use `TYPE_*` for masks and `DT_*` for
  values.

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

For each changed behaviour, add at least one focused regression test that
fails against the base revision and passes against the implementation. Run
the test against both revisions using the appropriate binaries. Related
tests that protect existing behaviour do not need to fail against the base
revision. Record the base revision, command and result in the issue comment.

Use the base commit SHA, not a moving branch name. Keep this check manual; do
not add a script or Makefile target.

For tests using the `binary` fixture, build the four binaries from the base
revision in a throwaway worktree and supply their paths when running the test
from the implementation checkout. For example, after creating the worktree
and building it with `make -C /tmp/zfind-base test-quiet`:

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

- For a behaviour change, change the code first, run `make test-quiet`, and
  update the tests it fails. Do not grep `tests/` up front.
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
- End experiments with `| head` or `| wc -l`, and write their output files
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

## Implementing an issue

All of these start the same loop: "implement issue 9", "implement #9",
"implement https://github.com/arthurk/zfind/issues/9", "work on #9".

That sentence authorises the whole loop: branch, commits, push, PR and one
issue comment. Nothing shorter does.

Before step 1, start from a clean tree: `git status -sb`. If unrelated changes
are present, stop and ask; never mix them into the issue. Branch with
`git switch -c issue-N-slug`.

1. Read the issue with `gh issue view` (it accepts a number, `#N` or a URL).
   The issue is the spec; its decided rows are binding. Check its blocked-by
   issues first; if one is open, say so and stop. If the issue leaves a
   behaviour undecided that matters here, ask before choosing.
2. One issue per session. If a second one appears, finish this one and ask
   for a new session.
3. Find the code with `scripts/xref.py`; keep source reads to 80 lines or less.
4. Make the smallest change that satisfies the issue. Do not refactor past
   it. Ask before restructuring a walker or a shared helper.
5. Implement the issue's gates. For a behaviour change, change the code first,
   run `make test-quiet`, then add or extend the tests the gates name. Update
   the documentation the issue names: the header comment, `help_msg`,
   `README.md`, or `AGENTS.md`.
6. All four builds matter: `make test` and `make test-quiet` build and run
   against parallel, serial, unknown and range, so one run covers all four.
   For output that must not change, build `main` in a throwaway worktree and
   diff.
7. Give the `reviewer` subagent the issue and the diff, not your reasoning.
   Fix what it confirms.
8. Stage only the files you changed (never `git add -A`), commit in small
   units (`zfind:`, `tests:`, `docs:`, `tools:`, `build:`, `ci:`), push, and
   open one PR whose body contains `Closes #N`. Check CI before the issue
   comment.
9. Finish with one issue comment: the commands run and their actual results,
   failures included, with long output inside `<details>`. Then stop.

The comment is evidence, not a summary: one line per command and its result.

Do not create `PLAN.md`, `IMPLEMENTATION.md` or `JOURNAL.md`.

## Commits and issues

- Outside the issue loop above, commit only when asked. When committing,
  stage only the files you changed (never `git add -A`) and create many small
  commits rather than one large: one logical change per commit, for example
  the assembly change, its tests and its docs (README, help text) as separate
  commits.
- Subject lines start with the area: `zfind: ...`, `tests: ...`, `docs: ...`,
  `tools: ...`, `build: ...`, `ci: ...`.
- Read an issue with `gh issue view` (number, `#N` or URL), not by fetching
  its web page, which is mostly navigation.
- File new issues with `.github/ISSUE_TEMPLATE/feature.md`. A good issue is a
  spec the loop above can implement without questions.
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
