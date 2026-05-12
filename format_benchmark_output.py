#!/usr/bin/env python3
"""
Convert benchmark JSON (e.g. output.json) into readable tables.

Each phase (pgbench, linpack) gets its own grid: load % in the first column,
metric columns across the header row.

Usage:
  python3 format_benchmark_output.py bench.json
  python3 format_benchmark_output.py bench.json --csv

Pass the same file you saved with vm_benchmark.py -o / container -o. The default
output.json is used only if you omit the path; empty rx/tx columns usually mean
an older JSON without per-row "network" or the wrong file.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _fmt_float(x: Optional[float], precision: int = 4) -> str:
    if x is None:
        return ""
    return f"{x:.{precision}f}".rstrip("0").rstrip(".")


def _fmt_float6(x: Optional[float]) -> str:
    if x is None:
        return ""
    return f"{x:.6f}"


def _fmt_net_mbps(x: Any) -> str:
    """Format rx_mbit_per_s / tx_mbit_per_s; empty if missing."""
    if x is None:
        return ""
    try:
        return f"{float(x):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def build_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(no rows)\n"
    widths: list[int] = []
    for i, h in enumerate(headers):
        w = len(h)
        for r in rows:
            if i < len(r):
                w = max(w, len(r[i]))
        widths.append(w)
    sep = " | "

    def fmt_line(cells: list[str]) -> str:
        return sep.join(cells[j].ljust(widths[j]) for j in range(len(headers)))

    out = [
        fmt_line(headers),
        sep.join("-" * widths[j] for j in range(len(headers))),
    ]
    for r in rows:
        padded = r + [""] * (len(headers) - len(r))
        out.append(fmt_line(padded[: len(headers)]))
    return "\n".join(out) + "\n"


def pgbench_row(r: dict[str, Any]) -> list[str]:
    perf = r.get("perf") or {}
    net = r.get("network") or {}
    err = r.get("error") or ""
    note = "ok" if not err else err[:40]
    return [
        str(r.get("load_percent", "")),
        _fmt_float(r.get("tps"), 4),
        _fmt_float6(perf.get("duration_sec")),
        str(perf.get("page_faults") if perf.get("page_faults") is not None else ""),
        str(
            perf.get("context_switches")
            if perf.get("context_switches") is not None
            else ""
        ),
        _fmt_net_mbps(net.get("rx_mbit_per_s")),
        _fmt_net_mbps(net.get("tx_mbit_per_s")),
        str(r.get("exit_code") if r.get("exit_code") is not None else ""),
        note,
    ]


def linpack_row(r: dict[str, Any]) -> list[str]:
    perf = r.get("perf") or {}
    net = r.get("network") or {}
    err = r.get("error") or ""
    if err:
        note = err[:48]
    elif r.get("exit_note"):
        note = "ok (nonzero exit accepted)"
    else:
        note = "ok"
    return [
        str(r.get("load_percent", "")),
        _fmt_float(r.get("mflops"), 4),
        _fmt_float6(perf.get("duration_sec")),
        str(perf.get("page_faults") if perf.get("page_faults") is not None else ""),
        str(
            perf.get("context_switches")
            if perf.get("context_switches") is not None
            else ""
        ),
        _fmt_net_mbps(net.get("rx_mbit_per_s")),
        _fmt_net_mbps(net.get("tx_mbit_per_s")),
        str(r.get("exit_code") if r.get("exit_code") is not None else ""),
        note,
    ]


def emit_text(data: dict[str, Any], *, source: Optional[Path] = None) -> str:
    chunks: list[str] = []
    meta = [
        f"source: {source.resolve()}" if source else "source: (stdin or unknown)",
        f"environment: {data.get('environment', '')}",
        f"pgbench_run_duration_sec: {data.get('duration_sec', '')}",
        f"load_levels: {data.get('load_levels', [])}",
    ]
    chunks.append("\n".join(meta) + "\n")

    by_phase: dict[str, list[dict[str, Any]]] = {}
    for row in data.get("rows", []):
        by_phase.setdefault(row.get("phase", "?"), []).append(row)

    pgbench_headers = [
        "preload_%",
        "tps",
        "duration_s",
        "page_faults",
        "context_switches",
        "rx_Mbit_s",
        "tx_Mbit_s",
        "exit_code",
        "error_or_note",
    ]
    linpack_headers = [
        "preload_%",
        "mflops",
        "duration_s",
        "page_faults",
        "context_switches",
        "rx_Mbit_s",
        "tx_Mbit_s",
        "exit_code",
        "error_or_note",
    ]

    for phase, headers, row_fn in (
        ("pgbench", pgbench_headers, pgbench_row),
        ("linpack", linpack_headers, linpack_row),
    ):
        rows_raw = sorted(
            by_phase.get(phase, []),
            key=lambda r: int(r.get("load_percent", -1)),
        )
        if not rows_raw:
            continue
        chunks.append(f"=== {phase.upper()} ===\n")
        chunks.append(build_table(headers, [row_fn(r) for r in rows_raw]))

    all_rows = data.get("rows", [])
    if all_rows and not any(
        isinstance(r.get("network"), dict) and r["network"] for r in all_rows
    ):
        chunks.append(
            "\n(no per-row \"network\" object in JSON; "
            "pass the file from -o with --network-iface set)\n"
        )

    return "".join(chunks)


def emit_csv(data: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "phase",
            "preload_%",
            "tps",
            "mflops",
            "duration_s",
            "page_faults",
            "context_switches",
            "rx_Mbit_s",
            "tx_Mbit_s",
            "exit_code",
            "error",
            "exit_note",
        ]
    )
    for row in sorted(
        data.get("rows", []),
        key=lambda r: (r.get("phase", ""), int(r.get("load_percent", -1))),
    ):
        perf = row.get("perf") or {}
        net = row.get("network") or {}
        w.writerow(
            [
                row.get("phase", ""),
                row.get("load_percent", ""),
                row.get("tps", ""),
                row.get("mflops", ""),
                perf.get("duration_sec", ""),
                perf.get("page_faults", ""),
                perf.get("context_switches", ""),
                net.get("rx_mbit_per_s", ""),
                net.get("tx_mbit_per_s", ""),
                row.get("exit_code", ""),
                row.get("error", "") or "",
                row.get("exit_note", "") or "",
            ]
        )
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "json_file",
        nargs="?",
        default="output.json",
        type=Path,
        help="Path to benchmark JSON (default: output.json)",
    )
    ap.add_argument(
        "--csv",
        action="store_true",
        help="Print one long CSV (all phases) instead of ASCII tables",
    )
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write to this file instead of stdout",
    )
    args = ap.parse_args()
    text = args.json_file.read_text(encoding="utf-8")
    data = json.loads(text)
    out = (
        emit_csv(data)
        if args.csv
        else emit_text(data, source=args.json_file)
    )
    if args.output:
        args.output.write_text(out, encoding="utf-8")
    else:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()
