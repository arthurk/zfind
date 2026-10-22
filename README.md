# zfind

Finds files and directories.

## Features

- Multi-threaded
- Skips hidden files; honors `.gitignore` only with `--gitignore`
  (the default searches everything else)

## Usage

```
zfind PATTERN                       # smart-case search
zfind -0 PATTERN | xargs -0 ...     # NUL-separated output
zfind --limit 100 PATTERN           # first 100 results, then stop
zfind --gitignore PATTERN           # apply .gitignore when cwd contains .git
zfind -- -abc                       # search for a name starting with '-'
```

## Building

```sh
make            # build into build/x86_64/
make run        # build, then run
make install    # install to ~/.local/bin (PREFIX=, DESTDIR= overridable)
```

Sources live per architecture under `src/x86_64/`; the build is driven by a
single `ARCH` variable, so `make ARCH=...` selects another port once one
exists. For the toolchain use `nix develop`.

## Development

Generate test and benchmark trees:

```sh
python3 tools/mktrees.py                 # Standard workloads
python3 tools/mktrees.py --preset smoke  # Quick checks
python3 tools/mktrees.py --preset stress # Large workloads
```

Existing trees are left unchanged. Delete `testdata/` before regenerating with a different preset.

## Requirements

- x86-64-v3 compatible CPU
