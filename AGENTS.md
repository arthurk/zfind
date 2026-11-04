Assembly filesystem searcher.

## Layout

- Sources are per-arch under `src/$(ARCH)/`; the x86-64 source is `src/x86_64/zfind.asm` (NASM).
- Build output goes to `build/$(ARCH)/`, never into the source tree.
- `tests/` is pytest, `scripts/` holds helper tools for agents, and
  `tools/mktrees.py` generates test data.
- Behaviour is described in three places that must stay in sync: the header
  comment of `zfind.asm`, `help_msg` in `.rodata` (every line at most 80
  columns, checked in `tests/test_cli.py`), and `README.md`.

## Reading the source

`src/x86_64/zfind.asm` is over 5,000 lines with long comment blocks. Never read
it whole and never `grep -C` it. Look things up with `scripts/xref.py`, then
read only the line range you need.

- `scripts/xref.py SYMBOL` lists the *code* references to a symbol (comments are
  ignored), one per line as `line<TAB>function<TAB>source`. The line whose
  source is `SYMBOL:` is the definition. It works for functions, data labels,
  `%define` constants and local labels (`walk_dir.mkpath`). The file defaults to
  `src/x86_64/zfind.asm`.
- `scripts/xref.py -c SYMBOL` lists only the functions that use it, with counts.
- `scripts/xref.py NAME.end_func` gives the last line of function `NAME`; the
  function spans its definition line through that line.
- No hits (exit status 1) can mean the symbol only appears in comments. Then use
  `grep -n SYMBOL src/x86_64/zfind.asm | head -20`.
- Read the largest functions (`walk_dir`, `_start`, `parse_args`) in pieces.
- To find everything a change touches, start from the symbol: for example
  `scripts/xref.py git_repo` shows where the repository flag is set and used.

## Tests

```sh
make test-quiet   # builds, then prints only failures and a one-line summary
make test         # same, but one line per test (pytest.ini sets -vv)
```

- Use `make test-quiet` by default; do not re-read passing output.
- Behaviour tests run against four builds: `parallel`, `serial`, `unknown` and
  `range` (fixtures `zfind_path`, `serial_zfind_path`, `unknown_zfind_path`,
  `range_zfind_path`). New tests should cover all four;
  `tests/test_gitignore.py` has a compact example
  (`TestJujutsuAndMetadataPruning`).
- Shared helpers (`run_in`, `entries`, `words`, `ZfindRunner`) are imported from
  `helpers`.

## Commits

- Use the `commit-work` skill
- Create many small commits rather than one large. One logical change per
  commit: for example the assembly change, its tests and its docs (README, help
  text) as separate commits.
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
make test        # build + pytest, verbose
make testdata  # PRESET ?= default; see tools/mktrees.py
```

- `ARCH ?= x86_64`; `ARCH=aarch64` is a stub that errors until ported. A new
  port needs `src/<arch>/zfind.asm` plus an `ifeq` block in the Makefile.
- Extra flags pass through the variables, e.g.
  `make ASFLAGS="-f elf64 -g -DNO_EOF_SHORTCUT"`.
