#!/usr/bin/env python3
"""mktrees.py — fixed-shape testdata generator for zfind.

Builds disposable directory trees in testdata/ that pin down zfind's known
cost structure: one huge directory with tied sort keys (main), per-directory
sort with a wide dispatch fanout (tree2), long equal-prefix names (tree3),
two-tier name styles in one directory (camera), .gitignore probing (repo),
recursion depth plus symlink edge classes (deep), and a three-file smoke
tree (small).

Four more trees are correctness fixtures, not cost shapes: tiny (the smallest
tree with a directory in it, checkable by eye), hidden (dot entries, including a
dot directory with entries inside), ignores (a .gitignore tree whose ignored
entries sort before and between the visible ones) and weird (names with
newlines, option-like names, non-ASCII and invalid UTF-8 bytes). They are fixed,
seedless and the same at every preset; each builder's docstring writes down what
zfind lists for it.

Usable as a script or as a module (a test suite imports it): generate() makes
trees and returns one result per tree, verify_tree() checks a tree against its
manifest, read_manifest() and remove_tree() round it out, and main() is only a
thin command-line wrapper. Importing has no side effects.

Contract:
- Trees are shaped by three named presets (smoke/default/stress), not size
  knobs. Sizes never derive from anything outside the PRESETS table. The
  fixtures tiny, ignores and weird have no sizes and ignore the preset.
- Existing tree directories are kept unchanged and reported as such; this
  tool never updates or regenerates them. Ordinary generation failures clean
  up the current partial tree, without recursion, so the 1200-deep fixture
  removes cleanly. A hard interruption (SIGKILL, power loss) cannot clean up,
  but it is detected: see the manifest below.
- Each tree has a sidecar manifest, <out>/<tree>.manifest.json, next to the tree
  and never inside it (zfind would list it, so run zfind in a tree, not in
  <out>). It is written last, and while a tree is being made it says
  "incomplete": the next run reports an interrupted tree and exits 1 instead of
  keeping it. A complete manifest records the tree shape version, preset, seed,
  Python version, donor (repo), entry counts and a SHA-256 fingerprint of every
  path, kind, symlink target and file content. It is a fingerprint, not an
  oracle: it says what was generated, not what zfind prints for it (that depends
  on zfind's flags and belongs in the test model). --verify, or verify_tree(),
  re-reads a tree and compares it with its manifest. Trees made before
  manifests existed are kept as before and reported as having none; kept trees
  made with a different preset or tree shape version are reported too.
- Ordinary files are zero-byte; generated .gitignore files contain the rules
  that the repo workload is intended to exercise (the ignores tree's own are
  spelled out in its builder). Entry counts include files,
  directories and symlinks below a tree root, with the root excluded.
- Creation order is shuffled within each directory from the tree's seed so
  sibling order on tmpfs does not flatter zfind's per-directory sort.
- Seeded random, versioned: determinism holds for a fixed Python version and
  the same donor directory shape. Python version and seeds are printed and
  recorded in the manifest as provenance.
- repo mirrors the --like donor's real-directory shape (default /usr); nested
  symlinked directories are excluded and donor files are ignored. A donor root
  may itself be a symlink. Donor shape affects the tree, and counts under a
  different donor are not comparable. Generated filenames avoid collisions
  with donor subdirectories.
- Patterns such as `file_00007`, `Item`, `access.log.1` match these trees at
  counts that follow arithmetically from the name formats and preset numbers;
  never hard code a count without that arithmetic or a live count.
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time

# Tree order is also the generation order and the message order.
TREE_NAMES = ("main", "tree2", "tree3", "camera", "repo", "deep", "small",
              "tiny", "ignores", "weird", "hidden")

# Seeds. deep, small, tiny, ignores, weird and hidden are seedless: no random draws,
# fixed names, natural creation order.
TREE_SEEDS = {"main": 1, "tree2": 5, "tree3": 11, "camera": 2, "repo": 4}

# Bump SHAPE_VERSION whenever any tree's names, counts or contents would change
# for the same preset and seed; manifests record it so a kept tree from an older
# shape is reported. MANIFEST_SCHEMA versions the manifest file itself.
SHAPE_VERSION = 1
MANIFEST_SCHEMA = 1
MANIFEST_SUFFIX = ".manifest.json"

# Trees that ignore the preset: their manifests stay valid under any preset.
PRESET_INDEPENDENT = frozenset(("small", "tiny", "ignores", "weird", "hidden"))

# Each preset is a complete list of numbers.
# Repo: 2-10 files per dir and a 2% .gitignore chance at every preset; the
# donor walk is capped (only smoke caps it).
PRESETS = {
    "smoke": {
        # main: 12 scaffold dirs x 1 leaf, 6000 files in big/
        "main_big": 6000, "main_dirs": 12, "main_leaves": 1,
        "t2_dirs": 40, "t2_huge": 600, "t2_other": (0, 1, 3, 40, 150),
        "t2_subdirs": 10, "t2_gmax": 60,
        "t3_dirs": 20, "t3_files": 500,
        "cam_img": 5000, "cam_log": 2000,
        "repo_cap": 200,
        "deep": 300,
    },
    "default": {
        # main: 40 x 5 leaves, 60000 files in big/
        "main_big": 60000, "main_dirs": 40, "main_leaves": 5,
        "t2_dirs": 300, "t2_huge": 6000,
        "t2_other": (0, 1, 3, 40, 500, 3000),
        "t2_subdirs": 30, "t2_gmax": 60,
        "t3_dirs": 100, "t3_files": 2000,
        "cam_img": 40000, "cam_log": 10000,
        "repo_cap": None,
        "deep": 1200,
    },
    "stress": {
        # main: 63 x 7 leaves, 150000 files in big/
        "main_big": 150000, "main_dirs": 63, "main_leaves": 7,
        "t2_dirs": 300, "t2_huge": 7500,
        "t2_other": (0, 2, 8, 100, 1250),
        "t2_subdirs": 30, "t2_gmax": 150,
        "t3_dirs": 100, "t3_files": 5000,
        "cam_img": 100000, "cam_log": 25000,
        "repo_cap": None,
        "deep": 1200,
    },
}

# Entry kinds handed to create_tree(): ("d" dir, "f" file, "l" symlink).
# The fourth field is file content for files (None means empty) and the target
# for symlinks.
DIR, FILE, LINK = "d", "f", "l"

# Directory traversal during creation is iterative, so deep fixtures do not
# require changing Python's process-wide recursion limit.


def gen_main(rng, cfg):
    """main: big/ file_%06d_%s + scaffold of 30 Item*.log + 30 .hidden*
    files per leaf dir. The Item*.log / .hidden* pairing is the
    hidden-file-skipping wedge."""
    ents = [("", "big", DIR, None)]
    for i in range(cfg["main_big"]):
        ents.append(("big", "file_%06d_%s" % (i, "x" * rng.randint(0, 40)), FILE, None))
    for s in range(cfg["main_dirs"]):
        base = "dir%02d" % s
        ents.append(("", base, DIR, None))
        for t in range(cfg["main_leaves"]):
            leaf = base + "/leaf%02d" % t
            ents.append((base, "leaf%02d" % t, DIR, None))
            for j in range(30):
                ents.append((leaf, "Item%03d.log" % j, FILE, None))
            for j in range(30):
                ents.append((leaf, ".hidden%03d" % j, FILE, None))
    return ents


def gen_tree2(rng, cfg):
    """tree2: d%02d top dirs. Every 7th dir is huge, the rest draw their file
    count from a steep mix; every 10th dir also gets sub%02d subdirs holding
    up to 60/150 g%03d.txt files. d=0 is both (%7 and %10)."""
    ents = []
    for d in range(cfg["t2_dirs"]):
        base = "d%02d" % d
        ents.append(("", base, DIR, None))
        nfiles = cfg["t2_huge"] if d % 7 == 0 else rng.choice(cfg["t2_other"])
        if d % 10 == 0:
            for j in range(cfg["t2_subdirs"]):
                sub = base + "/sub%02d" % j
                ents.append((base, "sub%02d" % j, DIR, None))
                for g in range(rng.randint(0, cfg["t2_gmax"])):
                    ents.append((sub, "g%03d.txt" % g, FILE, None))
        for i in range(nfiles):
            ents.append((base, "f%05d_%s" % (i, "n" * rng.randint(0, 120)), FILE, None))
    return ents


def gen_tree3(rng, cfg):
    """tree3: dir%02d x file_%05d_ (abcdefghij * 14..19): 151..201-byte names,
    long repeated suffixes on top of a shared `file_0` opening."""
    ents = []
    for d in range(cfg["t3_dirs"]):
        base = "dir%02d" % d
        ents.append(("", base, DIR, None))
        for i in range(cfg["t3_files"]):
            suffix = "abcdefghij" * rng.randint(14, 19)
            ents.append((base, "file_%05d_%s" % (i, suffix), FILE, None))
    return ents


def gen_camera(rng, cfg):
    """camera: IMG_2024%02d%02d_%06d.jpg (sequence = i, unique by
    construction) + access.log.%d, two name tiers in one flat directory."""
    ents = []
    for i in range(cfg["cam_img"]):
        name = "IMG_2024%02d%02d_%06d.jpg" % (rng.randint(1, 12), rng.randint(1, 31), i)
        ents.append(("", name, FILE, None))
    for i in range(cfg["cam_log"]):
        ents.append(("", "access.log.%d" % i, FILE, None))
    return ents


def _walk_error(error):
    """Make os.walk traversal failures visible instead of silently skipping dirs."""
    raise error


def gen_repo(rng, cfg, donor):
    """repo: mirror donor directories (not donor files); add generated files,
    a root .git directory, and .gitignore rules with 2% probability. Generated
    names avoid existing child-directory names. A donor root .git is omitted
    because the fixture supplies its own repository marker."""
    ents = []
    cap = cfg["repo_cap"]
    mirrored = 0

    for dpath, dirnames, _files in os.walk(donor, topdown=True, onerror=_walk_error):
        rel_native = os.path.relpath(dpath, donor)
        rel = "" if rel_native == "." else rel_native.replace(os.sep, "/")

        # Stable traversal gives the same seeded random decisions for the same
        # donor tree. Reserve the root .git name for the synthetic repo marker.
        # os.walk does not follow directory symlinks by default, so exclude
        # them explicitly rather than silently omitting them from the mirror.
        dirnames[:] = sorted(
            name for name in dirnames
            if not (rel == "" and name == ".git")
            and not os.path.islink(os.path.join(dpath, name))
        )

        if cap is not None and rel != "" and mirrored >= cap:
            break  # cap counts visited dirs, with the donor root included

        child_dirs = set(dirnames)
        nfiles = rng.randint(2, 10)
        wants_ignore = rng.random() < 0.02

        if rel == "":
            ents.append(("", ".git", DIR, None))
        else:
            parent, name = rel.rsplit("/", 1) if "/" in rel else ("", rel)
            ents.append((parent, name, DIR, None))

        # A child directory named .gitignore cannot coexist with a file of
        # that name; in that rare case preserve the donor directory.
        has_ignore_file = wants_ignore and ".gitignore" not in child_dirs
        if has_ignore_file:
            ents.append((rel, ".gitignore", FILE, "*.o\nbuild/\n"))

        # Keep the requested 2-10 generated files, skipping any file name that
        # would shadow a donor child directory. Candidate indices may therefore
        # be greater than nfiles - 1 in unusual donor trees.
        added = 0
        candidate = 0
        while added < nfiles:
            name = "file%d.c" % candidate
            candidate += 1
            if name in child_dirs:
                continue
            ents.append((rel, name, FILE, None))
            added += 1

        mirrored += 1

    return ents


def gen_deep(_rng, cfg):
    """deep: an exact chain of d dirs (one name at every level), leaf.txt at
    the bottom, plus emptydir / loop -> .. / dangling -> nowhere-gone at the
    root (the symlink edge classes). Seedless."""
    ents = []
    parent = ""
    for _ in range(cfg["deep"]):
        ents.append((parent, "d", DIR, None))
        parent = parent + "/d" if parent else "d"
    ents.append((parent, "leaf.txt", FILE, None))
    ents.append(("", "emptydir", DIR, None))
    ents.append(("", "loop", LINK, ".."))
    ents.append(("", "dangling", LINK, "nowhere-gone"))
    return ents


def gen_small(_rng, _cfg):
    """small: exactly f1, f2 and top at the root. Seedless."""
    return [("", "f1", FILE, None), ("", "f2", FILE, None), ("", "top", FILE, None)]


def gen_tiny(_rng, _cfg):
    """tiny: five files and one directory, the smallest tree where the order of
    a directory against its contents and against its siblings shows. zfind lists

        a  b  ca  cb  dir/  dir/c  dir/d  e

    (8 entries; pattern `c` lists ca, cb, dir/c). Seedless."""
    ents = [("", name, FILE, None) for name in ("a", "b", "ca", "cb", "e")]
    ents.append(("", "dir", DIR, None))
    ents.append(("dir", "c", FILE, None))
    ents.append(("dir", "d", FILE, None))
    return ents


# The rules below use only what zfind implements: '*', directory-only 'name/',
# a leading '/' anchor, '!' negation, comments, blank lines and a nested
# .gitignore (no [] or backslash escapes, no parent or global files).
IGNORES_ROOT = (
    "# build products\n"
    "*.o\n"
    "build/\n"
    "\n"
    "/skipme\n"
    "tmp*\n"
    "!tmp_keep\n"
)
IGNORES_SUB = "*.log\n!keep.log\nlocal/\n"


def gen_ignores(_rng, _cfg):
    """ignores: a repository (empty .git) whose ignored entries sort before and
    between the visible ones at every level, so a count taken before the
    ignore rules shows up as the wrong entry at the cap. Root rules: *.o,
    build/, /skipme, tmp*, !tmp_keep. sub/.gitignore: *.log, !keep.log, local/.

    zfind lists, in this order (9 entries):

        a.txt  c.txt  sub/  sub/build  sub/keep.log  sub/skipme  sub/z.txt
        tmp_keep  z.txt

    Never listed: the hidden .git, .gitignore (root and sub) and .hidden;
    a.o, c.o, sub/x.o (*.o); build/ with its 4 entries below (a directory
    rule); skipme (anchored, so sub/skipme stays); tmp1 and tmp_other (tmp*,
    while !tmp_keep brings tmp_keep back); sub/noise.log (*.log, while
    !keep.log brings keep.log back); sub/local/ and its l1 (a directory rule
    from the nested file). sub/build is a file, which the directory rule
    build/ must not match. 27 entries are created. Seedless."""
    return [
        ("", ".git", DIR, None),
        ("", ".gitignore", FILE, IGNORES_ROOT),
        ("", ".hidden", FILE, None),
        ("", "a.o", FILE, None),
        ("", "a.txt", FILE, None),
        ("", "build", DIR, None),
        ("build", "b1", FILE, None),
        ("build", "b2", FILE, None),
        ("build", "inner", DIR, None),
        ("build/inner", "b3", FILE, None),
        ("", "c.o", FILE, None),
        ("", "c.txt", FILE, None),
        ("", "skipme", FILE, None),
        ("", "sub", DIR, None),
        ("sub", ".gitignore", FILE, IGNORES_SUB),
        ("sub", "build", FILE, None),
        ("sub", "keep.log", FILE, None),
        ("sub", "local", DIR, None),
        ("sub/local", "l1", FILE, None),
        ("sub", "noise.log", FILE, None),
        ("sub", "skipme", FILE, None),
        ("sub", "x.o", FILE, None),
        ("sub", "z.txt", FILE, None),
        ("", "tmp1", FILE, None),
        ("", "tmp_keep", FILE, None),
        ("", "tmp_other", FILE, None),
        ("", "z.txt", FILE, None),
    ]


def _raw(data):
    """A name that must reach the disk as exactly these bytes in any locale:
    os.fsdecode() round-trips through os.fsencode() (surrogateescape)."""
    return os.fsdecode(data)


def gen_hidden(_rng, _cfg):
    """hidden: dot entries in every shape --no-hidden has to handle: a dot file,
    a dot directory with a visible-looking file and a dot file inside, and a dot
    file inside a visible directory. zfind lists, in this order (8 entries):

        .dotdir/  .dotdir/.deep  .dotdir/inside  .dotfile  sub/  sub/.dot2
        sub/visible2  visible

    and with --no-hidden only: sub/  sub/visible2  visible. Seedless."""
    return [
        ("", "visible", FILE, None),
        ("", ".dotfile", FILE, None),
        ("", ".dotdir", DIR, None),
        (".dotdir", "inside", FILE, None),
        (".dotdir", ".deep", FILE, None),
        ("", "sub", DIR, None),
        ("sub", "visible2", FILE, None),
        ("sub", ".dot2", FILE, None),
    ]


def gen_weird(_rng, _cfg):
    """weird: names that trip line-oriented and option-parsing assumptions.
    Option-like names (-0, -h, -t, --limit), a newline inside a file name and
    inside a directory name, glob characters and a backslash (literal here),
    a space, a non-ASCII name (UTF-8 bytes) and an invalid UTF-8 one. Linux
    file names are bytes, so the last two are written as exact bytes whatever
    the locale. zfind lists, in this order (14 entries, name bytes shown as
    Python literals):

        --limit  -0  -h  -t  a*b  back\\slash  c?d  caf\\xc3\\xa9  hi\\xff
        q\\nr/  q\\nr/s  sp ace  x\\ny  z

    With newline output the stream has 17 separators for 14 entries, so
    --limit 10 ends after the `q` of `q\\nr/`; with -0 the count is exact.
    Seedless."""
    ents = [("", name, FILE, None) for name in (
        "-0", "-h", "-t", "--limit", "x\ny", "z", "sp ace",
        "a*b", "c?d", "back\\slash",
        _raw(b"caf\xc3\xa9"), _raw(b"hi\xff"),
    )]
    ents.append(("", "q\nr", DIR, None))
    ents.append(("q\nr", "s", FILE, None))
    return ents


BUILDERS = {
    "main": gen_main,
    "tree2": gen_tree2,
    "tree3": gen_tree3,
    "camera": gen_camera,
    "deep": gen_deep,
    "small": gen_small,
    "tiny": gen_tiny,
    "ignores": gen_ignores,
    "weird": gen_weird,
    "hidden": gen_hidden,
}


def pick_entries(tree, rng, cfg, donor):
    if tree == "repo":
        return gen_repo(rng, cfg, donor)
    return BUILDERS[tree](rng, cfg)


def _entry_path(parent, name):
    return parent + "/" + name if parent else name


def validate_entries(ents):
    """Fail before filesystem mutation if an entry plan is ambiguous or incomplete."""
    occupied = set()
    dirs = set()

    for entry in ents:
        if len(entry) != 4:
            raise ValueError("invalid entry (expected 4 fields): %r" % (entry,))
        parent, name, kind, payload = entry
        if kind not in (DIR, FILE, LINK):
            raise ValueError("unknown entry kind %r for %r" % (kind, name))
        if not isinstance(parent, str) or not isinstance(name, str):
            raise ValueError("entry parent and name must be strings: %r" % (entry,))
        # Parent paths in the entry plan are normalized to '/' separators.
        # Use the platform's actual separators for leaf names: on POSIX a
        # backslash is a valid filename character, while on Windows it is not.
        if (not name or name in (".", "..") or os.sep in name
                or (os.altsep and os.altsep in name)):
            raise ValueError("invalid entry name %r" % name)
        if parent.startswith("/"):
            raise ValueError("invalid parent path %r" % parent)
        if any(part in ("", ".", "..") for part in parent.split("/") if parent):
            raise ValueError("invalid parent path %r" % parent)
        key = (parent, name)
        if key in occupied:
            raise ValueError("duplicate path in generated tree: %s" % _entry_path(parent, name))
        occupied.add(key)
        if kind == DIR:
            dirs.add(_entry_path(parent, name))
        elif kind == LINK and not isinstance(payload, str):
            raise ValueError("symlink %r has no string target" % name)
        elif kind == FILE and payload is not None and not isinstance(payload, str):
            raise ValueError("file content for %r must be text or None" % name)

    for parent, _name, _kind, _payload in ents:
        if not parent:
            continue
        parts = parent.split("/")
        for i in range(1, len(parts) + 1):
            ancestor = "/".join(parts[:i])
            if ancestor not in dirs:
                raise ValueError("parent directory missing from entry plan: %s" % ancestor)


def create_tree(root, ents, rng):
    """Create a validated tree using an explicit stack, cleaning up on failure.

    The iterative DFS preserves the previous creation order: each directory is
    created and fully visited before the next sibling is created. This avoids
    Python recursion limits for the deep fixture. If an ordinary exception or
    a catchable interruption occurs after the root is created, remove the
    partial tree before propagating the original exception.
    """
    validate_entries(ents)
    by_parent = {}
    for parent, name, kind, payload in ents:
        by_parent.setdefault(parent, []).append((name, kind, payload))

    # exist_ok=False avoids merging with a tree made by another process after
    # main() checked the path. Do this before the cleanup-protected section so
    # a concurrent process's existing target is never deleted on this failure.
    os.makedirs(root, exist_ok=False)
    created = 0

    try:
        root_children = by_parent.pop("", [])
        if rng is not None:
            rng.shuffle(root_children)
        stack = [("", iter(root_children))]

        while stack:
            parent, children = stack[-1]
            try:
                name, kind, payload = next(children)
            except StopIteration:
                stack.pop()
                continue

            here = os.path.join(root, parent)
            path = os.path.join(here, name)
            child_parent = _entry_path(parent, name)

            if kind == DIR:
                os.mkdir(path)
                created += 1
                grandchildren = by_parent.pop(child_parent, None)
                if grandchildren:
                    if rng is not None:
                        rng.shuffle(grandchildren)
                    stack.append((child_parent, iter(grandchildren)))
            elif kind == FILE:
                with open(path, "wb") as handle:
                    if payload is not None:
                        handle.write(payload.encode("utf-8"))
                created += 1
            else:
                os.symlink(payload, path)
                created += 1

        if by_parent:
            # Validation should make this unreachable; retain a guard so a
            # code change cannot silently report success with uncreated paths.
            raise ValueError(
                "entry plan contains unreachable paths: %s"
                % ", ".join(sorted(by_parent)[:5])
            )
        return created
    except BaseException:
        # Do not let cleanup errors mask the generation failure. A hard kill
        # (for example SIGKILL or power loss) cannot run this cleanup; those
        # partial trees must still be deleted manually before rerunning.
        try:
            remove_tree(root)
        except OSError as cleanup_error:
            print("mktrees: warning: could not clean partial tree %s: %s"
                  % (root, cleanup_error), file=sys.stderr)
        raise


def remove_tree(path):
    """Delete a directory tree without recursion. shutil.rmtree recurses once per
    level and raises RecursionError on the 1200-deep fixture at the default
    preset (RecursionError is not an OSError, so it would also slip past the
    handlers here). Symlinks are unlinked, never followed."""
    if os.path.islink(path):
        os.unlink(path)
        return
    directories = []
    stack = [os.fsencode(path)]
    while stack:
        here = stack.pop()
        directories.append(here)
        with os.scandir(here) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    os.unlink(entry.path)
    # A directory is always listed after its parent, so reversed order removes
    # children first.
    for here in reversed(directories):
        os.rmdir(here)


class MktreesError(Exception):
    """A usage or state problem the command line reports without a traceback.
    .code is the exit status main() returns for it."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def tree_path(out, tree):
    return os.path.join(out, tree)


