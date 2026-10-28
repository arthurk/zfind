# zfind

`zfind` searches file and directory names under the current directory. It searches recursively and in parallel.

## Usage

```sh
zfind [OPTIONS] [--] [PATTERN]
```

`PATTERN` is optional. When provided, zfind finds entries whose names contain that literal text. Regex and glob patterns are not supported. Without a pattern, it lists every entry unless `--type` restricts the result types.

Options must appear before `PATTERN`. `--` ends option parsing; any argument following `--` is treated as `PATTERN`.

```sh
zfind config                    # Find names containing "config"
zfind --case-sensitive foo      # Case-sensitive search for "foo"
zfind --case-insensitive FOO    # Case-insensitive search for "FOO"
zfind --hidden TODO             # Find names containing "TODO", hidden entries included
zfind --no-hidden TODO          # Find names containing "TODO" and skip hidden entries
zfind --gitignore Cargo         # Find names containing "Cargo" and apply supported .gitignore rules
zfind --no-gitignore Cargo      # Search without applying .gitignore rules (the default)
zfind --separator='\0' --limit 100 .conf  # Print up to 100 matches, NUL-separated
zfind --separator='\t' config   # Print matches separated by tabs
zfind --type f                  # List regular files, traversing directories as usual
zfind --type f --type d         # List regular files and directories
```

## Matching and output

- By default, matching ignores case unless the pattern contains an uppercase ASCII letter (`A`–`Z`, smart case).
- `--case-sensitive` forces case-sensitive byte comparison.
- `--case-insensitive` forces ASCII case-insensitive comparison, regardless of whether the pattern contains uppercase letters.
- Combining `--case-sensitive` and `--case-insensitive` is a usage error.
- Case folding is ASCII-only (`A`–`Z`). Non-ASCII bytes are compared unchanged in all modes.
- Case flags affect only `PATTERN` matching; `.gitignore` matching is unaffected.
- Hidden entries, including `.git/`, are searched by default. `--hidden`
  states that explicitly; on its own it changes no results. `--no-hidden`
  skips them and their contents; combining the two is a usage error.
- Results are sorted by name within each directory. Output follows depth-first order.
- Paths are relative to the current directory. Directory results end with `/`.
- Each result is terminated by the selected separator, including the last one. Newline is the default. A run with no results writes nothing.
- zfind does not follow symlinked directories. Explicit `--type f` and
  `--type d` filters exclude symlinks, including symlinks to files and directories.

## Options

- `--help` Show help and exit. A pre-scan recognizes `--help` anywhere before `--`, including after the pattern or where an option value would normally go.
- `--separator STR`, `--separator=STR` Terminate each result with `STR` instead of newline. The decoded value must be 1–64 bytes. Recognized escapes are `\n`, `\t`, `\r`, `\0`, and `\\`; unknown backslash escapes are rejected, and other bytes, including real control characters, are literal. `--separator` consumes the next token greedily as its value, even if it starts with `-`; repeated uses are last-wins. Pass the escape text literally, for example `--separator='\t'`. Do not use bash `$'\0'` for NUL: bash truncates the argument at the NUL byte and makes zfind see an empty value; use `--separator='\0'`. Only NUL guarantees unambiguous parsing, because a filename cannot contain NUL; a custom separator can appear in a filename or be formed across adjacent output bytes.
- `--case-sensitive` Match pattern case-sensitively. Must appear before `PATTERN`.
- `--case-insensitive` Match pattern case-insensitively (ASCII `A`–`Z`). Must appear before `PATTERN`.
- `--gitignore` Apply supported `.gitignore` rules and skip the `.git`
  entry itself. Conflicts with `--no-gitignore`. See
  [Gitignore support](#gitignore-support).
- `--no-gitignore` Do not apply `.gitignore` rules; this is the default. The
  `.git` entry is not pruned. Conflicts with `--gitignore`.
- `--hidden` Search hidden entries (the default). On its own this changes no
  results; combining it with `--no-hidden` is a usage error.
- `--no-hidden` Skip names that begin with `.`, including hidden directories and everything inside them.
- `--limit N`, `--limit=N` Print at most `N` results. `0` prints nothing. The limit counts results exactly for every separator, and limited output is the same byte prefix as an unlimited run. Use `--separator='\0'` when a consumer must split the output unambiguously. `--limit` consumes the next token greedily as its value.
- `--type f|d` Print regular files (`f`) and/or directories (`d`). Repeat the
  option to select both types; repeated values have no extra effect. Explicit
  type filters exclude symlinks and special entries. `--type f` still traverses
  directories so matching files below them can be found. `--type` consumes the next token greedily as its value.
- `--` End option parsing. Put it before a pattern that starts with `-`, as in `zfind -- -draft` to search for the name substring `-draft`. Any following argument is treated as positional (the pattern).

## Gitignore support

By default, zfind does not apply `.gitignore` rules and does not prune `.git`. `--no-gitignore` states this default explicitly. The two gitignore flags cannot be combined. `--gitignore` works only when the current directory contains `.git`; zfind does not search parent directories for a repository. It reads `.gitignore` files as it walks the tree. Rules in deeper directories take precedence over inherited rules. With `--gitignore`, the `.git` entry is never listed, walked, or counted: git never tracks anything under it.

Supported syntax includes negation (`!`), `*`, `?`, anchored patterns, directory-only patterns ending in `/`, `**/`, and patterns ending in `/**`. This is not full Git compatibility.

- zfind does not read parent-directory or global Git ignore rules. It also ignores `.ignore` files.
- Character classes (`[]`) and backslash escapes are not supported.
- Other hidden entries are not ignored automatically. The `.git` entry is
  the exception: `--gitignore` skips it even with `--hidden`, because git
  never tracks anything under it. Use `--no-hidden` to skip all
  dot-prefixed entries.
- Each `.gitignore` file is limited to 65,535 bytes. zfind keeps at most 1,024 rules per directory.

## Build

```sh
make            # Build into build/x86_64/
```

Sources live per architecture under `src/x86_64/`; the build is driven by a
single `ARCH` variable, so `make ARCH=...` selects another port once one
exists. For the toolchain, use `nix develop` as configured in `flake.nix`.

The resulting binary requires an x86-64-v3-compatible CPU.

## Release

After a commit is on `main` and its CI run passes, create and push a version tag:

```sh
git tag -a v1.0.0 -m "v1.0.0"
git push origin v1.0.0
```

The tag workflow runs the tests, builds the binary, and publishes a GitHub
Release with the `zfind-linux-x86_64-v3` asset.
