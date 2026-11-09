#!/usr/bin/env python3
"""Benchmark the current zfind binary with hyperfine.

    bench.py --binary BUILD/ZFIND [--pattern PATTERN] [--tree PATH]
             [--warmup N] [--runs N]

Measures the current checkout only: there is no reference binary, label or
revision comparison. On first use it generates build/bench/tree/, 200,000
empty files in a fixed layout, then runs two workloads in that tree and writes
the table to build/bench/results-<describe>.md and to stdout.

The tree is only regenerated when build/bench/tree.marker does not describe
the tree that is present. --tree PATH benchmarks an existing tree instead and
skips generation.

Self-test:  python3 -m doctest scripts/bench.py
"""
import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone

BENCH_DIR = "build/bench"
TREE = BENCH_DIR + "/tree"
MARKER = BENCH_DIR + "/tree.marker"
BENCH_HOME = BENCH_DIR + "/home"
GOVERNOR = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"

# Bump when tree_path() or the tree shape changes: the marker then mismatches
# and the old tree is deleted.
LAYOUT_VERSION = 1
FILES = 200_000
DIRS = 2_100
ENTRIES = FILES + DIRS
CONF_EVERY = 100

BASE_ARGS = ("--no-config", "--no-gitignore", "--color", "never")
WORKLOADS = ("floor", "pattern")
METRICS = (
    "time_wall_clock",
    "instructions",
    "cpu_cycles",
    "memory_peak_resident",
)
FALLBACK_METRICS = ("time_wall_clock", "memory_peak_resident")
WALL_CV_LIMIT = 0.10
INSTR_CV_LIMIT = 0.01


def tree_path(index):
    r"""Relative path of file INDEX in the benchmark tree.

    >>> tree_path(0)
    'd000/s00/file-000000-conf.txt'
    >>> tree_path(1)
    'd000/s00/file-000001.txt'
    >>> tree_path(199_999)
    'd099/s19/file-199999.txt'
    """
    directory = f"d{index // 2000:03d}/s{index % 2000 // 100:02d}"
    suffix = "-conf" if index % CONF_EVERY == 0 else ""
    return f"{directory}/file-{index:06d}{suffix}.txt"


def tree_dirs():
    """Relative paths of the tree's DIRS directories, parents before children.

    >>> dirs = tree_dirs()
    >>> len(dirs)
    2100
    >>> dirs[0], dirs[100], dirs[-1]
    ('d000', 'd000/s00', 'd099/s19')
    """
    top = [f"d{d:03d}" for d in range(100)]
    sub = [f"{parent}/s{s:02d}" for parent in top for s in range(20)]
    return top + sub


def marker_text():
    r"""Contents of the marker file the tree layout is checked against.

    >>> marker_text()
    'version=1\nfiles=200000\ndirs=2100\n'
    """
    return f"version={LAYOUT_VERSION}\nfiles={FILES}\ndirs={DIRS}\n"


def workload_argv(binary, pattern, workload):
    """argv of one benchmarked zfind run; options precede PATTERN.

    >>> workload_argv("/b/zfind", "a b", "floor")
    ['/b/zfind', '--no-config', '--no-gitignore', '--color', 'never']
    >>> workload_argv("/b/zfind", "-x", "pattern")
    ['/b/zfind', '--no-config', '--no-gitignore', '--color', 'never', '--', '-x']
    >>> shlex.join(workload_argv("/b/zfind", "a b", "pattern"))
    "/b/zfind --no-config --no-gitignore --color never -- 'a b'"
    """
    argv = [binary, *BASE_ARGS]
    if workload == "pattern":
        argv += ["--", pattern]
    return argv


