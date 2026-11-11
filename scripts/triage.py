#!/usr/bin/env python3
"""Run automated crash triage with GDB or inspect performance profiling.

Usage:
    scripts/triage.py --core COREFILE [--binary BIN] [--verbose]
    scripts/triage.py [--binary BIN] -- BINARY [ARGS...]
    scripts/triage.py --profile [--binary BIN] [--tree TREE]
                       [--perf-data FILE] [-- PATTERN]

Examples:
    # Live triage: explicitly select the starting directory with --path
    python3 scripts/triage.py -- build/x86_64/zfind --no-config --path src

    # Core dump analysis:
    python3 scripts/triage.py --core core.359936

    # Profile the complete src tree (no search pattern):
    python3 scripts/triage.py --profile --tree src

    # Profile names containing the literal substring "zfind":
    python3 scripts/triage.py --profile --tree src -- zfind

    # Keep the recorded perf data for later inspection:
    python3 scripts/triage.py --profile --perf-data perf.data --tree src

Patterns are literal substrings. zfind does not interpret glob or regex syntax;
for example, '*.c' searches for the literal characters '*.c'. Use --path DIR
when passing a bare directory name such as "src" to zfind.

Use --verbose to print the complete GDB transcript, including output from the
program being debugged.

Self-test:  python3 -m doctest scripts/triage.py
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_BINARY = "build/x86_64/zfind"
DEFAULT_TREE = "src"
MAX_REPORT_LINES = 40

THREAD_HEADER_RE = re.compile(r"^\s*Thread\s+(\d+)\s+\((.*?)\):\s*$")
FRAME_RE = re.compile(r"^\s*#\d+\s+")
REGISTER_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_]*)\s+(0x[0-9a-fA-F]+|[-+]?\d+)\b")
SIGNAL_RE = re.compile(r"\b(SIG[A-Z0-9]+)\b")
EXIT_CODE_RE = re.compile(r"\bexited with code\s+(\d+)\b", re.IGNORECASE)


GDB_REPORT_COMMANDS = (
    ("echo \\n--- BACKTRACE ---\\n", "thread apply all bt full"),
    ("echo \\n--- REGISTERS ---\\n", "info registers"),
    ("echo \\n--- INSTRUCTION ---\\n", "x/5i $rip - 8"),
)


def parse_gdb_output(raw: str) -> dict[str, Any]:
    r"""Extract process status, per-thread backtraces, registers and instructions.

    Frames are kept per thread. Identical frames in different threads are not
    deduplicated because they represent distinct execution contexts.

    >>> sample = '''[New LWP 1234]
    ... Program terminated with signal SIGSEGV, Segmentation fault.
    ... --- BACKTRACE ---
    ... Thread 1 (Thread 0x7f0000 (LWP 1234)):
    ... #0  0x404072 in append_quoted () at src/x86_64/zfind.asm:4727
    ... #1  0x401ef4 in select_root () at src/x86_64/zfind.asm:1718
    ... Thread 2 (Thread 0x7f1000 (LWP 1235)):
    ... #0  0x404072 in append_quoted () at src/x86_64/zfind.asm:4727
    ... --- REGISTERS ---
    ... rax            0x1fb61             129889
    ... rip            0x404072            0x404072 <append_quoted+16>
    ... --- INSTRUCTION ---
    ... => 0x404072 <append_quoted+16>: ret
    ... '''
    >>> result = parse_gdb_output(sample)
    >>> result["crashed"]
    True
    >>> result["signal"]
    'SIGSEGV'
    >>> len(result["threads"])
    2
    >>> len(result["threads"][0]["backtrace"])
    2
    >>> len(result["threads"][1]["backtrace"])
    1
    >>> result["registers"]["rax"]
    '0x1fb61'
    >>> len(result["instructions"])
    1

    A nonzero normal exit must remain distinguishable from a clean exit:

    >>> parse_gdb_output("Inferior 1 (process 123) exited with code 07")["exit_status"]
    7
    >>> parse_gdb_output("Inferior 1 (process 123) exited with code 010")["exit_status"]
    8
    >>> parse_gdb_output("Inferior 1 (process 123) exited normally")["exit_status"]
    0
    """
    result: dict[str, Any] = {
        "crashed": False,
        "signal": None,
        "exit_status": None,
        "threads": [],
        "backtrace": [],
        "registers": {},
        "instructions": [],
    }

    crash_match = re.search(
        r"(?:terminated with|received) signal\s+([^\n.]+)",
        raw,
        re.IGNORECASE,
    )
    if crash_match:
        result["crashed"] = True
        signal_match = SIGNAL_RE.search(crash_match.group(1))
        result["signal"] = signal_match.group(1) if signal_match else crash_match.group(1).strip()
    elif "exited normally" in raw:
        result["exit_status"] = 0
    else:
        exit_match = EXIT_CODE_RE.search(raw)
        if exit_match:
            # GDB formats inferior exit codes in octal (for example, 010 = 8).
            code = exit_match.group(1)
            try:
                result["exit_status"] = int(code, 8)
            except ValueError:
                # Be tolerant of nonstandard GDB-compatible output.
                result["exit_status"] = int(code, 10)

    section: str | None = None
    current_thread: dict[str, Any] | None = None

    for line in raw.splitlines():
        if "--- BACKTRACE ---" in line:
            section = "backtrace"
            current_thread = None
            continue
        if "--- REGISTERS ---" in line:
            section = "registers"
            current_thread = None
            continue
        if "--- INSTRUCTION ---" in line:
            section = "instructions"
            current_thread = None
            continue

        if section == "backtrace":
            thread_match = THREAD_HEADER_RE.match(line)
            if thread_match:
                current_thread = {
                    "thread_id": int(thread_match.group(1)),
                    "description": thread_match.group(2),
                    "backtrace": [],
                }
                result["threads"].append(current_thread)
                continue

            if FRAME_RE.match(line):
                frame = line.strip()
                if current_thread is None:
                    result["backtrace"].append(frame)
                else:
                    current_thread["backtrace"].append(frame)
            continue

        # Support simple/older GDB output without our section markers.
        if section is None and FRAME_RE.match(line):
            result["backtrace"].append(line.strip())
            continue

        if section == "registers":
            register_match = REGISTER_RE.match(line)
            if register_match:
                result["registers"][register_match.group(1)] = register_match.group(2)
            continue

        if section == "instructions":
            stripped = line.strip()
            if "=>" in line or re.search(r"0x[0-9a-fA-F]+.*:", line):
                result["instructions"].append(stripped)

    return result


def format_triage_report(info: dict[str, Any]) -> str:
    """Format parsed GDB information into a concise diagnostic report.

    >>> format_triage_report({"crashed": False, "exit_status": 0})
    'STATUS: Clean exit (code 0)'
    >>> format_triage_report({"crashed": False, "exit_status": 7})
    'STATUS: Program exited with code 7'
    >>> format_triage_report({"crashed": False, "exit_status": None})
    'STATUS: Exit status unknown'
    """
    if not info.get("crashed"):
        exit_status = info.get("exit_status")
        if exit_status == 0:
            return "STATUS: Clean exit (code 0)"
        if exit_status is not None:
            return f"STATUS: Program exited with code {exit_status}"
        return "STATUS: Exit status unknown"

    lines = [
        f"CRASH DETECTED: {info.get('signal') or 'Unknown fault'}",
        "",
        "--- BACKTRACE ---",
    ]

    threads = info.get("threads") or []
    if threads:
        for thread in threads:
            thread_id = thread.get("thread_id", "?")
            description = thread.get("description")
            heading = f"Thread {thread_id}"
            if description:
                heading += f" ({description})"
            lines.append(heading + ":")
            frames = thread.get("backtrace") or []
            if frames:
                lines.extend(f"  {frame}" for frame in frames)
            else:
                lines.append("  (No stack frames captured)")
    elif info.get("backtrace"):
        lines.extend(f"  {frame}" for frame in info["backtrace"])
    else:
        lines.append("  (No stack frames captured)")

    instructions = info.get("instructions") or []
    if instructions:
        lines.extend(["", "--- INSTRUCTION ---"])
        lines.extend(f"  {instruction}" for instruction in instructions[:5])

    registers = info.get("registers") or {}
    if registers:
        lines.extend(["", "--- KEY REGISTERS ---"])
        for register in ("rip", "rsp", "rax", "rdi", "rsi", "rdx", "rcx", "rbx", "rbp"):
            if register in registers:
                lines.append(f"  {register:<4} = {registers[register]}")

    return "\n".join(lines)


def _gdb_commands(corefile: str | None) -> list[str]:
    commands = ["set pagination off", "set confirm off"]
    if corefile is None:
        commands.append("run")
    for echo_command, gdb_command in GDB_REPORT_COMMANDS:
        commands.extend((echo_command, gdb_command))
    return commands


def run_gdb_triage(
    binary: str,
    args: list[str] | None = None,
    corefile: str | None = None,
    verbose: bool = False,
) -> tuple[dict[str, Any], str | None]:
    """Run GDB in batch mode against a live process or core file."""
    cmd = ["gdb", "-q", "-batch", "-nx", "-return-child-result"]
    for command in _gdb_commands(corefile):
        cmd.extend(("-ex", command))

    if corefile is None:
        cmd.extend(("--args", binary))
        if args:
            cmd.extend(args)
    else:
        cmd.extend((binary, corefile))

    env = os.environ.copy()
    env["LC_ALL"] = "C"

    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            sys.stderr.write("error: gdb not found; install GDB and ensure it is in PATH\n")
        else:
            sys.stderr.write(f"error: unable to run GDB: {exc}\n")
        return {}, None

    parsed = parse_gdb_output(completed.stdout)

    if verbose and completed.stdout:
        sys.stderr.write("--- GDB TRANSCRIPT ---\n")
        sys.stderr.write(completed.stdout)
        if not completed.stdout.endswith("\n"):
            sys.stderr.write("\n")

    # With -return-child-result, a nonzero return code can be the debugged
    # program's exit status. Only treat it as a GDB failure when GDB did not
    # report a process outcome that we can interpret.
    if completed.returncode != 0 and not parsed["crashed"] and parsed["exit_status"] is None:
        sys.stderr.write(f"error: GDB exited with status {completed.returncode}\n")
        if completed.stdout and not verbose:
            sys.stderr.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                sys.stderr.write("\n")
        return parsed, None

    return parsed, format_triage_report(parsed)


def _exit_code_for_signal(signal_name: str | None) -> int:
    """Return the conventional shell status (128 + signal number)."""
    if not signal_name:
        return 1
    signal_value = getattr(signal, signal_name, None)
    if signal_value is None:
        return 1
    return min(255, 128 + int(signal_value))


def triage_exit_code(info: dict[str, Any]) -> int:
    """Return a useful process status for scripts invoking this utility.

    >>> triage_exit_code({"crashed": False, "exit_status": 0})
    0
    >>> triage_exit_code({"crashed": False, "exit_status": 7})
    7
    >>> triage_exit_code({"crashed": True, "signal": "SIGSEGV"})
    139
    >>> triage_exit_code({"crashed": True, "signal": "not-a-signal"})
    1
    """
    if info.get("crashed"):
        return _exit_code_for_signal(info.get("signal"))
    exit_status = info.get("exit_status")
    return int(exit_status) if exit_status is not None else 2


def _run_command(
    command: list[str],
    *,
    capture_stdout: bool = False,
    cwd: str | Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run an external command and normalize missing-executable errors."""
    return subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE if capture_stdout else None,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def run_perf_profile(
    binary: str,
    tree: str = DEFAULT_TREE,
    pattern: str | None = None,
    perf_data: str | None = None,
) -> int:
    """Record a call graph and print a concise perf report.

    Perf permissions are checked by ``perf record`` itself. This avoids
    rejecting configurations based on a kernel setting that may not reflect
    whether the requested user-space event is permitted.
    """
    # Validate the selected tree and pass it with zfind's explicit --path
    # option. This avoids relying on positional path-candidate detection and
    # does not require a trailing slash.
    try:
        tree_path = Path(tree).expanduser().resolve(strict=True)
    except OSError as exc:
        sys.stderr.write(f"error: cannot access profiling tree {tree!r}: {exc}\n")
        return 1
    if not tree_path.is_dir():
        sys.stderr.write(f"error: profiling tree is not a directory: {tree_path}\n")
        return 1

    tree_arg = os.fspath(Path(tree).expanduser())

    # Resolve the binary because the profiling command runs from the caller's
    # current directory and passes the tree as an explicit zfind operand.
    binary_path = Path(binary).expanduser()
    if not binary_path.is_absolute():
        binary_path = Path.cwd() / binary_path
    binary_path = binary_path.resolve()

    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if perf_data:
        output_path = str(Path(perf_data).expanduser().resolve())
    else:
        temporary_directory = tempfile.TemporaryDirectory(prefix="triage-perf-")
        output_path = str(Path(temporary_directory.name) / "perf.data")

    perf_command = [
        "perf", "record",
        "--call-graph", "dwarf",
        "-F", "99",
        "-e", "cycles:u",
        "-o", output_path,
        "--", str(binary_path), "--no-config",
        "--path", tree_arg,
    ]
    # zfind treats patterns as literal substrings, not globs or regexes.
    # Use -- so path-like patterns and leading-hyphen patterns stay literal.
    if pattern is not None:
        perf_command.extend(("--", pattern))

    try:
        sys.stdout.write(f"Running: {shlex.join(perf_command)}\n")
        try:
            record = _run_command(perf_command)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                sys.stderr.write("error: perf not found; install Linux perf and ensure it is in PATH\n")
            else:
                sys.stderr.write(f"error: unable to run perf record: {exc}\n")
            return 1

        if record.returncode != 0:
            sys.stderr.write(f"error: perf record failed (exit {record.returncode})\n")
            if record.stderr:
                sys.stderr.write(record.stderr)
            return record.returncode if record.returncode > 0 else 1

        report_command = [
            "perf", "report",
            "-i", output_path,
            "--stdio",
            "--no-children",
            "-g", "folded,0,caller,function,period",
        ]
        try:
            report = _run_command(report_command, capture_stdout=True)
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                sys.stderr.write("error: perf not found; install Linux perf and ensure it is in PATH\n")
            else:
                sys.stderr.write(f"error: unable to run perf report: {exc}\n")
            return 1

        if report.returncode != 0:
            sys.stderr.write(f"error: perf report failed (exit {report.returncode})\n")
            if report.stderr:
                sys.stderr.write(report.stderr)
            return report.returncode if report.returncode > 0 else 1

        report_lines = report.stdout.splitlines()
        if report_lines:
            shown_lines = report_lines[:MAX_REPORT_LINES]
            sys.stdout.write("\n".join(shown_lines) + "\n")
            if len(report_lines) > MAX_REPORT_LINES:
                sys.stdout.write(f"... ({len(report_lines) - MAX_REPORT_LINES} more lines omitted)\n")
        else:
            sys.stdout.write("perf report produced no output\n")

        if perf_data:
            sys.stdout.write(f"\nPerf data saved to: {output_path}\n")
        return 0
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run automated crash triage with GDB or inspect performance profiling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--core", metavar="COREFILE", help="Core dump file to analyse")
    mode.add_argument("--profile", action="store_true", help="Run perf profiling instead of GDB crash triage")
    parser.add_argument("--binary", help=f"Path to binary (default: {DEFAULT_BINARY})")
    parser.add_argument("--tree", help=f"Directory tree to profile (default: {DEFAULT_TREE})")
    parser.add_argument("--perf-data", metavar="FILE", help="Keep profiling data in FILE instead of a temporary file")
    parser.add_argument("--verbose", action="store_true", help="Print the complete GDB transcript")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Binary and arguments after --, or one literal substring pattern in profile mode")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)

    extra = list(options.command)
    if extra and extra[0] == "--":
        extra.pop(0)

    if options.perf_data and not options.profile:
        parser.error("--perf-data is only valid with --profile")
    if options.tree is not None and not options.profile:
        parser.error("--tree is only valid with --profile")
    if options.verbose and options.profile:
        parser.error("--verbose is only valid for GDB triage")

    if options.profile:
        if len(extra) > 1:
            parser.error("--profile accepts at most one pattern after --")
        return run_perf_profile(
            options.binary or DEFAULT_BINARY,
            tree=options.tree or DEFAULT_TREE,
            pattern=extra[0] if extra else None,
            perf_data=options.perf_data,
        )

    if options.core:
        if extra:
            parser.error("unexpected arguments after -- when using --core")
        binary = options.binary or DEFAULT_BINARY
        parsed, report = run_gdb_triage(
            binary,
            corefile=options.core,
            verbose=options.verbose,
        )
    else:
        if extra and options.binary:
            parser.error("use either --binary or specify BINARY after --, not both")
        binary = options.binary or (extra.pop(0) if extra else DEFAULT_BINARY)
        parsed, report = run_gdb_triage(
            binary,
            args=extra,
            verbose=options.verbose,
        )

    if report is None:
        return 2

    print(report)
    return triage_exit_code(parsed)


if __name__ == "__main__":
    sys.exit(main())