def manifest_path(out, tree):
    return os.path.join(out, tree + MANIFEST_SUFFIX)


def read_manifest(out, tree):
    """The tree's manifest as a dict, or None if it has none (a tree made by an
    older mktrees, or never made). An unreadable manifest raises ValueError."""
    path = manifest_path(out, tree)
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ValueError("unreadable manifest %s: %s" % (path, exc)) from None


def _write_manifest(out, tree, data):
    path = manifest_path(out, tree)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        _unlink_quietly(temporary)
        raise


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# The fingerprint covers (path, kind, payload) for every entry: payload is the
# symlink target for a link, the content for a file (empty for the usual
# zero-byte file) and empty for a directory. Paths are bytes relative to the
# tree root, so names that are not valid UTF-8 or contain newlines are exact.

def _plan_records(ents):
    for parent, name, kind, payload in ents:
        path = os.fsencode(_entry_path(parent, name))
        if kind == DIR:
            yield path, b"d", b""
        elif kind == LINK:
            yield path, b"l", os.fsencode(payload)
        else:
            yield path, b"f", payload.encode("utf-8") if payload else b""


def _disk_records(root):
    """Every entry below root as (path, kind, payload), without recursion and
    without following symlinks. Kind ? is anything that is not a directory,
    file or symlink."""
    stack = [(os.fsencode(root), b"")]
    while stack:
        here, relative = stack.pop()
        with os.scandir(here) as entries:
            for entry in entries:
                path = relative + b"/" + entry.name if relative else entry.name
                if entry.is_symlink():
                    yield path, b"l", os.readlink(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    yield path, b"d", b""
                    stack.append((entry.path, path))
                elif entry.is_file(follow_symlinks=False):
                    payload = b""
                    if entry.stat(follow_symlinks=False).st_size:
                        with open(entry.path, "rb") as handle:
                            payload = handle.read()
                    yield path, b"f", payload
                else:
                    yield path, b"?", b""


def _summarize(records):
    """(entry counts, fingerprint) for an iterable of (path, kind, payload)."""
    counts = {b"d": 0, b"f": 0, b"l": 0}
    total = 0
    digest = hashlib.sha256()
    for path, kind, payload in sorted(records):
        total += 1
        if kind in counts:
            counts[kind] += 1
        digest.update(kind + b"\0" + path + b"\0" + b"%d" % len(payload)
                      + b"\0" + payload + b"\n")
    entries = {"total": total, "dirs": counts[b"d"], "files": counts[b"f"],
               "symlinks": counts[b"l"]}
    return entries, "sha256:" + digest.hexdigest()


def _manifest_for(tree, preset, seed, like, ents):
    entries, fingerprint = _summarize(_plan_records(ents))
    data = {
        "schema": MANIFEST_SCHEMA,
        "shape_version": SHAPE_VERSION,
        "status": "complete",
        "tree": tree,
        "preset": preset,
        "preset_dependent": tree not in PRESET_INDEPENDENT,
        "seed": seed,
        "python": sys.version.split()[0],
        "entries": entries,
        "fingerprint": fingerprint,
    }
    if tree == "repo":
        # The plan includes one synthetic .git directory; subtract it to count
        # the donor subdirectories mirrored into the fixture.
        data["donor"] = like
        data["donor_dirs"] = sum(1 for entry in ents if entry[2] == DIR) - 1
    return data


def verify_tree(out, tree, preset=None):
    """Compare a tree on disk with its manifest. Returns a list of problems,
    empty when the tree is exactly what this mktrees made. preset, if given, is
    checked for preset-dependent trees."""
    target = tree_path(out, tree)
    if not os.path.isdir(target) or os.path.islink(target):
        return ["tree directory not found: %s" % target]
    manifest = read_manifest(out, tree)
    if manifest is None:
        return ["no manifest (made by an older mktrees, or not by mktrees)"]
    if manifest.get("status") != "complete":
        return ["manifest says the tree is incomplete "
                "(an earlier run was interrupted)"]
    if manifest.get("schema") != MANIFEST_SCHEMA:
        return ["manifest schema %r, this mktrees reads %r"
                % (manifest.get("schema"), MANIFEST_SCHEMA)]
    if manifest.get("shape_version") != SHAPE_VERSION:
        return ["tree shape version %r, this mktrees makes %r"
                % (manifest.get("shape_version"), SHAPE_VERSION)]
    if (preset is not None and manifest.get("preset_dependent")
            and manifest.get("preset") != preset):
        return ["made with preset %r, not %r" % (manifest.get("preset"), preset)]
    entries, fingerprint = _summarize(_disk_records(target))
    if entries != manifest.get("entries") or fingerprint != manifest.get("fingerprint"):
        want = manifest.get("entries") or {}
        return ["tree differs from its manifest (disk: %d entries, %d dirs, "
                "%d files, %d symlinks; manifest: %s entries, %s dirs, "
                "%s files, %s symlinks)"
                % (entries["total"], entries["dirs"], entries["files"],
                   entries["symlinks"], want.get("total"), want.get("dirs"),
                   want.get("files"), want.get("symlinks"))]
    return []


def _select(only):
    if only:
        unknown = [name for name in only if name not in TREE_NAMES]
        if unknown:
            raise MktreesError("unknown tree name(s): %s (valid: %s)"
                               % (", ".join(unknown), ", ".join(TREE_NAMES)), 2)
        return list(dict.fromkeys(only))
    return list(TREE_NAMES)


def _staleness_notes(manifest, preset):
    if manifest is None:
        return ["no manifest: made by an older mktrees"]
    notes = []
    if manifest.get("shape_version") != SHAPE_VERSION:
        notes.append("made with tree shape version %s, this mktrees makes %s"
                     % (manifest.get("shape_version"), SHAPE_VERSION))
    if manifest.get("preset_dependent") and manifest.get("preset") != preset:
        notes.append("made with preset %s, this run asked for %s"
                     % (manifest.get("preset"), preset))
    return notes


def generate(out="testdata", preset="default", only=None, like="/usr", log=None):
    """Create the selected trees under out and return one dict per tree:

        tree, path, status ("created", "kept" or "skipped"), seed, entries (the
        count created, else None), seconds, manifest (the manifest written, or
        the existing one for a kept tree, else None), notes (why a kept tree
        may be stale).

    Existing trees are kept unchanged. log, if given, is called with each
    progress line (the command line passes print). Raises MktreesError for a
    usage or state problem (unknown tree or preset, bad output path, an
    interrupted tree), and lets OSError and ValueError from the work itself
    propagate after cleaning up the current partial tree."""
    emit = log if log is not None else (lambda message: None)
    if preset not in PRESETS:
        raise MktreesError("unknown preset %r (valid: %s)"
                           % (preset, ", ".join(PRESETS)), 2)
    selected = _select(only)
    if os.path.lexists(out) and not os.path.isdir(out):
        raise MktreesError("output path is not a directory: %s" % out)

    cfg = PRESETS[preset]
    emit("mktrees: python %s, preset %s, out %s"
         % (sys.version.split()[0], preset, out))

    results = []
    for tree in selected:
        target = tree_path(out, tree)
        seed = TREE_SEEDS.get(tree)
        result = {"tree": tree, "path": target, "status": None, "seed": seed,
                  "entries": None, "seconds": None, "manifest": None, "notes": []}
        results.append(result)

        if os.path.lexists(target):
            if not os.path.isdir(target) or os.path.islink(target):
                raise MktreesError("target exists but is not a directory: %s" % target)
            manifest = read_manifest(out, tree)
            if manifest is not None and manifest.get("status") != "complete":
                raise MktreesError(
                    "%s is incomplete: an earlier run was interrupted. "
                    "rm -rf %s %s and rerun"
                    % (tree, target, manifest_path(out, tree)))
            notes = _staleness_notes(manifest, preset)
            result.update(status="kept", manifest=manifest, notes=notes)
            emit("%-8s kept (existing tree left unchanged; "
                 "rm -rf it and rerun to regenerate)%s"
                 % (tree, " [%s]" % "; ".join(notes) if notes else ""))
            continue

        if tree == "repo" and not os.path.isdir(like):
            result["status"] = "skipped"
            emit("%-8s skipped: donor %s is not an existing directory" % (tree, like))
            continue

        rng = random.Random(seed) if seed is not None else None
        started = time.monotonic()
        ents = pick_entries(tree, rng, cfg, like)

        # The incomplete marker exists exactly while the tree might be partial:
        # it is replaced by the manifest when the tree is done, and removed if
        # generation fails and the tree is cleaned up. If the tree is still
        # there after a failure (a concurrent run owns it, or cleanup failed) the
        # marker stays so the next run reports it.
        if not os.path.isdir(out):
            os.makedirs(out, exist_ok=True)
        _write_manifest(out, tree, {"schema": MANIFEST_SCHEMA,
                                    "shape_version": SHAPE_VERSION,
                                    "status": "incomplete", "tree": tree})
        try:
            created = create_tree(target, ents, rng)
        except BaseException:
            if not os.path.lexists(target):
                _unlink_quietly(manifest_path(out, tree))
            raise
        seconds = time.monotonic() - started

        manifest = _manifest_for(tree, preset, seed, like, ents)
        if created != manifest["entries"]["total"]:
            raise ValueError("%s: created %d entries but the plan has %d"
                             % (tree, created, manifest["entries"]["total"]))
        _write_manifest(out, tree, manifest)
        result.update(status="created", entries=created, seconds=seconds,
                      manifest=manifest)

        detail = ""
        if tree == "repo":
            detail = " (%d donor dirs)" % manifest["donor_dirs"]
        emit("%-8s seed=%-3s %8d entries%s %.2fs" % (
            tree, seed if seed is not None else "-", created, detail, seconds))
    return results


def _verify_cli(args):
    selected = _select(args.only)
    checked = bad = 0
    for tree in selected:
        if not args.only and not os.path.lexists(tree_path(args.out, tree)):
            continue    # not generated, and not asked for by name
        checked += 1
        problems = verify_tree(args.out, tree, args.preset)
        if problems:
            bad += 1
            print("%-8s PROBLEM: %s" % (tree, "; ".join(problems)))
        else:
            manifest = read_manifest(args.out, tree)
            print("%-8s ok (%d entries, preset %s)"
                  % (tree, manifest["entries"]["total"], manifest["preset"]))
    if not checked:
        print("mktrees: no trees to verify in %s" % args.out, file=sys.stderr)
        return 1
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mktrees.py",
        description="Generate fixed-shape testdata trees for zfind.",
    )
    ap.add_argument("--out", default="testdata", metavar="DIR",
                    help="target directory, trees land in DIR/NAME (default: testdata)")
    ap.add_argument("--preset", choices=tuple(PRESETS), default=None,
                    help="smoke|default|stress (default: default; with --verify "
                         "it is checked only when given)")
    ap.add_argument("--only", nargs="*", metavar="NAME",
                    help="generate only these trees; a bare --only generates "
                         "all; default: all of %s" % ", ".join(TREE_NAMES))
    ap.add_argument("--like", default="/usr", metavar="PATH",
                    help="donor directory for repo's skeleton (default: /usr)")
    ap.add_argument("--verify", action="store_true",
                    help="do not generate: compare existing trees with their "
                         "manifests and exit 1 on any problem")
    args = ap.parse_args(argv)

    try:
        if args.verify:
            return _verify_cli(args)
        generate(args.out, args.preset or "default", args.only, args.like, log=print)
    except MktreesError as exc:
        print("mktrees: %s" % exc, file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print("mktrees: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
