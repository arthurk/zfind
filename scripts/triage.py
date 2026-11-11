#!/usr/bin/env python3
"""Run automated crash triage with GDB or inspect performance profiling.

Usage:
    scripts/triage.py --core COREFILE [--binary BIN]
    scripts/triage.py [--binary BIN] -- [ARGS...]
    scripts/triage.py --profile [--binary BIN] [--tree TREE] [-- PATTERN]

Examples:
    # Live triage: run a command under GDB and report on crash
    python3 scripts/triage.py -- build/x86_64/zfind --no-config src

    # Core dump analysis:
    python3 scripts/triage.py --core core.359936

    # Call-graph profiling (checks kernel permissions first):
    python3 scripts/triage.py --profile --tree src -- '*.c'

Self-test:  python3 -m doctest scripts/triage.py
"""
import argparse
import re
import subprocess
import sys

DEFAULT_BINARY = "build/x86_64/zfind"
PARANOID_PATH = "/proc/sys/kernel/perf_event_paranoid"


def get_perf_paranoid(path=PARANOID_PATH):
    r"""Return the integer value of perf_event_paranoid, or None if unreadable.

    >>> get_perf_paranoid("/nonexistent") is None
    True
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def parse_gdb_output(raw):
    r"""Extract crash details, backtrace, and registers from GDB batch output.

    >>> sample = '''[New LWP 1234]
    ... Program terminated with signal SIGSEGV, Segmentation fault.
    ... #0  0x404072 in append_quoted () at src/x86_64/zfind.asm:4727
    ... #1  0x401ef4 in select_root () at src/x86_64/zfind.asm:1718
    ... --- REGISTERS ---
    ... rax            0x1fb61             129889
    ... rip            0x404072            0x404072 <append_quoted+16>
    ... '''
    >>> res = parse_gdb_output(sample)
    >>> res['crashed']
    True
    >>> res['signal']
    'SIGSEGV, Segmentation fault'
    >>> len(res['backtrace'])
    2
    >>> res['backtrace'][0]
    '#0  0x404072 in append_quoted () at src/x86_64/zfind.asm:4727'
    >>> res['registers']['rax']
    '0x1fb61'
    """
    result = {
        "crashed": False,
        "signal": None,
        "exit_status": None,
        "backtrace": [],
        "registers": {},
        "instructions": [],
    }

    if "Program terminated with signal" in raw or "received signal" in raw:
        result["crashed"] = True
        sig_match = re.search(r"(?:terminated with|received) signal ([^,\n]+(?:, [^\n]+)?)", raw)
        if sig_match:
            result["signal"] = sig_match.group(1).strip().rstrip(".")
    elif "exited normally" in raw:
        result["exit_status"] = 0
    else:
        exit_match = re.search(r"exited with code (\d+)", raw)
        if exit_match:
            result["exit_status"] = int(exit_match.group(1))

    # Parse backtrace frames (#0, #1, ...)
    seen_frames = set()
    for line in raw.splitlines():
        line_clean = line.strip()
        if re.match(r"^#\d+\s+0x[0-9a-fA-F]+", line_clean) or re.match(r"^#\d+\s+in\s+", line_clean):
            if line_clean not in seen_frames:
                seen_frames.add(line_clean)
                result["backtrace"].append(line_clean)

    # Parse registers
    in_regs = False
    in_inst = False
    for line in raw.splitlines():
        if "--- REGISTERS ---" in line:
            in_regs = True
            in_inst = False
            continue
        if "--- INSTRUCTION ---" in line or "--- BACKTRACE ---" in line:
            in_regs = False
            if "--- INSTRUCTION ---" in line:
                in_inst = True
            continue

        if in_regs:
            parts = line.split()
            if len(parts) >= 2 and re.match(r"^[a-z0-9_]+$", parts[0]):
                result["registers"][parts[0]] = parts[1]
        elif in_inst:
            if "=>" in line or re.search(r"0x[0-9a-fA-F]+.*:", line):
                result["instructions"].append(line.strip())

    return result


def format_triage_report(info):
    r"""Format parsed GDB triage information into a concise diagnostic string.

    >>> info = {'crashed': False, 'exit_status': 0, 'backtrace': [], 'registers': {}, 'instructions': []}
    >>> format_triage_report(info)
    'STATUS: Clean exit (code 0)'
    """
    if not info["crashed"]:
        status = info["exit_status"] if info["exit_status"] is not None else "unknown"
        return f"STATUS: Clean exit (code {status})"

    lines = [
        f"CRASH DETECTED: {info['signal'] or 'Unknown fault'}",
        "",
        "--- BACKTRACE ---",
    ]
    if info["backtrace"]:
        lines.extend(f"  {frame}" for frame in info["backtrace"])
    else:
        lines.append("  (No stack frames captured)")

    if info["instructions"]:
        lines.append("")
        lines.append("--- INSTRUCTION ---")
        lines.extend(f"  {inst}" for inst in info["instructions"][:5])

    if info["registers"]:
        lines.append("")
        lines.append("--- KEY REGISTERS ---")
        for reg in ("rip", "rsp", "rax", "rdi", "rsi", "rdx", "rcx", "rbx", "rbp"):
            if reg in info["registers"]:
                lines.append(f"  {reg:<4} = {info['registers'][reg]}")

    return "\n".join(lines)


def run_gdb_triage(binary, args=None, corefile=None):
    """Run GDB in batch mode against a live process or core file."""
    cmd = ["gdb", "-batch", "-return-child-result"]

    if corefile:
        cmd.extend([
            "-ex", "set pagination off",
            "-ex", "echo \\n--- BACKTRACE ---\\n",
            "-ex", "thread apply all bt full",
            "-ex", "echo \\n--- REGISTERS ---\\n",
            "-ex", "info registers",
            "-ex", "echo \\n--- INSTRUCTION ---\\n",
            "-ex", "x/5i $rip - 8",
            binary,
            corefile,
        ])
    else:
        cmd.extend([
            "-ex", "set pagination off",
            "-ex", "run",
            "-ex", "echo \\n--- BACKTRACE ---\\n",
            "-ex", "thread apply all bt full",
            "-ex", "echo \\n--- REGISTERS ---\\n",
            "-ex", "info registers",
            "-ex", "echo \\n--- INSTRUCTION ---\\n",
            "-ex", "x/5i $rip - 8",
            "--args",
            binary,
        ])
        if args:
            cmd.extend(args)

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    parsed = parse_gdb_output(res.stdout)
    # If GDB itself failed (binary missing, can't open core, etc.) and we didn't
    # detect a crash signal, surface the raw output so the caller isn't misled.
    if res.returncode != 0 and not parsed["crashed"] and parsed["exit_status"] is None:
        sys.stderr.write("GDB exited with an error:\n")
        sys.stderr.write(res.stdout)
        return parsed, None
    report = format_triage_report(parsed)
    return parsed, report


def run_perf_profile(binary, tree="src", pattern=None):
    """Run perf recording if allowed by kernel paranoid settings, else warn."""
    paranoid = get_perf_paranoid()
    if paranoid is not None and paranoid >= 2:
        sys.stderr.write(
            f"error: perf profiling restricted (/proc/sys/kernel/perf_event_paranoid is {paranoid} >= 2).\n"
            "Hardware counter sampling requires paranoid <= 1 or CAP_PERFMON.\n"
            "Use 'make bench' for instruction/cycle counts without perf privileges.\n"
        )
        return 1

    perf_cmd = ["perf", "record", "--call-graph", "dwarf", "-F", "99", "-e", "cycles:u", "-o", "/tmp/perf.data", "--", binary, "--no-config", tree]
    if pattern:
        perf_cmd.append(pattern)

    sys.stdout.write(f"Running: {' '.join(perf_cmd)}\n")
    rec = subprocess.run(perf_cmd, stderr=subprocess.PIPE, text=True)
    if rec.returncode != 0:
        sys.stderr.write(f"perf record failed (exit {rec.returncode}):\n")
        if rec.stderr:
            sys.stderr.write(rec.stderr)
        return rec.returncode

    rep = subprocess.run(
        ["perf", "report", "-i", "/tmp/perf.data", "--stdio", "--no-children", "-g", "folded,0,caller,overhead,cycles"],
        stdout=subprocess.PIPE,
        text=True,
    )
    lines = rep.stdout.splitlines()[:40]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--core", metavar="COREFILE", help="Core dump file to analyse")
    parser.add_argument("--binary", default=DEFAULT_BINARY, help=f"Path to zfind binary (default: {DEFAULT_BINARY})")
    parser.add_argument("--profile", action="store_true", help="Run perf profiling instead of GDB crash triage")
    parser.add_argument("--tree", default="src", help="Directory tree to profile (default: src)")
    parser.add_argument("args", nargs=argparse.REMAINDER, help="Arguments passed to binary (after --)")

    opts = parser.parse_args(argv)

    # Strip a leading '--' separator from REMAINDER in all modes.
    extra = opts.args
    if extra and extra[0] == "--":
        extra = extra[1:]

    if opts.profile:
        pattern = extra[0] if extra else None
        return run_perf_profile(opts.binary, tree=opts.tree, pattern=pattern)

    parsed, report = run_gdb_triage(opts.binary, args=extra, corefile=opts.core)
    if report is None:
        return 2  # GDB itself failed; error already written to stderr
    print(report)
    return 1 if parsed["crashed"] else 0


if __name__ == "__main__":
    sys.exit(main())
