# zfind

Finds files and directories.

## Features

- respects `.gitignore`, skips hidden files and VCS dirs unless asked

## Usage

```
zfind PATTERN                       # smart-case search
zfind -0 PATTERN | xargs -0 ...     # NUL-separated output
```
