# zfind

`zfind` is a parallel recursive filesystem search tool for Linux. It searches file and directory names for literal substrings, with deterministic output, smart-case matching, and options for filtering and scripting.

## Features

- **Parallel traversal:** Search directory trees concurrently.
- **Deterministic results:** Sort entries by name within each directory and emit them in depth-first order.
- **Smart-case matching:** Search case-insensitively by default; an uppercase ASCII character makes the search case-sensitive.
- **Literal matching:** Treat the pattern as a byte substring. Globs and regular expressions have no special meaning.
- **Flexible filtering:** Select entry types, prune exact basenames, skip hidden entries, or apply `.gitignore` rules in Git and Jujutsu repositories.
- **Script-friendly output:** Use NUL or a custom separator, limit the number of results, and control ANSI colors.
- **Persistent defaults:** Load options from an XDG-style configuration file, with command-line overrides.

## Quick start

```sh
# Find entries whose names contain "config"
zfind config

# List regular files whose names contain "cargo"
zfind --type f cargo

# Search while applying repository .gitignore rules
zfind --gitignore main

# Search from ./src for names containing "test"
zfind ./src test

# Pass results safely to another command
zfind --type f --separator='\0' log | xargs -0 grep -H "ERROR"

# Keep colors when viewing results in less
zfind --color=always config | less -R
```

## Installation

### Pre-built binary

Standalone binaries are published on the GitHub Releases page.

1. Download `zfind-linux-x86_64-v3` from the latest release.
2. Make it executable and install it:

   ```sh
   chmod +x zfind-linux-x86_64-v3
   sudo mv zfind-linux-x86_64-v3 /usr/local/bin/zfind
   ```

> [!NOTE]
> The release binary requires an **x86-64-v3** compatible processor. This includes support for AVX, AVX2, BMI1, BMI2, and MOVBE; Intel Haswell and AMD Excavator are examples of processor generations that support these instructions.

### Build from source

The repository includes a Nix flake for a reproducible development environment.

```sh
nix develop
make
```

The binary is written to `build/x86_64/zfind`.

## Usage

### Syntax

```text
zfind [OPTIONS] [PATH] [PATTERN]
```

`PATH` is an optional starting directory. It must come before `PATTERN`. `PATTERN` is an optional literal substring to search for in entry names. If omitted, `zfind` lists all entries that pass the active filters. Only one pattern is accepted.

The positional arguments are interpreted as follows:

| Command | Meaning |
| :--- | :--- |
| `zfind config` | Search the current directory for names containing `config`. |
| `zfind src` | Search for the pattern `src`. A bare directory name is not automatically treated as a path. |
| `zfind ./src` | Search from `./src`; the starting directory is not printed. |
| `zfind ./src test` | Search from `./src` for names containing `test`. |
| `zfind --path src` | Explicitly search from the bare directory `src`. |
| `zfind -- ./src` | Search for the literal pattern `./src`, without path detection. |

When there is one positional argument, a token containing `/`, or exactly `.` or `..`, is tested as a starting directory. If it opens as a directory, it becomes `PATH`. If it fails with `ENOENT` or `ENOTDIR`, it is treated as `PATTERN`; other errors are reported. Use `--` to force a path-like token to be a pattern.

When there are two positional arguments, the first is always `PATH` and must be a directory. The second is `PATTERN`. Use `--path DIR` or `--path=DIR` to specify the starting directory explicitly; its value is always a path and is never treated as a pattern.

Options must appear before positional arguments. `--` must also appear before the first positional argument; it ends option parsing and disables path detection for the remaining argument. `--help` is an exception: it can appear anywhere before `--` and exits immediately without reading the configuration file.

### Common tasks

#### Match case explicitly

```sh
zfind test                       # Smart case: matches test, Test, TEST, etc.
zfind Test                       # Smart case: uppercase makes the match case-sensitive
zfind --case=sensitive test      # Force case-sensitive matching
zfind --case=insensitive TEST    # Force ASCII case-insensitive matching
zfind --case=smart test          # Restore smart case, including over a config setting
```

`--case` accepts one mode: `smart`, `sensitive`, or `insensitive`. If specified more than once, the last value wins.

#### Choose a starting directory

```sh
zfind ./src                      # Search from src; output paths start with ./src/
zfind ./src test                 # Search from src for names containing test
zfind --path src test            # Same search, with an explicit path option
zfind .                          # Search the current directory; output paths start with ./
zfind ..                         # Search the parent directory; output paths start with ../
```

