# zfind

Finds files and directories.

## Features

- Multi-threaded
- Respects `.gitignore`, skips hidden files and VCS dirs unless asked

## Usage

```
zfind PATTERN                       # smart-case search
zfind -0 PATTERN | xargs -0 ...     # NUL-separated output
```

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
