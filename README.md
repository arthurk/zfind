# zfind

A high-performance, parallel recursive file search utility for Linux.

`zfind` traverses directory trees concurrently and matches filenames using literal substring queries. Designed with deterministic depth-first ordering, smart-case sensitivity, and script-friendly output formats, it provides a fast and predictable foundation for interactive shell usage and automated pipelines.

---

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
- [Installation](#installation)
  - [Pre-built Binaries](#pre-built-binaries)
  - [Building from Source](#building-from-source)
- [Usage](#usage)
  - [Syntax](#syntax)
  - [Common Recipes](#common-recipes)
- [Options Reference](#options-reference)
- [Search & Matching Mechanics](#search--matching-mechanics)
  - [Pattern Matching & Smart Case](#pattern-matching--smart-case)
  - [Hidden Files & Directories](#hidden-files--directories)
  - [Symlinks & Type Filtering](#symlinks--type-filtering)
  - [Output Format & Separators](#output-format--separators)
  - [Color Output](#color-output)
  - [Result Limiting](#result-limiting)
- [Gitignore Support](#gitignore-support)
- [Configuration](#configuration)
  - [File Resolution](#file-resolution)
  - [Format & Syntax](#format--syntax)
  - [Precedence & Merging](#precedence--merging)
- [Release Workflow](#release-workflow)
- [Disclaimer](#disclaimer)

---

## Features

- **Blazing Fast & Parallel:** Concurrent recursive traversal optimized for modern multi-core x86-64 processors.
- **Deterministic Output:** Results are sorted lexicographically by name within each directory and emitted in depth-first order.
- **Smart Case by Default:** Case-insensitive search if the query is all-lowercase; automatically switches to case-sensitive when an uppercase ASCII character is present.
- **Safe Pipeline Integration:** Built-in support for NUL (`\0`) and custom delimiters makes piping to `xargs -0` robust and injection-safe.
- **Adaptive Color:** Cyan directory components and bold/reverse match highlights on a terminal; piped output stays plain unless `--color=always` is given.
- **Exact Literal Matching:** Never worry about unexpected globbing or regex syntax quirks—queries match raw substring bytes directly.
- **Flexible Exclusions:** Fast exact basename pruning (`--exclude-name`) and nested `.gitignore` parsing.
- **Persistent Configuration:** Configure project or user defaults via XDG-compliant config files with full CLI override support.

---

## Quick Start

```sh
# Search for any file or directory containing "config"
zfind config

# Search files only, excluding node_modules and .git
zfind --type f --exclude-name=node_modules --exclude-name=.git cargo

# Respect .gitignore rules and limit output to 10 entries
zfind --gitignore --limit 10 main

# Safely delete all temporary cache directories
zfind --type d --separator='\0' .cache | xargs -0 rm -rf

# Colorized results through a pager
zfind --color=always config | less -R
```

---

## Installation

### Pre-built Binaries

Pre-compiled standalone binaries are published for each version on the GitHub Releases page:

1. Download `zfind-linux-x86_64-v3` from the latest release.
2. Make the binary executable:
   ```sh
   chmod +x zfind-linux-x86_64-v3
   sudo mv zfind-linux-x86_64-v3 /usr/local/bin/zfind
   ```

> [!NOTE]
> The binary requires an **x86-64-v3** compatible processor (supporting AVX, AVX2, BMI1, BMI2, and MOVBE; generally Intel Haswell / AMD Excavator or newer).

### Building from Source

The repository includes a Nix flake for reproducible builds.

1. **Enter the Nix development environment:**
   ```sh
   nix develop
   ```

2. **Build the binary:**
   ```sh
   make
   ```
   The compiled binary will be placed in `build/x86_64/zfind`.

3. **Targeting other architectures:**
   Sources are structured per architecture under `src/<ARCH>/`. To build for an alternative supported target:
   ```sh
   make ARCH=x86_64
   ```

---

## Usage

### Syntax

```text
zfind [OPTIONS] [--] [PATTERN]
```

- **`PATTERN`** is optional. When omitted, `zfind` lists every traversed entry (subject to `--type` and exclusion filters). When provided, it performs a literal substring search on entry names. Regex and glob expansions are not supported.
- **Options** must precede `PATTERN`.
- **`--`** explicitly terminates option parsing. Any subsequent token is treated as `PATTERN`, which is necessary when searching for names beginning with a hyphen (e.g., `zfind -- -draft`).
- **`--help`** can be passed anywhere before `--` (including where an option value or pattern would otherwise appear). It immediately prints help text and exits.

---

### Common Recipes

#### Basic & Case-Sensitive Searching
```sh
zfind test                  # Smart case: matches "test", "Test", "TEST"
zfind Test                  # Smart case: matches only "Test", "Testing", etc.
zfind --case-sensitive foo  # Force exact case-sensitive byte comparison
zfind --case-insensitive FOO # Force case-insensitive match for "foo", "Foo", etc.
```

#### Traversal & File Types
```sh
zfind --type f              # List all regular files
zfind --type d              # List all directories (output ends with /)
zfind --type l              # List all symlinks (to files, to directories, or dangling)
zfind --type f --type d     # List both regular files and directories (excludes symlinks)
zfind --type f --type l     # List regular files and symlinks
zfind --type=f              # Same as --type f (--type=VALUE works for every value)
```

#### Filtering & Ignoring
```sh
zfind --no-hidden TODO      # Skip dotfiles/hidden directories like .git/ or .env
zfind --gitignore Cargo     # Apply .gitignore rules (requires .git in current directory)
zfind --exclude-name=target --exclude-name=dist bundle
zfind --exclude-name='a*b'  # Matches the literal name "a*b", not a glob
```

#### Pipelines & Tool Integration
```sh
# Process matched files safely with xargs (handles spaces and newlines)
zfind --type f --separator='\0' log | xargs -0 grep -H "ERROR"

# Tab-separated output for awk or tabular tools
zfind --separator='\t' --limit 50 config

# No limit (the default); also cancels a configured limit
zfind --limit=none config

# Interactive fuzzy filtering with fzf
zfind --type f | fzf
```

#### Color & Paging
```sh
zfind --color=always foo | less -R   # keep color through the pager
zfind foo | less -R                  # plain: auto sees a pipe
```

Scripts need no flag: piped output is plain by default. Add `--color=never` only when a wrapper or alias forces color. If you want color in every pager invocation, pass `--color=always` explicitly (or alias it), knowing that pipes will then be colored too.

---

## Options Reference

| Option | Value / Argument | Default | Description |
| :--- | :--- | :--- | :--- |
| `--help` | — | — | Print help text and exit immediately. |
| `--case-sensitive` | — | Off | Force exact case-sensitive byte matching on `PATTERN`. |
| `--case-insensitive`| — | Off | Force ASCII case-insensitive matching on `PATTERN`. |
| `--hidden` | — | Enabled | Search hidden files and directories (names starting with `.`). |
| `--no-hidden` | — | Off | Prune all hidden files and directories from traversal. |
| `--gitignore` | — | Off | Parse and apply `.gitignore` rules (skips `.git/`). |
| `--no-gitignore` | — | Enabled | Do not apply `.gitignore` rules; walk all non-pruned trees. |
| `--type` | `f` \| `d` \| `l` | All types | Filter output by regular files (`f`), directories (`d`) or symlinks (`l`). Repeatable. Also accepted as `--type=VALUE`. |
| `--exclude-name` | `NAME` | None | Prune entries whose exact basename matches `NAME`. Repeatable (up to 64). |
| `--separator` | `STR` (escaped) | `\n` | Terminate each result with `STR` (1–64 bytes). Supports escapes like `\0`, `\t`. |
| `--limit` | `N` \| `none` | Unlimited | Print at most `N` results; `0` prints nothing. `none` means no limit: it is the default, and it also cancels a limit set in the config file. |
| `--color` | `auto` \| `always` \| `never` | `auto` | Style results for terminals. `auto` colors only a tty with `NO_COLOR` unset/empty and `TERM` set and not `dumb`; `always` forces ANSI even into pipes; `never` disables it. Also accepted as `--color=WHEN`. |
| `--config` | `PATH` | System paths | Load persistent configuration from `PATH`. |
| `--no-config` | — | Off | Bypass all configuration files for this run. |
| `--` | — | — | Cease option processing; remaining argument is taken as `PATTERN`. |

---

## Search & Matching Mechanics

### Pattern Matching & Smart Case

- **Literal Substrings:** `PATTERN` is checked directly against directory and file basenames. Glob patterns (`*`, `?`) and regular expressions are treated as raw literal bytes.
- **Smart Case (Default):** Matching is ASCII case-insensitive unless the query string contains at least one uppercase ASCII character (`A`–`Z`), in which case matching becomes case-sensitive.
- **Explicit Flags:**
  - `--case-sensitive` overrides default behavior to require exact byte matches.
  - `--case-insensitive` forces ASCII case folding regardless of whether uppercase characters are present.
  - Specifying both `--case-sensitive` and `--case-insensitive` in the same invocation triggers a usage error.
- **Encoding:** Case folding is strictly ASCII-based (`A`–`Z` $\leftrightarrow$ `a`–`z`). Non-ASCII bytes (e.g., UTF-8 multibyte characters) are compared verbatim without normalization.
- **Scope:** Case flags affect only the search `PATTERN`. `--exclude-name` and `.gitignore` matches are completely unaffected by them.

### Hidden Files & Directories

- By default, hidden entries (names starting with a dot `.`, including `.git/`) are visited and searched.
- `--hidden` explicitly documents this default behavior and has no additional effect on its own.
- `--no-hidden` prunes any file or directory starting with `.`. Pruned directories are never traversed.
- Combining `--hidden` and `--no-hidden` is an error.

### Symlinks & Type Filtering

- `zfind` **never descends into symlinked directories**.
- By default, all entry types (files, directories, symlinks, special files) are output.
- `--type f` restricts output to regular files. Directory traversal continues unaffected so child files can be found.
- `--type d` restricts output to directories. Output paths for directories always end with a trailing slash (`/`).
- `--type l` restricts output to symlinks: links to files, links to directories and dangling links alike. Links are never followed, so each is reported by its own name; a symlink to a directory is printed without a trailing slash and is not descended. Directory traversal continues unaffected so links below real directories are found.
- Repeating `--type` (e.g., `--type f --type d` or `--type f --type l`) selects the union of the given types; repeated values have no extra effect.
- `--type f` and `--type d` **exclude all symlinks**, even if the target is a regular file or directory; only `--type l` selects them. Special files (FIFOs, sockets, devices) are listed only when no `--type` is given.
- The value can be written as `--type f` or `--type=f`. It must be exactly one lowercase letter (`f`, `d` or `l`); anything else, including an empty value, is a usage error. `--type` consumes the next token greedily as its value.

### Output Format & Separators

- **Ordering:** Output is deterministic. Within any given directory, entries are sorted lexicographically by name. Directories are output in depth-first traversal order.
- **Relative Paths:** All output paths are relative to the current directory (e.g. `src/main.c`, `build/`).
- **Separators:** Every match, including the last one, is followed by the chosen separator. If no matches are found, `zfind` writes zero bytes.
- Custom delimiters can be specified via `--separator STR` or `--separator=STR`:
  - Decoded length must be between 1 and 64 bytes.
  - Recognized escape sequences: `\n`, `\t`, `\r`, `\0`, and `\\`.
  - All other characters (including unescaped control characters) are treated literally. Unrecognized backslash sequences are rejected.
  - `--separator` greedily takes the next argument token, even if it begins with a hyphen (`-`).

> [!WARNING]
> In shells such as Bash, passing `--separator=$'\0'` will cause the shell to truncate the argument at the NUL byte, passing an empty string to `zfind`. Always pass escape sequences literally:
> ```sh
> zfind --separator='\0' pattern
> ```
> Using NUL (`\0`) is the only way to guarantee unambiguous stream parsing, as NUL is the only byte that cannot appear in a POSIX filesystem path.

### Color Output

`--color WHEN` (also `--color=WHEN`) styles results for human reading:

| Element | ANSI style |
| :--- | :--- |
| Directory components | Cyan (`ESC[36m`) |
| Matching basename text | Bold + reverse video (`ESC[1;7m`) |
| Everything else | Terminal default |

- Only the basename is highlighted; directory components are never matched. Matching uses the same smart-case and ASCII folding rules as the search itself, adjacent matches merge into one run, and the original bytes and casing are preserved.
- Directory results are cyan, including the trailing `/`, except where a pattern highlights part of the directory basename (those bytes are bold + reverse). Symlinks are rendered like files, so they never gain a trailing slash.
- `auto` (the default) emits color only when all of these hold: stdout is a terminal, `NO_COLOR` is unset or empty, and `TERM` is set and not `dumb`. An unset `TERM` means plain output.
- `always` emits color even into a pipe or a file. `never` emits exactly the plain output.
- Precedence is command line, then config file, then `auto`. The environment and terminal checks apply only to `auto`, so an explicit `--color=always` overrides `NO_COLOR` and `TERM=dumb`.
- The mode is resolved once at startup, before the first result.

The config file accepts `--color auto` and `--color never`, but not `always`: a silent global default must not put escape codes into every pipe and break path-consuming scripts. `always` in the config file is a startup error. A missing or invalid value reports:

```text
zfind: expected auto, always or never after --color
```

A config error carries the usual location prefix:

```text
zfind: /home/user/.config/zfind/config:3: --color always is not allowed in the config file (use auto or never)
```

### Result Limiting

- `--limit N` (or `--limit=N`) halts output after emitting at most `N` results.
- `--limit none` (or `--limit=none`) means no limit: it is the default, and it
  also cancels a limit set in the config file. `N` is a whole number up to
  18446744073709551615, and `none` is lowercase.
- Setting `--limit 0` writes nothing and exits immediately.
- The output with `--limit N` is an exact byte-for-byte prefix of an unlimited search run.
- `--limit` consumes the next token greedily as its value.

### Basename Exclusions (`--exclude-name`)

- `--exclude-name NAME` rejects any entry whose basename equals `NAME` exactly.
- Matches are case-sensitive and bytewise; they are not affected by `--case-sensitive` or `--case-insensitive`.
- Globs, regexes, Unicode normalization, and backslash escape sequences are **not** interpreted.
- `NAME` must be non-empty and must not contain `/`.
- If a directory matches `NAME`, it is neither printed nor traversed.
- Exclusions override `.gitignore` negation patterns (`!`).
- You can specify up to 64 unique `--exclude-name` arguments across the configuration file and command line.

---

## Gitignore Support

By default, `.gitignore` files are ignored, and `.git/` and `.jj/` are traversed like normal directories.

Passing `--gitignore` activates Git-style ignore filtering in Git and Jujutsu repositories with the following rules:

1. **Repository Root Requirement:** `--gitignore` activates if the **current working directory** contains a `.git` or `.jj` entry. If both are present, ignore processing is enabled once. `zfind` does not search parent directories for a repository root.
2. **Metadata Directories:** When `--gitignore` is enabled for a recognized repository, `.git` and `.jj` are never listed, traversed, or counted, even if `--hidden` is passed.
3. **Hierarchical Traversal:** `.gitignore` files are read dynamically as the directory tree is walked. Rules encountered deeper in the filesystem hierarchy override inherited parent rules.
4. **Limits:**
   - Maximum `.gitignore` file size: **65,535 bytes**.
   - Maximum number of active rules kept per directory: **1,024 rules**.
5. **Supported Patterns:**
   - Negation prefixes (`!`)
   - Wildcards: `*` and `?`
   - Anchored paths: `/pattern`
   - Directory-only patterns: `pattern/`
   - Leading and trailing glob stars: `**/foo` and `foo/**`
6. **Unsupported Features:**
   - Character classes (e.g. `[a-z]`)
   - Backslash escape sequences (e.g. `\#` or `\*`)
   - Global ignore files (`~/.config/git/ignore`), repo-level `.git/info/exclude`, or `.ignore` files

> [!NOTE]
> `--exclude-name` takes absolute precedence over `.gitignore`. If a directory is excluded via `--exclude-name`, any `.gitignore` file inside it is never read.

---

## Configuration

`zfind` can load persistent default flags from a configuration file. Command-line options automatically take precedence over configured settings.

### File Resolution

`zfind` selects at most one configuration file, evaluated in the following priority:

1. `--config PATH` (relative paths resolve against the current working directory). The file must exist.
2. `$XDG_CONFIG_HOME/zfind/config` (if `XDG_CONFIG_HOME` is set to a non-empty absolute path).
3. `$HOME/.config/zfind/config` (if `HOME` is set and non-empty).

If neither environment variable is set or the default file does not exist, `zfind` starts with internal defaults.

- `--no-config` forces `zfind` to ignore all configuration files.
- `--help` never parses configuration files.
- `zfind` will exit with an error before searching if a configuration file exceeds 65,536 bytes, cannot be read, or contains invalid syntax:
  ```text
  zfind: /home/user/.config/zfind/config:3: unknown option
  ```

### Format & Syntax

The configuration file uses a line-oriented format matching command-line option syntax:

```ini
# ~/.config/zfind/config

# Matching & Traversal
--case-insensitive
--no-hidden
--gitignore

# Default Exclusions
--exclude-name=node_modules
--exclude-name=.venv
--exclude-name="build output"

# Formatting
--separator='\n'
--limit 500
--color auto
```

- **Lines:** Each non-blank line represents one option: `--name`, `--name=value`, or `--name value`.
- **Comments:** Lines starting with `#` (ignoring leading whitespace) are comments. Inline comments are not supported (`#` elsewhere is treated as part of the value).
- **Quoting:** Values may be enclosed in single (`'...'`) or double (`"..."`) quotes to preserve leading or trailing whitespace. The enclosing quote character cannot appear inside the string.
- **Escapes & Expansions:** Shell variables (`$VAR`), tilde (`~`), globs, and command substitutions are not expanded. Backslashes are passed directly to the corresponding option handler (e.g. `--separator='\t'`).
- **Disallowed Options:** Short options, `PATTERN`, `--`, `--help`, `--config`, and `--no-config` are forbidden inside the configuration file. `--color always` is also rejected (use `auto` or `never`).

### Precedence & Merging

Evaluation flows from **Built-in Defaults** $\rightarrow$ **Config File** $\rightarrow$ **Command Line**.

| Option Class | Behavior | Override Rule |
| :--- | :--- | :--- |
| **Modes**<br>`--case-sensitive` / `--case-insensitive`<br>`--hidden` / `--no-hidden`<br>`--gitignore` / `--no-gitignore` | CLI overrides config. | Conflicting flags within the same source produce an error. Setting a mode on the CLI replaces the config mode. Note: If a config file specifies a case flag, there is no flag to revert back to smart case from the CLI. |
| **Scalars**<br>`--separator`, `--limit`, `--color` | Last-write wins. | CLI overwrites config file values. A later `--limit N` replaces an earlier limit, and `--limit none` cancels a configured one (for example `--limit none` over `--limit 200` in the file). `--limit 0` prints nothing. The config file allows only `--color auto` and `--color never`; `always` there is an error. |
| **Type filter**<br>`--type` | Command-line set replaces the file's. | If the command line gives any `--type`, the file's `--type` values are ignored for that run; otherwise the file's apply. Repeated values within one source accumulate, so `--type f --type d` selects both. |
| **Exclusions**<br>`--exclude-name` | Cumulative set union. | Command-line values append to config file entries. Duplicate items are deduplicated. The total combined unique `--exclude-name` entries must not exceed 64. Use `--no-config` to discard configured exclusions. |

---

## Release Workflow

To create a new release:

1. Ensure all changes are merged to `main` and all CI tests pass.
2. Create and push an annotated git tag:
   ```sh
   git tag -a v1.0.0 -m "v1.0.0"
   git push origin v1.0.0
   ```
3. The automated CI tag workflow will build the project, run the test suite, and generate a GitHub Release containing the `zfind-linux-x86_64-v3` artifact.

## Disclaimer

This project makes heavy use of LLM-based coding agents. A human set the ideas, goals, architecture, and review criteria; AI agents did the bulk of the implementation and experimentation. All contributions are reviewed by a human before merge.