The starting directory is a traversal root, not a result. It is never printed, and filters such as `--type`, `--no-hidden`, and `--exclude-name` apply to its descendants. Output preserves the spelling of the selected root, apart from redundant trailing slashes. For example, selecting `/` prints `/name`, not `//name`.

#### Filter entry types

```sh
zfind --type f                   # Regular files
zfind --type d                   # Directories (output paths end in /)
zfind --type l                   # Symlinks, including dangling links
zfind --type b                   # Broken symlinks only
zfind --type f --type d          # Regular files and directories, but no symlinks
zfind --type f --type l          # Regular files and symlinks to regular files
zfind --exclude-type d           # Everything except directories and links to directories
zfind --type l --exclude-type d  # Symlinks except links to directories
```

See [Symlinks and type filters](#symlinks-and-type-filters) for the exact rules.

#### Exclude names and hidden entries

```sh
zfind --no-hidden TODO
zfind --exclude-name=target --exclude-name=dist bundle
zfind --exclude-name='a*b'       # Exact name a*b; this is not a glob
```

#### Use repository ignore rules

```sh
zfind --gitignore main
zfind --gitignore ./src main
```

`--gitignore` applies the supported `.gitignore` rules in recognized Git and Jujutsu repositories. The way the repository is discovered depends on whether you select a starting path; see [Gitignore support](#gitignore-support).

#### Use custom output separators and limits

```sh
# NUL-delimited output is safe for arbitrary filesystem names
zfind --type f --separator='\0' log | xargs -0 grep -H "ERROR"

# Tab-separated output, capped at 50 results
zfind --separator='\t' --limit=50 config

# Print no results
zfind --limit=0 config

# Print every result, overriding a configured limit
zfind --limit=none config
```

#### Use color with a pager

```sh
zfind config | less -R                 # Plain output: auto detects the pipe
zfind --color=always config | less -R  # Keep ANSI colors in the pager
```

## Options reference

All options must appear before the positional arguments, except `--help`, which is accepted anywhere before `--`.

| Option | Value | Default | Description |
| :--- | :--- | :--- | :--- |
| `--help` | — | — | Print help and exit. Does not read configuration files. |
| `--case` | `smart`, `sensitive`, `insensitive` | `smart` | Select the pattern-matching case mode. Accepts `--case MODE` or `--case=MODE`; repeated uses are last-wins. |
| `--hidden` / `--no-hidden` | — | Hidden entries included | Include hidden entries or prune them from traversal. The flags conflict if both are given in the same source. |
| `--gitignore` / `--no-gitignore` | — | Off | Enable or disable `.gitignore` processing. The flags conflict if both are given in the same source. |
| `--path` | `DIR` | Current directory | Set the starting directory. Accepts `--path DIR` or `--path=DIR`; may be used only once. The value must be non-empty and name an accessible directory. |
| `--type` | `f`, `d`, `l`, `b` | All entry types | Include regular files, directories, symlinks, or broken symlinks. Repeatable; accepts `--type=VALUE`. See [Symlinks and type filters](#symlinks-and-type-filters). |
| `--exclude-type` | `f`, `d`, `l`, `b` | None | Exclude selected types from output. Repeatable and additive across the config file and command line. Accepts `--exclude-type=VALUE`. |
| `--exclude-name` | `NAME` | None | Prune entries with this exact basename. Repeatable; up to 64 unique values across the config file and command line. |
| `--separator` | `STR` | `\n` | Terminate each result with the decoded separator, from 1 to 64 bytes. Supports `\n`, `\t`, `\r`, `\0`, and `\\`. |
| `--limit` | `N` or `none` | `none` | Print at most `N` results. `0` prints no results; `none` means unlimited. `N` may be any whole number up to `18446744073709551615`. |
| `--color` | `auto`, `always`, `never` | `auto` | Control ANSI styling. `auto` uses terminal and environment checks; `always` forces color; `never` disables it. Accepts `--color=WHEN`. |
| `--config` | `PATH` | Automatic lookup | Load the configuration file at `PATH`. Relative paths are resolved from the current working directory. The file must exist. |
| `--no-config` | — | Off | Ignore all configuration files for this run. |
| `--` | — | — | End option parsing and disable automatic path detection. The remaining argument is treated as the literal pattern. |

## Search and traversal behavior

### Pattern matching

- Matching is performed against each entry's **basename**, not its full path.
- Patterns are literal byte substrings. `*`, `?`, and regular-expression syntax have no special meaning.
- Smart case is the default. Matching is ASCII case-insensitive unless the pattern contains at least one uppercase ASCII character (`A`–`Z`), which makes it case-sensitive.
- `--case=sensitive` compares bytes exactly. `--case=insensitive` folds only ASCII letters (`A`–`Z` to `a`–`z`). Non-ASCII bytes are compared verbatim; there is no Unicode normalization or case folding.
- `--case` affects only `PATTERN`. It does not affect `--exclude-name` or `.gitignore` matching.

### Hidden entries

Hidden entries are included by default. An entry is hidden when its name begins with a dot (`.`); this includes directories such as `.git` and files such as `.env`.

`--no-hidden` prunes hidden files and directories. A pruned directory is not traversed. `--hidden` explicitly selects the default behavior and can override a configured `--no-hidden`. Combining `--hidden` and `--no-hidden` in the same source is an error.

### Symlinks and type filters

`zfind` does not descend into symlinked directories found during traversal. A symlink explicitly supplied as the starting directory is followed; output retains the symlink's spelling, while repository discovery and ignore matching use its physical filesystem location.

Without `--type`, all entry types are listed, including special files. When `--type` is used, the values combine as a set:

| Type | Meaning |
| :--- | :--- |
| `f` | Regular files. When `l` is also selected, includes symlinks whose final target is a regular file. |
| `d` | Directories. When `l` is also selected, includes symlinks whose final target is a directory. Directory results end with `/`. |
| `l` | All symlinks when used alone. When combined with `f` and/or `d`, the selected target types determine which symlinks are included. Symlinks are printed without a trailing slash and are never traversed. |
| `b` | Broken symlinks. This value does not require `l`; it adds broken links to `f` and/or `d` results. |

With `--type l` alone, every symlink is selected, including dangling links. When `l` is combined with `f` and/or `d`, symlinks are included only when their final target matches one of those selected types. For example, `--type f --type l` includes regular files and links to regular files; it does not include links to directories. `--type f --type d --type l` includes files, directories, and links to either. Links to sockets, FIFOs, or devices do not match `f` or `d`. A chain is followed to its final target for this check.

A link is classified as broken only when its target is known not to exist: for example, the target is missing (`ENOENT`), a path component is not a directory (`ENOTDIR`), the link chain loops (`ELOOP`), or the target path is too long (`ENAMETOOLONG`). A link that cannot be checked because of permissions, I/O errors, or a stale mount is not considered broken. A link to a device, socket, or FIFO that resolves is not broken either.

`--type b` combined with `--type l` is equivalent to `--type l`, because `l` already includes every symlink. Adding `b` to `f` and/or `d` adds broken links to those results. For example, `--type f --type b` selects regular files plus broken symlinks.

Repeating a type has no additional effect. A type can be written as `--type f` or `--type=f`. Values must be a single lowercase letter from `f`, `d`, `l`, or `b`; invalid or empty values are usage errors. The option consumes the next argument as its value, even when it begins with `-`.

### Type exclusions

`--exclude-type` removes matching entries from the output after the inclusion filter is applied. If no `--type` is specified, it filters the default set of all entry types.

It uses the same meanings as `--type`, with these consequences:

- `--exclude-type f` removes regular files and symlinks to regular files.
- `--exclude-type d` removes directories and symlinks to directories.
- `--exclude-type l` removes every symlink.
- `--exclude-type b` removes broken symlinks. Broken links are neither files nor directories, so they are not removed by `f` or `d` alone.

Repeated values accumulate. A value present in both `--type` and `--exclude-type` is a usage error. Unlike `--exclude-name`, `--exclude-type` does not prune traversal: an excluded directory is still traversed, and its children can still be printed.

### Basename exclusions

`--exclude-name NAME` prunes any entry whose basename exactly matches `NAME`.

- Matching is case-sensitive and bytewise, regardless of `--case`.
- `NAME` must not be empty or contain `/`.
- Globs, regular expressions, Unicode normalization, and backslash escapes are not interpreted.
- A matching directory is neither printed nor traversed.
- Exclusions take precedence over `.gitignore` negation patterns (`!`). If a directory is excluded, its `.gitignore` file is never read.

Up to 64 unique `--exclude-name` values may be specified across the configuration file and command line.

## Gitignore support

By default, `.gitignore` files are not read. The `.git/` and `.jj/` directories are traversed like ordinary directories unless `--gitignore` is enabled.

With `--gitignore`, `zfind` applies its supported Git-style ignore rules in recognized Git and Jujutsu repositories.

### Repository discovery

Repository discovery depends on how the search root is selected:

- **No selected path:** The current working directory must contain a `.git` or `.jj` entry. Parent directories are not searched.
- **Selected path:** `zfind` walks upward from the selected directory's actual filesystem location and uses the nearest repository marker. A `.git` directory, a regular `.git` file, or a `.jj` directory is a marker.

The current working directory's repository is not used when a different path is selected. If both `.git` and `.jj` exist at the same level, ignore processing is enabled once. If no repository marker is found, ignore processing remains inactive.

For example, when the current directory is `repo/sub`:

```sh
zfind --gitignore      # Does not search parents for repo/.git or repo/.jj
zfind . --gitignore    # Selected path: discovers the enclosing repository
```

### Rule loading and traversal

- For a selected path, `.gitignore` files from the repository root through the selected directory's parent are loaded in parent-to-child order.
- The selected directory's own `.gitignore` applies to its descendants. Before traversal, ancestor rules are checked against the selected directory itself; if it is ignored, the command succeeds with no results.
- For a selected symlink root, repository discovery and ignore matching use its physical location, but output keeps the symlink spelling. For example, if `link` points to `repo/sub`, a `/sub/build/` rule applies to `link/build/`.
- When ignore processing is active, `.git` and `.jj` metadata directories are never listed, traversed, or counted, even when `--hidden` is used. Selecting a path equal to or below one of these metadata directories produces no results.
- `.gitignore` files are read as traversal proceeds. Rules in deeper directories override inherited parent rules.

### Supported patterns and limits

Supported pattern features:

- Negation prefixes (`!`)
- `*` and `?` wildcards
- Root-anchored patterns such as `/pattern`
- Directory-only patterns such as `pattern/`
- Leading and trailing glob stars, such as `**/foo` and `foo/**`

The implementation has these limits:

- Maximum `.gitignore` file size: **65,535 bytes**.
- Maximum active rules per directory: **1,024**.

The following features are not supported:

- Character classes such as `[a-z]`.
- Backslash escapes such as `\#` or `\*`.
- Global ignore files such as `~/.config/git/ignore`.
- `.git/info/exclude` and `.ignore` files.

## Output

### Ordering and paths

Results are deterministic. Entries are sorted lexicographically by name within each directory and emitted in depth-first traversal order.

Results are descendants of the selected starting directory; the starting directory itself is never printed. Without an explicit starting path, output is relative to the current directory (`src/main.c`, `build/`). Selecting `./src` preserves that prefix (`./src/main.c`); selecting `.` prints paths such as `./main.c`; selecting `/` prints `/main.c` without a doubled slash. Absolute roots retain their absolute prefix. Redundant trailing slashes are removed from the selected root.

Directory results end with `/`. Symlinks never end with `/`, even when they point to directories.

### Separators

Every result, including the last, is followed by the selected separator. If there are no results, `zfind` writes zero bytes.

Use `--separator STR` or `--separator=STR` to choose a delimiter. The decoded separator must be between 1 and 64 bytes. The recognized escape sequences are `\n`, `\t`, `\r`, `\0`, and `\\`. Other characters, including literal control characters, are used as-is; unrecognized backslash sequences are rejected. The option consumes the next argument as its value, even if it begins with `-`.

> [!WARNING]
> Do not pass an actual NUL byte in a shell argument. For example, Bash truncates `$'\0'` at the NUL byte, resulting in an empty argument. Pass the two-character escape sequence instead:
>
> ```sh
> zfind --separator='\0' pattern
> ```
>
> NUL is the only byte that cannot appear in a POSIX filesystem path, so NUL-delimited output is the only separator mode that is unambiguous for every possible path.

### Result limits

`--limit N` stops after emitting at most `N` results. The result stream is an exact byte-for-byte prefix of an unlimited search run.

- `--limit 0` emits no results.
- `--limit N`, where `N` is positive, emits at most `N` results.
- `--limit none` disables the limit. This is the built-in default and overrides a limit set in the configuration file.

`N` is a whole number from `0` through `18446744073709551615`. The value `none` is lowercase. `--limit` consumes the next argument as its value.

### Color

`--color WHEN` styles results for terminal reading.

| Element | Style |
| :--- | :--- |
| Directory path components | Cyan (`ESC[36m`) |
| Matching text in the basename | Bold and reverse video (`ESC[1;7m`) |
| Other text | Terminal default |

Only the basename is highlighted; directory components are not searched. Highlighting uses the same smart-case and ASCII case-folding rules as the search, merges adjacent matched spans, and preserves the original bytes and casing. For directory results, the directory path is cyan, except that matching bytes in the directory basename use the match style. The trailing slash is cyan. Symlinks are styled like files.

`auto` (the default) emits color only when stdout is a terminal, `NO_COLOR` is unset or empty, and `TERM` is set to a value other than `dumb`. If `TERM` is unset, output is plain. `always` emits ANSI styling even when output is piped or redirected. `never` disables styling.

The mode is resolved once at startup. Command-line settings override the configuration file. Environment and terminal checks apply only to `auto`; `--color=always` therefore overrides `NO_COLOR` and `TERM=dumb`.

The configuration file accepts `--color auto` and `--color never`, but not `--color always`. This prevents a global setting from unexpectedly adding escape sequences to scripts and pipelines. Setting `always` in the configuration file is an error.

## Configuration

`zfind` can read default options from one configuration file. Built-in defaults are applied first, then the configuration file, then command-line options.

### File lookup

At most one configuration file is loaded. The lookup order is:

1. `--config PATH`. Relative paths are resolved from the current working directory, and the file must exist.
2. `$XDG_CONFIG_HOME/zfind/config`, when `XDG_CONFIG_HOME` is set to a non-empty absolute path.
3. `$HOME/.config/zfind/config`, when `HOME` is set and non-empty.

If no default configuration file is available, `zfind` uses its built-in defaults. `--no-config` skips configuration loading. `--help` never reads a configuration file.

A configuration file must be readable and no larger than **65,536 bytes**. An unreadable file, oversized file, or syntax error stops the search and reports the file and line where applicable. For example:

```text
zfind: /home/user/.config/zfind/config:3: unknown option
```

### File format

The configuration file is line-oriented. Each non-blank line contains one option, using the same forms as the command line: `--name`, `--name=value`, or `--name value`.

```ini
# ~/.config/zfind/config

# Matching and traversal
--case=insensitive
--no-hidden
--gitignore

# Default exclusions
--exclude-name=node_modules
--exclude-name=.venv
--exclude-name="build output"

# Output
--separator='\n'
--limit 500
--color auto
```

Rules:

- Lines whose first non-whitespace character is `#` are comments. Inline comments are not supported; `#` elsewhere is part of the value.
- Values may be enclosed in single or double quotes. Quotes preserve leading and trailing whitespace and are removed when parsing; the enclosing quote character cannot appear inside the value.
- Shell variables, tilde expansion, globs, and command substitutions are not expanded. Backslashes are passed to the option handler, so `--separator='\t'` is decoded by the separator option.
- Short options, positional paths or patterns, `--`, `--path`, `--help`, `--config`, and `--no-config` are not allowed in the configuration file.
- `--color always` is not allowed in the configuration file. Use `auto` or `never` instead.

### Precedence and merging

Options are evaluated in this order: **built-in defaults → configuration file → command line**.

| Option group | Merge behavior | Rule |
| :--- | :--- | :--- |
| Hidden and Gitignore modes | CLI overrides config | Conflicting flags in the same source are errors. A CLI mode replaces the configured mode. |
| Scalar options: `--case`, `--separator`, `--limit`, `--color` | Last value wins | A command-line value replaces the configured value. Use `--case=smart` or `--limit=none` to undo a configured setting. `--limit=0` still means no results. |
| `--type` | CLI set replaces config set | If the CLI specifies any `--type`, all configured `--type` values are ignored for that run. Repeated values within one source accumulate. |
| `--exclude-name` and `--exclude-type` | Cumulative set union | Config and CLI values are combined; duplicates are removed. The combined set can contain at most 64 unique `--exclude-name` values. A value present in both the merged `--type` and `--exclude-type` sets is an error. |

`--exclude-type` values accumulate across the configuration file and command line. For example, a configured `--exclude-type l` and a command-line `--exclude-type d` exclude both symlinks and directories (including links to directories).

Use `--no-config` when a command must ignore all configured defaults, including exclusions.

## Release workflow

To create a release:

1. Merge the changes to `main` and make sure CI passes.
2. Create and push an annotated version tag:

   ```sh
   git tag -a v1.0.0 -m "v1.0.0"
   git push origin v1.0.0
   ```

3. The tag workflow builds the project, runs the test suite, and creates a draft GitHub Release with the `zfind-linux-x86_64-v3` binary and its `.sha256` checksum. The tag must match `vMAJOR.MINOR.PATCH` and point to a commit on `main`; otherwise, the workflow aborts. Review the assets, add release notes, and publish the draft.

## Disclaimer

This project uses LLM-based coding agents for much of its implementation and experimentation. A human defines the goals, architecture, and review criteria, and reviews all contributions before they are merged.