def hyperfine_command(exe, binary, pattern, out_json, runs, warmup, metrics=METRICS):
    """The hyperfine argv for both workloads.

    >>> hyperfine_command("hyperfine", "/b/zfind", "a b", "/b/hf.json", 50, 5)
    ['hyperfine', '--warmup', '5', '--runs', '50', '--style', 'none', '--metrics', 'time_wall_clock,instructions,cpu_cycles,memory_peak_resident', '--command-name', 'floor', '/b/zfind --no-config --no-gitignore --color never', '--command-name', 'pattern', "/b/zfind --no-config --no-gitignore --color never -- 'a b'", '--export-json', '/b/hf.json']
    """
    command = [
        exe,
        "--warmup", str(warmup),
        "--runs", str(runs),
        "--style", "none",
        "--metrics", ",".join(metrics),
    ]
    for workload in WORKLOADS:
        command += [
            "--command-name", workload,
            shlex.join(workload_argv(binary, pattern, workload)),
        ]
    return command + ["--export-json", out_json]


def benchmark_env(bench_home):
    """The exact environment hyperfine and the benchmarked child receive.

    >>> benchmark_env("/r/build/bench/home")["HOME"]
    '/r/build/bench/home'
    >>> sorted(benchmark_env("/h"))
    ['HOME', 'LC_ALL', 'NO_COLOR', 'PATH', 'TERM']
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": bench_home,
        "LC_ALL": "C",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }


def check_schema(data):
    """Return DATA if it is hyperfine 2.x JSON, else raise ValueError.

    >>> check_schema({"schema_version": 2})["schema_version"]
    2
    >>> check_schema({"schema_version": 1})
    Traceback (most recent call last):
        ...
    ValueError: unsupported hyperfine JSON schema_version 1 (need 2)
    """
    version = data.get("schema_version")
    if version != 2:
        raise ValueError(f"unsupported hyperfine JSON schema_version {version} (need 2)")
    return data


def summaries(data):
    """Map each command name to its metric summary.

    >>> summaries({"results": [{"name": "floor", "summary": {"instructions": {"min": 7}}}]})
    {'floor': {'instructions': {'min': 7}}}
    """
    return {entry["name"]: entry["summary"] for entry in data["results"]}


def metric(summary, name, key="min"):
    """KEY (default min) of metric NAME in a hyperfine summary, or None.

    >>> metric({"instructions": {"min": 7}}, "instructions")
    7
    >>> metric({"instructions": {"min": 7}}, "cpu_cycles") is None
    True
    """
    return summary.get(name, {}).get(key)


def noisy(summary, name, limit):
    """Coefficient of variation of metric NAME as a percentage, or None.

    The value is returned only when it exceeds LIMIT, so it can be used
    directly in a warning.

    >>> noisy({"time_wall_clock": {"mean": 1.0, "stddev": 0.2}}, "time_wall_clock", 0.10)
    '20.0'
    >>> noisy({"instructions": {"mean": 100.0, "stddev": 0.5}}, "instructions", 0.01) is None
    True
    >>> noisy({}, "instructions", 0.01) is None
    True
    """
    mean = metric(summary, name, "mean")
    stddev = metric(summary, name, "stddev")
    if not mean or stddev is None:
        return None
    cv = stddev / mean
    return f"{cv * 100:.1f}" if cv > limit else None


def counters_unavailable(stderr):
    """True if hyperfine failed because perf counters are restricted.

    >>> counters_unavailable(
    ...     "Error: Metric 'instructions' is unavailable for this platform. "
    ...     "Check platform support and hardware-counter permissions."
    ... )
    True
    >>> counters_unavailable("Error: Could not run command '/nope'")
    False
    """
    return (
        "is unavailable" in stderr.lower()
        and "hardware-counter permissions" in stderr.lower()
    )


def result_row(workload, summary):
    """One report row for WORKLOAD's hyperfine summary.

    >>> result_row("floor", {
    ...     "time_wall_clock": {"min": 0.006, "median": 0.0062,
    ...                         "mean": 0.0061, "stddev": 0.0002},
    ...     "instructions": {"min": 113_500_000},
    ...     "cpu_cycles": {"min": 69_300_000},
    ...     "memory_peak_resident": {"min": 4_404_019},
    ... })
    ['floor', '6.0 ms', '6.2 ms', '6.1 ± 0.2 ms', '113.5 M', '69.3 M', '4.2 MiB']
    >>> result_row("pattern", {})[1:]
    ['n/a', 'n/a', 'n/a', 'n/a', 'n/a', 'n/a']
    """
    return [
        workload,
        millis(metric(summary, "time_wall_clock", "min")),
        millis(metric(summary, "time_wall_clock", "median")),
        millis_pair(
            metric(summary, "time_wall_clock", "mean"),
            metric(summary, "time_wall_clock", "stddev"),
        ),
        human_count(metric(summary, "instructions")),
        human_count(metric(summary, "cpu_cycles")),
        mebibytes(metric(summary, "memory_peak_resident")),
    ]


def millis(seconds):
    """Seconds as milliseconds with one decimal, or 'n/a'.

    >>> millis(0.00612)
    '6.1 ms'
    >>> millis(None)
    'n/a'
    """
    return "n/a" if seconds is None else f"{seconds * 1000:.1f} ms"


def millis_pair(mean, stddev):
    """Mean and sample standard deviation in ms, or 'n/a'.

    >>> millis_pair(0.0064, 0.0002)
    '6.4 ± 0.2 ms'
    >>> millis_pair(None, None)
    'n/a'
    """
    if mean is None or stddev is None:
        return "n/a"
    return f"{mean * 1000:.1f} ± {stddev * 1000:.1f} ms"


def mebibytes(value):
    """Bytes as mebibytes with one decimal, or 'n/a'.

    >>> mebibytes(3_497_984)
    '3.3 MiB'
    >>> mebibytes(None)
    'n/a'
    """
    return "n/a" if value is None else f"{value / (1 << 20):.1f} MiB"


def human_count(value):
    """A counter with an SI prefix and one decimal place, or 'n/a'.

    >>> human_count(113_600_000)
    '113.6 M'
    >>> human_count(1_234)
    '1.2 k'
    >>> human_count(999)
    '999'
    >>> human_count(None)
    'n/a'
    """
    if value is None:
        return "n/a"
    for scale, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if value >= scale:
            return f"{value / scale:.1f} {prefix}"
    return f"{value:.0f}"


def human_size(value):
    """A file size in binary units.

    >>> human_size(65_456)
    '63.9 KiB'
    >>> human_size(1_258_291)
    '1.2 MiB'
    >>> human_size(512)
    '512 B'
    """
    if value >= 1 << 20:
        return f"{value / (1 << 20):.1f} MiB"
    if value >= 1 << 10:
        return f"{value / (1 << 10):.1f} KiB"
    return f"{value} B"


def commit_from(describe):
    """The commit part of `git describe --dirty` output.

    >>> commit_from("v0.0.3-3-gabc-dirty")
    'v0.0.3-3-gabc'
    >>> commit_from("v0.0.3")
    'v0.0.3'
    """
    return describe.removesuffix("-dirty")


def dirty_flag(describe):
    """Whether `git describe --dirty` marked the tree dirty.

    >>> dirty_flag("v0.0.3-3-gabc")
    False
    >>> dirty_flag("v0.0.3-3-gabc-dirty")
    True
    """
    return describe.endswith("-dirty")


def report_stem(describe):
    """Report filename stem: `git describe` output without its slashes.

    >>> report_stem("v0.0.3")
    'v0.0.3'
    >>> report_stem("feature/x-3-gabc-dirty")
    'feature_x-3-gabc-dirty'
    """
    return describe.replace("/", "_")


def display_path(path, start="."):
    """PATH relative to START when it stays inside it, else absolute.

    >>> display_path("/repo/build/bench/x.md", "/repo")
    'build/bench/x.md'
    >>> display_path("/elsewhere/x.md", "/repo")
    '/elsewhere/x.md'
    """
    relative = os.path.relpath(path, start)
    return path if relative.startswith("..") else relative


def table(rows):
    """Pad ROWS into columns two spaces apart.

    >>> print(table([["workload", "wall min"], ["floor", "6.1 ms"]]))
    workload  wall min
    floor     6.1 ms
    """
    widths = [
        max(len(row[i]) for row in rows if i < len(row))
        for i in range(max(map(len, rows)))
    ]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )


def cpu_model_from(text):
    r"""The 'model name' value from /proc/cpuinfo, or 'n/a'.

    >>> cpu_model_from("processor\t: 0\nmodel name\t: AMD Ryzen 5 7640U\n")
    'AMD Ryzen 5 7640U'
    >>> cpu_model_from("flags\t: fpu\n")
    'n/a'
    """
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "model name":
            return value.strip()
    return "n/a"


def error(message):
    """Print MESSAGE to stderr and exit 1."""
    print(message, file=sys.stderr)
    sys.exit(1)


def git_describe():
    """`git describe --tags --always --dirty` output, or exit 1."""
    try:
        proc = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        error(f"bench: git describe failed: {exc}")
    if proc.returncode != 0:
        error(f"bench: git describe failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def marker_is_current(path):
    """True if the marker at PATH describes the current tree layout."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read() == marker_text()
    except OSError:
        return False


