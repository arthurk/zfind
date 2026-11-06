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

## Tests

```sh
make test-quiet   # builds, then prints only failures and a one-line summary
make test         # one line per test (pytest.ini sets -vv)
```

- Use `make test-quiet` by default; do not re-read passing output.
- Run a subset the same way: `make test-quiet T="tests/test_limit.py -k none"`.
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
- Each shell call is a fresh shell: set variables and use them in the same call.
- End experiments with `| head` or `| wc -l`, and write their output files
  outside the tree zfind searches.

## Commits and issues

- Commit only when asked. Then create many small commits rather than one
  large: one logical change per commit, for example the assembly change, its
  tests and its docs (README, help text) as separate commits.
- Subject lines start with the area: `zfind: ...`, `tests: ...`, `docs: ...`,
  `tools: ...`.
- Read an issue with `gh issue view N`, not by fetching its web page, which is
  mostly navigation.
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
make           # assemble + link
make run       # build, then run
make clean
make test-quiet  # build + pytest, failures and summary only
make test        # build + pytest, one line per test
make testdata  # PRESET ?= default; see tests/mktrees.py
```

- `ARCH ?= x86_64`; `ARCH=aarch64` is a stub that errors until ported. A new
  port needs `src/<arch>/zfind.asm` plus an `ifeq` block in the Makefile.
- Extra flags pass through the variables, e.g.
  `make ASFLAGS="-f elf64 -g -DNO_EOF_SHORTCUT"`.
