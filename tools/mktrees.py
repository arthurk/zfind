#!/usr/bin/env python3
"""mktrees.py — fixed-shape testdata generator for zfind.

Builds disposable directory trees in testdata/ that pin down zfind's known
cost structure: one huge directory with tied sort keys (main), per-directory
sort with a wide dispatch fanout (tree2), long equal-prefix names (tree3),
two-tier name styles in one directory (camera), .gitignore probing (repo),
recursion depth plus symlink edge classes (deep), and a three-file smoke
tree (small).

Contract:
- Trees are shaped by three named presets (smoke/default/stress), not size
  knobs. Sizes never derive from anything outside the PRESETS table.
- Existing tree directories are kept unchanged and reported as such; this
  tool never updates or regenerates them. Ordinary generation failures clean
  up the current partial tree. A hard interruption can leave a partial tree
  that nothing detects — delete it before rerunning.
- Ordinary files are zero-byte; generated .gitignore files contain the rules
  that the repo workload is intended to exercise. Entry counts include files,
  directories and symlinks below a tree root, with the root excluded.
- Creation order is shuffled within each directory from the tree's seed so
  sibling order on tmpfs does not flatter zfind's per-directory sort.
- Seeded random, versioned: determinism holds for a fixed Python version and
  the same donor directory shape. Python version and seeds are printed as
  provenance, not a manifest.
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
import os
import random
import shutil
import sys
import time

# Tree order is also the generation order and the message order.
TREE_NAMES = ("main", "tree2", "tree3", "camera", "repo", "deep", "small")

# Seeds. deep and small are seedless: no random draws, fixed names,
# natural creation order.
TREE_SEEDS = {"main": 1, "tree2": 5, "tree3": 11, "camera": 2, "repo": 4}

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


BUILDERS = {
    "main": gen_main,
    "tree2": gen_tree2,
    "tree3": gen_tree3,
    "camera": gen_camera,
    "deep": gen_deep,
    "small": gen_small,
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
            shutil.rmtree(root)
        except OSError as cleanup_error:
            print("mktrees: warning: could not clean partial tree %s: %s"
                  % (root, cleanup_error), file=sys.stderr)
        raise


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mktrees.py",
        description="Generate fixed-shape testdata trees for zfind.",
    )
    ap.add_argument("--out", default="testdata", metavar="DIR",
                    help="target directory, trees land in DIR/NAME (default: testdata)")
    ap.add_argument("--preset", choices=tuple(PRESETS), default="default",
                    help="smoke|default|stress (default: default)")
    ap.add_argument("--only", nargs="*", metavar="NAME",
                    help="generate only these trees; a bare --only generates "
                         "all; default: all of %s" % ", ".join(TREE_NAMES))
    ap.add_argument("--like", default="/usr", metavar="PATH",
                    help="donor directory for repo's skeleton (default: /usr)")
    args = ap.parse_args(argv)

    if args.only:
        unknown = [name for name in args.only if name not in TREE_NAMES]
        if unknown:
            print("mktrees: unknown tree name(s): %s (valid: %s)"
                  % (", ".join(unknown), ", ".join(TREE_NAMES)), file=sys.stderr)
            return 2
        selected = list(dict.fromkeys(args.only))
    else:
        selected = list(TREE_NAMES)

    if os.path.lexists(args.out) and not os.path.isdir(args.out):
        print("mktrees: output path is not a directory: %s" % args.out, file=sys.stderr)
        return 1

    cfg = PRESETS[args.preset]
    print("mktrees: python %s, preset %s, out %s"
          % (sys.version.split()[0], args.preset, args.out))

    try:
        for tree in selected:
            target = os.path.join(args.out, tree)
            if os.path.lexists(target):
                if os.path.isdir(target) and not os.path.islink(target):
                    print("%-8s kept (existing tree left unchanged; "
                          "rm -rf it and rerun to regenerate)" % tree)
                    continue
                print("mktrees: target exists but is not a directory: %s" % target, file=sys.stderr)
                return 1

            seed = TREE_SEEDS.get(tree)
            rng = random.Random(seed) if seed is not None else None
            if tree == "repo" and not os.path.isdir(args.like):
                print("%-8s skipped: donor %s is not an existing directory"
                      % (tree, args.like))
                continue

            started = time.monotonic()
            ents = pick_entries(tree, rng, cfg, args.like)
            created = create_tree(target, ents, rng)
            elapsed = time.monotonic() - started

            detail = ""
            if tree == "repo":
                # The plan includes one synthetic .git directory; subtract it
                # to report donor subdirectories mirrored into the fixture.
                ndonor = sum(1 for entry in ents if entry[2] == DIR) - 1
                detail = " (%d donor dirs)" % ndonor

            print("%-8s seed=%-3s %8d entries%s %.2fs" % (
                tree, seed if seed is not None else "-", created, detail, elapsed))
    except (OSError, ValueError) as exc:
        print("mktrees: %s" % exc, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
