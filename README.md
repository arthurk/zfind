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
zfind --exclude-name=.git --exclude-name=node_modules config
zfind --exclude-name='a*b' config   # literal a*b, not a glob
zfind --separator='\0' --limit 100 .conf  # Print up to 100 matches, NUL-separated
zfind --separator='\t' config   # Print matches separated by tabs
zfind --type f                  # List regular files, traversing directories as usual
zfind --type f --type d         # List regular files and directories
zfind --config ./zfind.conf foo # Read settings from this file instead of the default config
zfind --no-config foo           # Ignore the config file for this run
```

## Matching and output

- By default, matching ignores case unless the pattern contains an uppercase ASCII letter (`A`–`Z`, smart case).
- `--case-sensitive` forces case-sensitive byte comparison.
- `--case-insensitive` forces ASCII case-insensitive comparison, regardless of whether the pattern contains uppercase letters.
- Combining `--case-sensitive` and `--case-insensitive` is a usage error.
- Case folding is ASCII-only (`A`–`Z`). Non-ASCII bytes are compared unchanged in all modes.
- Case flags affect only `PATTERN` matching; `.gitignore` and `--exclude-name` matching are unaffected.
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
- `--exclude-name NAME`, `--exclude-name=NAME` Skip every entry whose basename
  is exactly `NAME`. Repeatable; repeating a name has no additional effect.
  Matching is bytewise and case-sensitive regardless of
  `--case-sensitive`/`--case-insensitive`; it does not interpret glob or regex
  syntax, decode backslash escapes, or normalize Unicode. `NAME` must be
  non-empty and cannot contain `/`. A matching directory is neither printed nor
  descended. Explicit exclusions always apply, including over `.gitignore`
  negations. Options must appear before `PATTERN`.
- `--limit N`, `--limit=N` Print at most `N` results. `0` prints nothing. The limit counts results exactly for every separator, and limited output is the same byte prefix as an unlimited run. Use `--separator='\0'` when a consumer must split the output unambiguously. `--limit` consumes the next token greedily as its value.
- `--type f|d` Print regular files (`f`) and/or directories (`d`). Repeat the
  option to select both types; repeated values have no extra effect. Explicit
  type filters exclude symlinks and special entries. `--type f` still traverses
  directories so matching files below them can be found. `--type` consumes the next token greedily as its value.
- `--config PATH`, `--config=PATH` Read settings from `PATH` instead of the
  default config file. The file must exist. Command-line only; see
  [Configuration](#configuration). `--config` consumes the next token greedily
  as its value; a repeated `--config` is last-wins. Conflicts with `--no-config`.
- `--no-config` Read no config file for this run. Conflicts with `--config`.
- `--` End option parsing. Put it before a pattern that starts with `-`, as in `zfind -- -draft` to search for the name substring `-draft`. Any following argument is treated as positional (the pattern).

## Gitignore support

By default, zfind does not apply `.gitignore` rules and does not prune `.git`. `--no-gitignore` states this default explicitly. The two gitignore flags cannot be combined. `--gitignore` works only when the current directory contains `.git`; zfind does not search parent directories for a repository. It reads `.gitignore` files as it walks the tree. Rules in deeper directories take precedence over inherited rules. With `--gitignore`, the `.git` entry is never listed, walked, or counted: git never tracks anything under it.

Explicit `--exclude-name` exclusions are independent of `.gitignore` and
override its rules, including `!` negations. Excluding `.gitignore` itself
hides the file but does not disable its rules; excluding a directory prevents
its nested `.gitignore` from being read because the directory is never
traversed.

Supported syntax includes negation (`!`), `*`, `?`, anchored patterns, directory-only patterns ending in `/`, `**/`, and patterns ending in `/**`. This is not full Git compatibility.

- zfind does not read parent-directory or global Git ignore rules. It also ignores `.ignore` files.
- Character classes (`[]`) and backslash escapes are not supported.
- Other hidden entries are not ignored automatically. The `.git` entry is
  the exception: `--gitignore` skips it even with `--hidden`, because git
  never tracks anything under it. Use `--no-hidden` to skip all
  dot-prefixed entries.
- Each `.gitignore` file is limited to 65,535 bytes. zfind keeps at most 1,024 rules per directory.

## Configuration

zfind can read persistent defaults from one config file. Command-line options
override them for a single run.

Which file is read:

1. The file given with `--config PATH` (relative paths are relative to the
   current directory). It must exist.
2. Otherwise `$XDG_CONFIG_HOME/zfind/config`, when `XDG_CONFIG_HOME` is a
   non-empty absolute path.
3. Otherwise `$HOME/.config/zfind/config`, when `HOME` is set and non-empty.

If neither default location can be resolved, or the default file does not
exist, zfind runs with its built-in defaults. A file that exists but cannot be
read, is larger than 65,536 bytes, or contains an error stops zfind before any
search, with a message that names the file and the line:

```
zfind: /home/me/.config/zfind/config:3: unknown option
```

zfind does not search the current or parent directories for a config and does
not support includes. `--no-config` skips the file; `--help` never reads it.
`--config` and `--no-config` are command-line options only and must appear
before `PATTERN`.

### Format

One option per line, using the same names and the same value rules as the
command line:

```
# Personal zfind defaults
--case-insensitive
--no-hidden
--gitignore
--exclude-name=node_modules
--exclude-name='build output'
--separator='\t'
--limit 200
```

- Blank lines are ignored. A line whose first non-blank character is `#` is a
  comment. Inline comments are not supported: a `#` elsewhere is part of the value.
- A line is `--name`, `--name=value` or `--name value`. Leading and trailing
  spaces, tabs and `\r` are ignored. Text after a flag (for example a second
  option) is an error; after an option that takes a value, the rest of the line
  is that value. Write `--type f`, not `--type=f`, exactly
  as on the command line.
- A value may be wrapped in matching single or double quotes, which are
  removed; this lets it start or end with a space. The same quote cannot appear
  inside the value. Quotes elsewhere in a value are literal.
- Backslashes are not processed by the config parser; the option itself
  decides what they mean, so `--separator='\t'` is a tab, as on the command line.
- Nothing is expanded: no shell variables, `~`, globs or command substitution.
- Short options, `PATTERN`, `--`, `--help`, `--config` and `--no-config` are
  not allowed in the file. Unknown or malformed lines are errors, never ignored.

Every behavior option the command line accepts is accepted in the file, with
the same validation.

### Precedence

Built-in defaults, then the config file, then the command line.

- **Modes** (`--case-sensitive`/`--case-insensitive`, `--hidden`/`--no-hidden`,
  `--gitignore`/`--no-gitignore`): a mode given on the command line replaces the
  file's. Opposing modes within one source are an error, so a file cannot rely
  on line order. A file's `--case-sensitive` can be overridden with
  `--case-insensitive`, and vice versa; there is no way to ask for smart case
  again from the command line.
- **Scalars** (`--separator`, `--limit`): the command line overwrites the file,
  and the last value within one source wins. A later `--limit N` replaces a
  configured limit, but nothing cancels one yet: `--limit 0` prints nothing.
- **Lists** (`--type`, `--exclude-name`): the command line adds to the file's
  entries; repeating a value has no extra effect. Together they still count
  toward the 64 unique `--exclude-name` values. To ignore the file's lists, use
  `--no-config`.

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