def generate_tree(tree):
    """Create FILES empty files in DIRS directories under TREE."""
    if os.path.isdir(tree):
        shutil.rmtree(tree)
    for directory in tree_dirs():
        os.makedirs(os.path.join(tree, directory), exist_ok=True)
    for index in range(FILES):
        path = os.path.join(tree, tree_path(index))
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o644))


def prepare_tree(given):
    """Return the absolute tree path and its size description."""
    if given:
        tree = os.path.abspath(given)
        if not os.path.isdir(tree):
            error(f"bench: tree not found: {given}")
        return tree, "n/a files, n/a dirs; n/a entries"

    tree = os.path.abspath(TREE)
    marker = os.path.abspath(MARKER)
    if os.path.isdir(tree) and marker_is_current(marker):
        return tree, f"{FILES} files, {DIRS} dirs; {ENTRIES} entries"

    print(f"bench: generating {display_path(tree)} ({FILES} files)", file=sys.stderr)
    generate_tree(tree)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(marker_text())
    return tree, f"{FILES} files, {DIRS} dirs; {ENTRIES} entries"


def run_hyperfine(exe, binary, pattern, tree, json_path, runs, warmup, env):
    """Run hyperfine; return 'available' or 'unavailable' for counters."""
    command = hyperfine_command(exe, binary, pattern, json_path, runs, warmup)
    proc = subprocess.run(
        command, cwd=tree, env=env, capture_output=True, text=True
    )
    if proc.returncode == 0:
        return "available"

    stderr = proc.stderr
    if not counters_unavailable(stderr):
        error(f"bench: hyperfine failed:\n{stderr.strip()}")

    print(
        "bench: perf counters unavailable; rerunning with wall time and RSS only",
        file=sys.stderr,
    )
    command = hyperfine_command(
        exe, binary, pattern, json_path, runs, warmup, FALLBACK_METRICS
    )
    proc = subprocess.run(
        command, cwd=tree, env=env, capture_output=True, text=True
    )
    if proc.returncode != 0:
        error(f"bench: hyperfine failed:\n{proc.stderr.strip()}")
    return "unavailable"


