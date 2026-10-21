Assembly filesystem searcher.

## Layout

- Sources are per-arch under `src/$(ARCH)/`; the x86-64 source is `src/x86_64/zfind.asm` (NASM).
- Build output goes to `build/$(ARCH)/`, never into the source tree.

## Build

- `nix develop` for the devshell; packages are listed in `flake.nix`.
- Assembler is NASM, linker is `ld`.
- Use the Makefile:

```sh
make           # assemble + link
make run       # build, then run
make clean
make install   # PREFIX ?= ~/.local, DESTDIR-aware
make testdata  # PRESET ?= default; see tools/mktrees.py
```

- `ARCH ?= x86_64`; `ARCH=aarch64` is a stub that errors until ported. A new
  port needs `src/<arch>/zfind.asm` plus an `ifeq` block in the Makefile.
- Extra flags pass through the variables, e.g.
  `make ASFLAGS="-f elf64 -g -DNO_EOF_SHORTCUT"`.
