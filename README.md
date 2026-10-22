# zfind

`zfind` searches file and directory names under the current directory. It searches recursively and in parallel.

## Usage

```sh
zfind [OPTIONS] [--] [PATTERN]
```

`PATTERN` is optional. When provided, zfind finds entries whose names contain that literal text. Regex and glob patterns are not supported. Without a pattern, it lists every entry.

```sh
zfind config                # Find names containing "config"
zfind --no-hidden TODO      # Find names containing "TODO" and skip hidden entries
zfind --gitignore Cargo     # Find names containing "Cargo" and apply supported .gitignore rules
zfind -0 --limit 100 .conf  # Print up to 100 matches, separated by NUL bytes
```

## Matching and output

- Matching ignores case unless the pattern contains an uppercase ASCII letter.
- Hidden entries, including `.git/`, are searched by default. `--no-hidden` skips them and their contents.
- Results are sorted by name within each directory. Output follows depth-first order.
- Paths are relative to the current directory. Directory results end with `/`.
- zfind does not follow symlinked directories.

## Options

- `-h, --help` Show help and exit.
- `-0, --null` Separate results with NUL instead of newline. Use this when filenames may contain newlines.
- `--gitignore` Apply supported `.gitignore` rules. See [Gitignore support](#gitignore-support).
- `--no-hidden` Skip names that begin with `.`, including hidden directories and everything inside them.
- `--limit N`, `--limit=N` Print at most `N` results. `0` prints nothing. Use `-0` to count entries correctly when filenames may contain newlines.
- `--` End option parsing. Put it before a pattern that starts with `-`, as in `zfind -- -draft` to search for the name substring `-draft`.

## Gitignore support

`--gitignore` works only when the current directory contains `.git`. zfind does not search parent directories for a repository. It reads `.gitignore` files as it walks the tree. Rules in deeper directories take precedence over inherited rules.

Supported syntax includes negation (`!`), `*`, `?`, anchored patterns, directory-only patterns ending in `/`, `**/`, and patterns ending in `/**`. This is not full Git compatibility.

- zfind does not read parent-directory or global Git ignore rules. It also ignores `.ignore` files.
- Character classes (`[]`) and backslash escapes are not supported.
- `.git/` and other hidden entries are not ignored automatically. Use `--no-hidden` to skip them.
- Each `.gitignore` file is limited to 65,535 bytes. zfind keeps at most 1,024 rules per directory.

## Build

```sh
make            # Build into build/x86_64/
```

Sources live per architecture under `src/x86_64/`; the build is driven by a
single `ARCH` variable, so `make ARCH=...` selects another port once one
exists. For the toolchain, use `nix develop` as configured in `flake.nix`.

The resulting binary requires an x86-64-v3-compatible CPU.