def hyperfine_version(exe):
    """The first line of `hyperfine --version`, or 'n/a'."""
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True)
    except OSError:
        return "n/a"
    if proc.returncode != 0:
        return "n/a"
    return proc.stdout.splitlines()[0].strip() if proc.stdout else "n/a"


def sha256(path):
    """SHA-256 of the file at PATH."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cpu_model():
    """The CPU model from /proc/cpuinfo, or 'n/a'."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            return cpu_model_from(f.read())
    except OSError:
        return "n/a"


def thread_count():
    """The number of CPUs this process may run on."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 0


def governor():
    """The CPU scaling governor, or 'n/a'."""
    try:
        with open(GOVERNOR, encoding="utf-8") as f:
            return f.read().strip() or "n/a"
    except OSError:
        return "n/a"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--binary", required=True, metavar="PATH",
        help="zfind binary to benchmark (the Makefile passes $(BIN))",
    )
    parser.add_argument(
        "--pattern", default="conf", metavar="PATTERN",
        help="PATTERN for the pattern workload (default: conf)",
    )
    parser.add_argument(
        "--tree", metavar="PATH",
        help="benchmark an existing tree; skip tree generation",
    )
    parser.add_argument(
        "--warmup", type=int, default=5, metavar="N",
        help="hyperfine warm-up runs (default 5)",
    )
    parser.add_argument(
        "--runs", type=int, default=50, metavar="N",
        help="hyperfine measured runs (default 50)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    binary = os.path.abspath(args.binary)
    if not os.path.isfile(binary):
        error(f"bench: binary not found: {args.binary}")

    exe = shutil.which("hyperfine")
    if exe is None:
        error("bench: hyperfine not found in PATH (run inside nix develop)")

    describe = git_describe()
    stem = report_stem(describe)

    bench_dir = os.path.abspath(BENCH_DIR)
    bench_home = os.path.abspath(BENCH_HOME)
    os.makedirs(bench_home, exist_ok=True)
    tree, size = prepare_tree(args.tree)

    json_path = os.path.join(bench_dir, f"hyperfine-{stem}.json")
    report_path = os.path.join(bench_dir, f"results-{stem}.md")

    counters = run_hyperfine(
        exe, binary, args.pattern, tree, json_path,
        args.runs, args.warmup, benchmark_env(bench_home),
    )

    try:
        with open(json_path, encoding="utf-8") as f:
            data = check_schema(json.load(f))
    except (OSError, ValueError) as exc:
        error(f"bench: {exc}")
    if not data.get("results"):
        error("bench: hyperfine wrote no results")

    results = summaries(data)
    rows = [[
        "workload", "wall min", "wall median", "wall mean±sd",
        "instr (min)", "cycles (min)", "rss (min)",
    ]]
    warnings = []
    for workload in WORKLOADS:
        summary = results.get(workload, {})
        rows.append(result_row(workload, summary))
        if percent := noisy(summary, "time_wall_clock", WALL_CV_LIMIT):
            warnings.append(
                f"warning: {workload}: wall-time CV {percent} % > 10 %"
            )
        if percent := noisy(summary, "instructions", INSTR_CV_LIMIT):
            warnings.append(
                f"warning: {workload}: instructions CV {percent} % > 1 %"
            )

    facts = [
        ("commit", commit_from(describe)),
        ("dirty", "yes" if dirty_flag(describe) else "no"),
        ("binary", f"{display_path(binary)}  "
                   f"{human_size(os.path.getsize(binary))}  "
                   f"sha256:{sha256(binary)}"),
        ("tree", f"{display_path(tree)} ({size})"),
        ("pattern", args.pattern),
        ("counters", counters),
        ("runs", f"{args.runs} (warmup {args.warmup})"),
        ("date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")),
        ("host", f"{cpu_model()}, {thread_count()} threads, "
                 f"governor={governor()}, Linux {platform.release()}"),
        ("hyperfine", hyperfine_version(exe)),
    ]

    lines = [f"{key + ':':<8} {text}" for key, text in facts]
    lines.append("")
    lines += warnings
    if warnings:
        lines.append("")
    lines.append(table(rows))
    report = "\n".join(lines) + "\n"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report, end="")
    print(f"\nwrote {display_path(report_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
