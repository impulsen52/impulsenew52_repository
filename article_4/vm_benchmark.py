#!/usr/bin/env python3
"""
Native VM benchmark driver: single file, no Docker, no other project modules.

Run on the guest OS after PostgreSQL, pgbench, stress-ng, and a linpack binary are
available. For each CPU load level (0, 20, ...), optional stress-ng on the VM,
then pgbench (with optional perf stat), then linpack (with optional perf stat).

Optional --network-iface (e.g. eth0) records RX/TX Mbit/s per phase from /sys/class/net.
By default pgbench perf samples PostgreSQL server processes (``--pgbench-perf-target server``);
use ``client`` to measure the pgbench driver instead.
If omitted and --pg-host is loopback (localhost, 127.0.0.1, ::1) or ``local``, ``lo`` is
used automatically so local TCP/formatter tables get NIC columns (Unix sockets do not
use ``lo`` - see stderr note).

Dependencies (typical Debian package names):
  postgresql, postgresql-client  ->  pg_isready, psql, pgbench (default path /usr/bin/pgbench)
  stress-ng
  perf / linux-tools (optional; use --no-perf if unavailable).
  For --pgbench-perf-target server, non-root users need passwordless ``sudo -n``
  so ``perf -p`` can attach to postgres PIDs (or run the script as root).
  Remote DB: use ``--pgbench-perf-ssh user@db_host`` so ``perf -p`` runs on the
  server over SSH (needs key-based or agent auth). SSH uses ``BatchMode=yes``, a connect
  timeout, ``PreferredAuthentications=publickey``, ``NumberOfPasswordPrompts=0``, and
  ``IdentitiesOnly=yes`` when ``-i`` is passed under ``--pgbench-perf-ssh-opts``.
  If ``authorized_keys`` uses ``command=...``, it overrides the remote command and breaks
  perf capture (often seen as a dump of bash variables from a stray ``set``).
  The remote command uses a non-login bash (no profile/rc) so perf output is not mixed with
  shell startup noise. Non-root SSH users usually need passwordless ``sudo -n`` for
  ``perf`` on the server, or ``--pgbench-perf-ssh-no-sudo`` when policy allows; for
  ``root@host``, ``perf`` is invoked without a remote ``sudo`` wrap. The DB ``postgres`` OS
  role often has no SSH/shell — use a dedicated benchmark account when possible.
  linpack: build from https://github.com/ereyes01/linpack
    gcc -O3 -o linpack linpack.c -lm

PostgreSQL note: pgbench -i creates pgbench tables inside an existing database; this
script never runs CREATE DATABASE. Empty --pg-user / --pg-database means libpq defaults
(OS user name, default database). Use --pg-host local to omit -h/-p (Unix socket).
Use --skip-pgbench-init if you already ran pgbench -i. Defaults: -c 80 -j 8.
Use either --duration for pgbench -T (default 30s) or --pgbench-transactions for -t
(fixed work per client; no time limit on pgbench).

By default pgbench is run as OS user postgres: sudo -E -u postgres -- pgbench ...
(peer auth). Use --pgbench-no-sudo to run pgbench as the invoking user, or
--pgbench-os-user NAME to pick another OS account. Requires sudo in PATH unless
--pgbench-no-sudo.

Examples:
  python3 vm_benchmark.py --duration 30 --pg-host local --linpack-binary /path/to/linpack
  python3 vm_benchmark.py --duration 30 --pg-user postgres --pg-database mydb
      --pg-password secret --linpack-binary /path/to/linpack -o out.json
  python3 vm_benchmark.py ... --pg-host 192.168.122.10 --pgbench-perf-ssh bench@192.168.122.10
      --linpack-binary /path/to/linpack -o remote.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import select
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional


DEFAULT_LOAD_LEVELS = tuple(range(0, 101, 20))
DEFAULT_SCALE = 50
DEFAULT_CLIENTS = 80
DEFAULT_JOBS = 8

LINPACK_NONZERO_EXIT_NOTE = (
    "Exit status is non-zero but the LINPACK table looks complete. "
    "Upstream ereyes01/linpack often omits 'return 0' in main()."
)


@dataclass
class PerfMetrics:
    duration_sec: Optional[float] = None
    page_faults: Optional[int] = None
    context_switches: Optional[int] = None
    #: Full perf text (stdout+stderr) for server-PID monitoring; used for perf_raw_tail.
    raw_stderr: str = ""
    counter_target: Optional[str] = None
    monitored_pid_count: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "duration_sec": self.duration_sec,
            "page_faults": self.page_faults,
            "context_switches": self.context_switches,
        }
        if self.counter_target:
            d["counter_target"] = self.counter_target
        if self.monitored_pid_count is not None:
            d["monitored_pid_count"] = self.monitored_pid_count
        if self.raw_stderr.strip() and (
            self.duration_sec is None
            and self.page_faults is None
            and self.context_switches is None
        ):
            d["perf_raw_tail"] = self.raw_stderr.strip()[-2000:]
        return d


@dataclass
class BenchRow:
    phase: str
    load_percent: int
    tps: Optional[float] = None
    mflops: Optional[float] = None
    perf: Optional[dict[str, Any]] = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: Optional[str] = None
    exit_code: Optional[int] = None
    exit_note: Optional[str] = None
    network: Optional[dict[str, Any]] = None


@dataclass
class BenchReport:
    environment: str
    #: pgbench -T seconds, or None when using -t (no time limit in pgbench).
    duration_sec: Optional[int] = None
    load_levels: list[int] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    linpack_docker_env: dict[str, str] = field(default_factory=dict)
    linpack_process_env: dict[str, str] = field(default_factory=dict)
    pgbench_transactions_per_client: Optional[int] = None


def _run_cmd(
    cmd: list[str],
    *,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
        env=env,
    )


def _merge_env(extra: Optional[dict[str, str]]) -> dict[str, str]:
    e = os.environ.copy()
    if extra:
        e.update(extra)
    return e


def _net_iface_exists(iface: str) -> bool:
    return os.path.isdir(os.path.join("/sys/class/net", iface))


def read_net_rx_tx_bytes(iface: str) -> tuple[int, int]:
    base = f"/sys/class/net/{iface}/statistics"
    with open(os.path.join(base, "rx_bytes"), encoding="utf-8") as f:
        rx = int(f.read())
    with open(os.path.join(base, "tx_bytes"), encoding="utf-8") as f:
        tx = int(f.read())
    return rx, tx


def net_throughput_dict(
    iface: str,
    t0: float,
    t1: float,
    rx0: int,
    tx0: int,
    rx1: int,
    tx1: int,
) -> dict[str, Any]:
    dt = t1 - t0
    drx = max(0, rx1 - rx0)
    dtx = max(0, tx1 - tx0)
    if dt <= 0:
        rx_mbps = tx_mbps = 0.0
    else:
        rx_mbps = (drx * 8.0) / (dt * 1e6)
        tx_mbps = (dtx * 8.0) / (dt * 1e6)
    return {
        "interface": iface,
        "window_sec": round(dt, 6),
        "rx_bytes_delta": drx,
        "tx_bytes_delta": dtx,
        "rx_mbit_per_s": round(rx_mbps, 6),
        "tx_mbit_per_s": round(tx_mbps, 6),
        "_note": "Counters are for the whole NIC (all processes), not just this benchmark.",
    }


def _wrap_pgbench_for_os_user(cmd: list[str], *, os_user: str) -> list[str]:
    """Run inner command as another OS user (for peer auth as linux postgres)."""
    u = os_user.strip()
    if not u:
        return cmd
    return ["sudo", "-E", "-u", u, "--"] + cmd


def _looks_like_pg_auth_failure(stderr: Optional[str]) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    keys = (
        "password",
        "authentication",
        "peer",
        "fatal",
        "md5",
        "scram",
        "pgpass",
    )
    if any(k in s for k in keys):
        return True
    # Common Russian phrasing: "authentication", "password"
    if "\u043f\u0430\u0440\u043e\u043b" in s:
        return True
    if "\u043f\u043e\u0434\u043b\u0438\u043d\u043d\u043e\u0441\u0442" in s:
        return True
    return False


def _pg_auth_failure_hint() -> str:
    return (
        "\n\nPostgreSQL authentication help:\n"
        "  - TCP (e.g. --pg-host localhost): set the role password, e.g.\n"
        "      export PGPASSWORD='your_secret'\n"
        "    or pass  --pg-password 'your_secret'\n"
        "    or use ~/.pgpass (see the psql documentation).\n"
        "  - Unix socket / peer: use  --pg-host local  and  --pg-user <linux_user>\n"
        "    if PostgreSQL has that role and pg_hba.conf allows peer for local sockets.\n"
        "  - Already initialized DB? Skip init with  --skip-pgbench-init\n"
        "    (you still need a working connection for the benchmark run).\n"
    )


def _use_tcp(host: str) -> bool:
    """If False, use libpq default (usually Unix socket)."""
    return bool(host.strip() and host.strip().lower() not in ("local", "unix"))


def _infer_loopback_network_iface(pg_host: str) -> Optional[str]:
    """Use lo for local DB when --network-iface was not set."""
    h = pg_host.strip().lower()
    if h in ("localhost", "127.0.0.1", "::1", "local", "unix"):
        if _net_iface_exists("lo"):
            return "lo"
    return None


def _pg_host_colocated_for_server_perf(host: str) -> bool:
    """True if Postgres is expected on this machine (socket or loopback TCP)."""
    h = host.strip().lower()
    if not h or h in ("local", "unix"):
        return True
    return h in ("localhost", "127.0.0.1", "::1")


def _ssh_login_user_is_root(ssh_target: str) -> bool:
    """True for ssh targets like root@host (remote perf does not need sudo wrap)."""
    t = ssh_target.strip()
    if "@" not in t:
        return False
    user, _, _ = t.partition("@")
    return user == "root"


def _ssh_capture_looks_like_forced_bash_set_dump(blob: str) -> bool:
    """Heuristic: ssh authorized_keys command= forced a bare ``set`` or similar, not perf."""
    if "Performance counter stats" in blob:
        return False
    return "BASH_EXECUTION_STRING=" in blob and "which ()" in blob


def _native_postgres_host_pids() -> list[int]:
    p = _run_cmd(["pgrep", "-x", "postgres"], timeout=30)
    acc: list[int] = []
    if p.returncode == 0 and p.stdout.strip():
        for line in p.stdout.splitlines():
            t = line.strip()
            if t.isdigit():
                acc.append(int(t))
    if acc:
        return sorted(set(acc))
    p2 = _run_cmd(["pidof", "postgres"], timeout=30)
    if p2.returncode != 0 or not p2.stdout.strip():
        return []
    for t in p2.stdout.split():
        st = t.strip()
        if st.isdigit():
            acc.append(int(st))
    return sorted(set(acc))


def _pg_conn_args(
    host: str, port: int, user: str, db: str
) -> list[str]:
    """Build libpq flags; omit -U/-d when user or db is empty (libpq defaults)."""
    parts: list[str] = []
    if _use_tcp(host):
        parts.extend(["-h", host.strip(), "-p", str(port)])
    if user.strip():
        parts.extend(["-U", user.strip()])
    if db.strip():
        parts.extend(["-d", db.strip()])
    return parts


def wait_postgres_ready(
    host: str,
    port: int,
    user: str,
    *,
    env: Optional[dict[str, str]],
    timeout: float = 120.0,
    pg_isready_cmd_timeout: float = 30.0,
    verbose: bool = False,
) -> None:
    deadline = time.time() + timeout
    last_msg = 0.0
    if verbose:
        loc = (
            f"{host.strip()}:{port}"
            if _use_tcp(host)
            else (
                f"socket (user={user.strip()!r})"
                if user.strip()
                else "socket (default OS user)"
            )
        )
        print(
            f"vm_benchmark: waiting for PostgreSQL at {loc} (overall up to {timeout:.0f}s, "
            f"each pg_isready up to {pg_isready_cmd_timeout:.0f}s)...",
            file=sys.stderr,
            flush=True,
        )
    last_msg = time.time()
    while time.time() < deadline:
        if verbose and (time.time() - last_msg >= 15.0):
            print(
                "vm_benchmark: still waiting for PostgreSQL (pg_isready)...",
                file=sys.stderr,
                flush=True,
            )
            last_msg = time.time()
        cmd = ["pg_isready"]
        if user.strip():
            cmd.extend(["-U", user.strip()])
        if _use_tcp(host):
            cmd.extend(["-h", host.strip(), "-p", str(port)])
        try:
            p = _run_cmd(cmd, env=env, timeout=pg_isready_cmd_timeout)
        except subprocess.TimeoutExpired:
            if verbose:
                print(
                    "vm_benchmark: pg_isready subprocess timed out "
                    f"(>{pg_isready_cmd_timeout:.0f}s); retrying.",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(1)
            continue
        if p.returncode == 0:
            if verbose:
                print("vm_benchmark: PostgreSQL is accepting connections.", file=sys.stderr, flush=True)
            return
        time.sleep(1)
    if _use_tcp(host):
        loc = f"{host}:{port}"
    else:
        loc = f"socket (user={user.strip()!r})" if user.strip() else "socket (default OS user)"
    raise TimeoutError(f"PostgreSQL not ready at {loc}.")


def init_pgbench(
    host: str,
    port: int,
    user: str,
    database: str,
    scale: int,
    *,
    pgbench_bin: str,
    pgbench_os_user: str,
    env: Optional[dict[str, str]],
    verbose: bool = False,
) -> None:
    cmd = [
        pgbench_bin,
        "-i",
        "-s",
        str(scale),
        *_pg_conn_args(host, port, user, database),
    ]
    cmd = _wrap_pgbench_for_os_user(cmd, os_user=pgbench_os_user)
    if verbose:
        print(
            "vm_benchmark: running pgbench -i (scale=%d); on slow disks this can take many minutes. "
            "If this hangs with no CPU use, sudo may be waiting for a password use "
            "NOPASSWD for the target user, run from a tty, or --pgbench-no-sudo."
            % scale,
            file=sys.stderr,
            flush=True,
        )
    p = _run_cmd(cmd, timeout=3600, env=env)
    if verbose:
        print("vm_benchmark: pgbench -i finished.", file=sys.stderr, flush=True)
    if p.returncode != 0:
        err = f"pgbench -i failed: {p.stderr}"
        if _looks_like_pg_auth_failure(p.stderr):
            err += _pg_auth_failure_hint()
        raise RuntimeError(err)


def parse_pgbench_tps(text: str) -> Optional[float]:
    m = re.search(r"tps\s*=\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _linpack_bench_text_only(text: str) -> str:
    head, sep, _ = text.partition("Performance counter stats")
    if sep:
        return head
    return text


def parse_linpack_mflops(text: str) -> Optional[float]:
    bench = _linpack_bench_text_only(text)
    for pat in (
        r"([\d.eE+-]+)\s*MFLOPS",
        r"MFLOPS\s*[=:]\s*([\d.eE+-]+)",
        r"mflops\s*[=:]\s*([\d.eE+-]+)",
        r"Speed\s*:?\s*([\d.eE+-]+)",
    ):
        m = re.search(pat, bench, re.IGNORECASE | re.MULTILINE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    row_re = re.compile(
        r"^\s*\d+\s+[\d.]+\s+[\d.]+%\s+[\d.]+%\s+[\d.]+%\s+([\d.]+)\s*$",
        re.MULTILINE,
    )
    matches = row_re.findall(bench)
    if matches:
        return float(matches[-1]) / 1000.0
    return None


def _linpack_run_ok(code: int, text: str, mflops: Optional[float]) -> bool:
    if code == 0:
        return True
    bench = _linpack_bench_text_only(text)
    if mflops is None or "LINPACK benchmark" not in bench or "KFLOPS" not in bench:
        return False
    return True


def _perf_normalize_int(raw: str) -> int:
    t = re.sub(r"[\s\u202f]", "", raw)
    t = t.replace(",", "")
    return int(t, 10)


def _perf_normalize_elapsed(raw: str, unit: str) -> float:
    t = re.sub(r"[\s\u202f]", "", raw.strip())
    if "," in t and "." in t:
        t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    val = float(t)
    u = unit.lower()
    if u.startswith("msec"):
        return val / 1000.0
    if u.startswith("usec"):
        return val / 1_000_000.0
    if u.startswith("sec"):
        return val
    return val


def _parse_perf_stat(stderr: str) -> PerfMetrics:
    metrics = PerfMetrics()
    m = re.search(
        r"([0-9][0-9\u202f\s,.]*)\s+(msec|seconds?|sec|usec)\s+time elapsed",
        stderr,
        re.IGNORECASE,
    )
    if m:
        try:
            metrics.duration_sec = _perf_normalize_elapsed(m.group(1), m.group(2))
        except ValueError:
            pass
    m = re.search(r"([\d\u202f\s,]+)\s+page-faults", stderr)
    if m:
        try:
            metrics.page_faults = _perf_normalize_int(m.group(1))
        except ValueError:
            pass
    m = re.search(r"([\d\u202f\s,]+)\s+context-switches", stderr)
    if m:
        try:
            metrics.context_switches = _perf_normalize_int(m.group(1))
        except ValueError:
            pass
    return metrics


def _perf_monitor_elevation_prefix(env: Optional[dict[str, str]] = None) -> list[str]:
    if getattr(os, "geteuid", lambda: 0)() == 0:
        return []
    r = _run_cmd(["sudo", "-n", "true"], timeout=5, env=env)
    if r.returncode == 0:
        return ["sudo", "-n", "--"]
    return []


def _signal_perf_process_group(perf_proc: subprocess.Popen[str], sig: int) -> None:
    pid = perf_proc.pid
    if not pid:
        return
    if hasattr(os, "killpg"):
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    try:
        perf_proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _stop_perf_monitor_and_read(perf_proc: subprocess.Popen[str]) -> str:
    """
    Stop ``perf stat … -- sleep`` without ``communicate()`` (can hang if children ignore
    SIGTERM). Send SIGINT to the whole session, drain stdout, then SIGTERM / SIGKILL.
    """
    chunks: list[str] = []
    stdout = perf_proc.stdout

    def drain_available() -> None:
        if not stdout:
            return
        try:
            r, _, _ = select.select([stdout], [], [], 0.0)
            if r:
                chunks.append(stdout.read(1024 * 1024))
        except (ValueError, OSError, TypeError):
            pass

    _signal_perf_process_group(perf_proc, signal.SIGINT)
    deadline = time.time() + 50.0
    while time.time() < deadline:
        drain_available()
        if perf_proc.poll() is not None:
            break
        time.sleep(0.05)

    if perf_proc.poll() is None:
        _signal_perf_process_group(perf_proc, signal.SIGTERM)
        time.sleep(0.6)
        drain_available()

    if perf_proc.poll() is None:
        _signal_perf_process_group(perf_proc, signal.SIGKILL)

    try:
        perf_proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass

    if stdout:
        try:
            tail = stdout.read()
            if tail:
                chunks.append(tail)
        except Exception:
            pass
        try:
            stdout.close()
        except Exception:
            pass
    return "".join(chunks)


def run_with_perf(
    base_cmd: list[str],
    *,
    use_perf: bool = True,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
    prefix_cmd: Optional[list[str]] = None,
    counter_target: Optional[str] = None,
) -> tuple[int, str, str, Optional[PerfMetrics]]:
    """
    Run base_cmd optionally under perf stat.

    If prefix_cmd is set (e.g. sudo -u postgres --), it is placed *before* perf so that
    perf's direct child is pgbench, not sudo; otherwise page-faults / context-switches
    often stay at zero while only duration_time grows.
    """
    prefix = list(prefix_cmd) if prefix_cmd else []
    if use_perf:
        perf_check = _run_cmd(["perf", "stat", "true"], env=env)
        if perf_check.returncode != 0:
            use_perf = False
    if use_perf:
        inner = [
            "perf",
            "stat",
            "-e",
            "duration_time,page-faults,context-switches",
            "-B",
            "--",
        ] + base_cmd
        cmd = prefix + inner
    else:
        cmd = prefix + list(base_cmd)
    p = _run_cmd(cmd, timeout=timeout, env=env)
    perf_m: Optional[PerfMetrics] = None
    if use_perf:
        perf_m = _parse_perf_stat(p.stderr)
        if perf_m and counter_target:
            perf_m.counter_target = counter_target
    return p.returncode, p.stdout, p.stderr, perf_m


def run_with_perf_monitor_pids(
    workload_cmd: list[str],
    pids: list[int],
    *,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
    counter_target: str = "postgres_host_pids",
) -> tuple[int, str, str, Optional[PerfMetrics]]:
    """perf stat -p … for Postgres PIDs while workload_cmd runs (no perf around pgbench)."""
    pids = sorted(set(pids))
    if not pids:
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, None
    elev = _perf_monitor_elevation_prefix(env)
    if not elev and getattr(os, "geteuid", lambda: 0)() != 0:
        print(
            "pgbench (server perf): not root and `sudo -n` unavailable; "
            "`perf -p` on postgres PIDs may yield empty metrics. "
            "Allow NOPASSWD for sudo, run as root, or use --pgbench-perf-target client.",
            file=sys.stderr,
        )
    perf_check = _run_cmd(elev + ["perf", "stat", "true"], env=env)
    if perf_check.returncode != 0:
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, None
    pid_arg = ",".join(str(x) for x in pids)
    perf_cmd = elev + [
        "perf",
        "stat",
        "-e",
        "duration_time,page-faults,context-switches",
        "-B",
        "-p",
        pid_arg,
        "--",
        "sleep",
        "86400",
    ]
    perf_proc = subprocess.Popen(
        perf_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        rc, out, err = p.returncode, p.stdout, p.stderr
    finally:
        blob = _stop_perf_monitor_and_read(perf_proc)
    perf_m = _parse_perf_stat(blob)
    perf_m.counter_target = counter_target
    perf_m.monitored_pid_count = len(pids)
    perf_m.raw_stderr = blob
    if (
        perf_m.duration_sec is None
        and perf_m.page_faults is None
        and perf_m.context_switches is None
        and not blob.strip()
    ):
        print(
            "pgbench (server perf): perf produced no output after the run. "
            "See kernel.perf_event_paranoid and permissions on perf -p.",
            file=sys.stderr,
        )
    return rc, out, err, perf_m


def run_with_perf_monitor_pids_remote_ssh(
    workload_cmd: list[str],
    *,
    ssh_target: str,
    ssh_extra: list[str],
    remote_use_sudo: bool,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
    counter_target: str = "postgres_server_ssh",
) -> tuple[int, str, str, Optional[PerfMetrics]]:
    """
    Run workload locally while ``perf stat -p $(pgrep -x postgres)`` runs on ``ssh_target``.

    Mirrors :func:`run_with_perf_monitor_pids`: long remote ``sleep`` under perf,
    stop via SIGINT when the local workload finishes.
    """
    use_sudo_on_remote = remote_use_sudo and not _ssh_login_user_is_root(ssh_target)
    perf_inv = (
        "sudo -n -- perf stat -e duration_time,page-faults,context-switches "
        '-B -p "$PIDS" -- sleep 86400'
        if use_sudo_on_remote
        else (
            "perf stat -e duration_time,page-faults,context-switches "
            '-B -p "$PIDS" -- sleep 86400'
        )
    )
    remote_script = (
        "set -e; "
        r'PIDS=$(pgrep -x postgres 2>/dev/null | tr "\n" "," | sed "s/,$//"); '
        'test -n "$PIDS" || exit 3; '
        "exec " + perf_inv
    )
    ssh_trailer = [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=25",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "NumberOfPasswordPrompts=0",
    ]
    if "-i" in ssh_extra:
        ssh_trailer.extend(["-o", "IdentitiesOnly=yes"])
    ssh_cmd = (
        ["ssh", "-T"]
        + list(ssh_extra)
        + ssh_trailer
        + [ssh_target, "bash", "--noprofile", "--norc", "-c", remote_script]
    )
    try:
        perf_proc = subprocess.Popen(
            ssh_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        print(
            f"pgbench (remote server perf): ssh failed ({exc}); running without perf.",
            file=sys.stderr,
            flush=True,
        )
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, None

    time.sleep(0.2)
    if perf_proc.poll() is not None:
        early = ""
        if perf_proc.stdout:
            try:
                early = perf_proc.stdout.read()
            except Exception:
                pass
        print(
            "pgbench (remote server perf): could not start monitoring on DB host "
            "(no postgres PIDs, ssh error, or perf/sudo failed). "
            f"Remote output (first 800 chars): {early[:800]!r}",
            file=sys.stderr,
            flush=True,
        )
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr, None

    try:
        p = _run_cmd(workload_cmd, timeout=timeout, env=env)
        rc, out, err = p.returncode, p.stdout, p.stderr
    finally:
        blob = _stop_perf_monitor_and_read(perf_proc)

    perf_m = _parse_perf_stat(blob)
    perf_m.counter_target = counter_target
    perf_m.raw_stderr = blob
    if (
        perf_m.duration_sec is None
        and perf_m.page_faults is None
        and perf_m.context_switches is None
        and _ssh_capture_looks_like_forced_bash_set_dump(blob)
    ):
        print(
            "pgbench (remote server perf): captured output looks like a bare bash 'set' dump, "
            "not perf. On the DB host, check ~/.ssh/authorized_keys for this key: a "
            "'command=...' option overrides the remote command (often a typo or bad paste). "
            "Remove forced command or use a key without it; advanced setups can wrap "
            "$SSH_ORIGINAL_COMMAND per OpenSSH docs.",
            file=sys.stderr,
            flush=True,
        )
    if (
        perf_m.duration_sec is None
        and perf_m.page_faults is None
        and perf_m.context_switches is None
        and not blob.strip()
    ):
        print(
            "pgbench (remote server perf): empty perf output. "
            "On the DB host check perf, kernel.perf_event_paranoid, and sudo -n for perf.",
            file=sys.stderr,
            flush=True,
        )
    return rc, out, err, perf_m


class NativeStressController:
    """Host stress-ng subprocess (not Docker)."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen[Any]] = None

    def start(self, load_percent: int) -> None:
        self.stop()
        if load_percent <= 0:
            return
        self._proc = subprocess.Popen(
            [
                "stress-ng",
                "--cpu",
                "0",
                "--cpu-load",
                str(load_percent),
                "--timeout",
                "0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if not self._proc:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None


def report_to_json(report: BenchReport) -> str:
    d: dict[str, Any] = {
        "environment": report.environment,
        "duration_sec": report.duration_sec,
        "load_levels": report.load_levels,
        "rows": report.rows,
        "linpack_docker_env": report.linpack_docker_env,
        "linpack_process_env": report.linpack_process_env,
    }
    if report.pgbench_transactions_per_client is not None:
        d["pgbench_transactions_per_client"] = report.pgbench_transactions_per_client
    return json.dumps(d, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--duration",
        type=int,
        default=None,
        metavar="SEC",
        help=(
            "pgbench -T: time limit in seconds (default 30 when not using --pgbench-transactions). "
            "With --pgbench-transactions, pgbench runs a fixed -t workload only (no -T)."
        ),
    )
    ap.add_argument(
        "--pgbench-transactions",
        type=int,
        default=None,
        metavar="N",
        help=(
            "pgbench -t: each client runs N transactions (no -T; run ends when work is done)."
        ),
    )
    ap.add_argument(
        "--pg-host",
        default="localhost",
        help="PostgreSQL host, or 'local' for default Unix socket (omit -h/-p).",
    )
    ap.add_argument("--pg-port", type=int, default=5432)
    ap.add_argument(
        "--pgbench-binary",
        default="/usr/bin/pgbench",
        metavar="PATH",
        help="pgbench executable (default: /usr/bin/pgbench).",
    )
    ap.add_argument(
        "--pgbench-os-user",
        default="postgres",
        metavar="USER",
        help=(
            "Run pgbench as this OS user via sudo -E -u (default: postgres; "
            "use with --pgbench-no-sudo to run as the invoking user)."
        ),
    )
    ap.add_argument(
        "--pgbench-no-sudo",
        action="store_true",
        help="Do not wrap pgbench in sudo (run as current user).",
    )
    ap.add_argument(
        "--pg-user",
        default="",
        metavar="NAME",
        help="PostgreSQL role (empty = libpq default, usually current OS user).",
    )
    ap.add_argument(
        "--pg-database",
        default="",
        metavar="NAME",
        help="Database name (empty = libpq default, often same as role name).",
    )
    ap.add_argument(
        "--pg-maintenance-db",
        default="",
        metavar="NAME",
        help="Unused: this script never runs CREATE DATABASE.",
    )
    ap.add_argument(
        "--pg-password",
        default="",
        help="If set, passed as PGPASSWORD for this process (avoid on shared systems).",
    )
    ap.add_argument("--scale", type=int, default=DEFAULT_SCALE, help="pgbench -i -s")
    ap.add_argument("--clients", type=int, default=DEFAULT_CLIENTS, help="pgbench -c")
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="pgbench -j (threads)")
    ap.add_argument(
        "--linpack-binary",
        required=True,
        help="Path to ereyes01/linpack executable (build on this VM).",
    )
    ap.add_argument(
        "--linpack-array-size",
        type=int,
        default=None,
        metavar="N",
        help="Set LINPACK_ARRAY_SIZE in linpack's environment (>= 10).",
    )
    ap.add_argument(
        "--loads",
        default="",
        help="Comma-separated CPU load percents (default 0,20,...,100)",
    )
    ap.add_argument("--no-perf", action="store_true")
    ap.add_argument(
        "--pgbench-perf-target",
        choices=("server", "client"),
        default="server",
        help=(
            "pgbench: perf samples PostgreSQL server PIDs (default) while pgbench runs; "
            "use client to wrap pgbench. Local server mode uses pgrep on this host; remote "
            "TCP needs --pgbench-perf-ssh unless you accept client-side perf."
        ),
    )
    ap.add_argument(
        "--pgbench-perf-ssh",
        default="",
        metavar="USER@HOST",
        help=(
            "With --pgbench-perf-target server and a remote --pg-host, run perf -p on this "
            "SSH user@host (root not required). Appends -o BatchMode=yes -o ConnectTimeout=25 "
            "-o PreferredAuthentications=publickey -o NumberOfPasswordPrompts=0 "
            "(and IdentitiesOnly=yes if -i is present in --pgbench-perf-ssh-opts) after your "
            "ssh opts (openssh: first -o wins). On the server: perf must trace postgres PIDs "
            "(NOPASSWD sudo for perf as non-root, or use --pgbench-perf-ssh-no-sudo; root@ "
            "skips sudo on the remote side). The cluster OS user postgres often cannot SSH."
        ),
    )
    ap.add_argument(
        "--pgbench-perf-ssh-opts",
        default="",
        metavar="ARGS",
        help='Extra ssh arguments as one shell-quoted string (e.g. -i /path/key -p 2222).',
    )
    ap.add_argument(
        "--pgbench-perf-ssh-no-sudo",
        action="store_true",
        help=(
            "On the DB host, run perf without sudo -n (use when perf can attach to postgres "
            "PIDs as the SSH user, e.g. relaxed kernel.perf_event_paranoid or matching caps)."
        ),
    )
    ap.add_argument(
        "--no-stress",
        action="store_true",
        help="Do not run stress-ng (all load levels run with 0%% background load).",
    )
    ap.add_argument(
        "--skip-create-database",
        action="store_true",
        help="No-op (kept for compatibility; CREATE DATABASE is never run).",
    )
    ap.add_argument(
        "--skip-pgbench-init",
        action="store_true",
        help="Skip pgbench -i (benchmark tables must already exist).",
    )
    ap.add_argument(
        "--network-iface",
        default="",
        metavar="IFACE",
        help=(
            "If set (e.g. eth0), record NIC RX/TX from /sys during each pgbench/linpack phase. "
            "If empty and --pg-host is localhost/127.0.0.1/::1/local, lo is used automatically."
        ),
    )
    ap.add_argument("-o", "--output", default="")
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Log startup phases (PostgreSQL wait, pgbench -i) to stderr for debugging.",
    )
    args = ap.parse_args()

    _ident = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

    def _require_ident(label: str, value: str) -> None:
        if not value.strip():
            return
        if not _ident.match(value.strip()):
            print(
                f"{label} must be a simple identifier (letters, digits, underscore).",
                file=sys.stderr,
            )
            sys.exit(2)

    _require_ident("--pg-database", args.pg_database)
    _require_ident("--pg-maintenance-db", args.pg_maintenance_db)

    if args.linpack_array_size is not None and args.linpack_array_size < 10:
        print("--linpack-array-size must be >= 10", file=sys.stderr)
        sys.exit(2)

    if args.pgbench_transactions is not None and args.pgbench_transactions < 1:
        print("--pgbench-transactions must be >= 1", file=sys.stderr)
        sys.exit(2)

    if args.pgbench_transactions is None:
        if args.duration is None:
            args.duration = 30
    elif args.duration is not None:
        print(
            "Note: --duration is not used when --pgbench-transactions is set "
            "(pgbench uses -t only, no -T).",
            file=sys.stderr,
        )

    linpack_path = os.path.abspath(args.linpack_binary)
    if not os.path.isfile(linpack_path) or not os.access(linpack_path, os.X_OK):
        print(f"Not an executable file: {linpack_path!r}", file=sys.stderr)
        sys.exit(2)

    pgbench_path = os.path.abspath(args.pgbench_binary)
    if not os.path.isfile(pgbench_path) or not os.access(pgbench_path, os.X_OK):
        print(f"Not an executable file: {pgbench_path!r}", file=sys.stderr)
        sys.exit(2)

    pgbench_os_user = "" if args.pgbench_no_sudo else args.pgbench_os_user.strip()
    if pgbench_os_user:
        if _run_cmd(["bash", "-lc", "command -v sudo"]).returncode != 0:
            print(
                "sudo not found in PATH but --pgbench-os-user is set. "
                "Install sudo or use --pgbench-no-sudo.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not args.no_stress:
        if _run_cmd(["bash", "-lc", "command -v stress-ng"]).returncode != 0:
            print(
                "stress-ng not found in PATH. Install it or use --no-stress.",
                file=sys.stderr,
            )
            sys.exit(1)

    network_iface = args.network_iface.strip() or None
    if not network_iface:
        auto_lo = _infer_loopback_network_iface(args.pg_host)
        if auto_lo:
            network_iface = auto_lo
            if not _use_tcp(args.pg_host):
                print(
                    "Note: --pg-host local uses Unix sockets; lo RX/TX does not include "
                    "Postgres traffic. Use --pg-host 127.0.0.1 (TCP) to measure DB on lo.",
                    file=sys.stderr,
                )
    if network_iface and not _net_iface_exists(network_iface):
        print(
            f"Warning: --network-iface {network_iface!r} not found; skipping NIC stats.",
            file=sys.stderr,
        )
        network_iface = None

    perf_ssh_target = (args.pgbench_perf_ssh or "").strip()
    perf_ssh_extra: list[str] = []
    if perf_ssh_target:
        if _run_cmd(["bash", "-lc", "command -v ssh"]).returncode != 0:
            print("ssh not found in PATH but --pgbench-perf-ssh is set.", file=sys.stderr)
            sys.exit(1)
        try:
            perf_ssh_extra = shlex.split(args.pgbench_perf_ssh_opts or "")
        except ValueError as exc:
            print(f"Could not parse --pgbench-perf-ssh-opts: {exc}", file=sys.stderr)
            sys.exit(2)
        if (args.pgbench_perf_target or "server").lower() == "client":
            print(
                "Warning: --pgbench-perf-ssh is ignored with --pgbench-perf-target client.",
                file=sys.stderr,
            )
        elif _pg_host_colocated_for_server_perf(args.pg_host):
            print(
                "Note: --pgbench-perf-ssh ignored for colocated --pg-host "
                "(local server perf is used).",
                file=sys.stderr,
            )

    extra_pg: dict[str, str] = {}
    if args.pg_password:
        extra_pg["PGPASSWORD"] = args.pg_password
    elif os.environ.get("PGPASSWORD"):
        pass
    else:
        u = args.pg_user.strip() or "default OS user"
        print(
            "Hint: TCP (--pg-host localhost) usually needs a password for the DB role "
            f"({u!r}; export PGPASSWORD, --pg-password, or ~/.pgpass). "
            "For peer auth on a socket: --pg-host local (omit --pg-user to use your "
            "Linux login).",
            file=sys.stderr,
        )

    pg_env = _merge_env(extra_pg)

    load_levels = (
        [int(x) for x in args.loads.split(",") if x.strip()]
        if args.loads
        else list(DEFAULT_LOAD_LEVELS)
    )

    wait_postgres_ready(
        args.pg_host,
        args.pg_port,
        args.pg_user,
        env=pg_env,
        verbose=args.verbose,
    )
    if args.verbose:
        print(
            f"vm_benchmark: entering benchmark loop, load levels {load_levels}.",
            file=sys.stderr,
            flush=True,
        )
    if not args.skip_pgbench_init:
        init_pgbench(
            args.pg_host,
            args.pg_port,
            args.pg_user,
            args.pg_database,
            args.scale,
            pgbench_bin=pgbench_path,
            pgbench_os_user=pgbench_os_user,
            env=pg_env,
            verbose=args.verbose,
        )

    linpack_env_map: dict[str, str] = {}
    if args.linpack_array_size is not None:
        linpack_env_map["LINPACK_ARRAY_SIZE"] = str(args.linpack_array_size)
    elif os.environ.get("LINPACK_ARRAY_SIZE"):
        linpack_env_map["LINPACK_ARRAY_SIZE"] = os.environ["LINPACK_ARRAY_SIZE"]
    linpack_run_env = _merge_env(linpack_env_map)

    report = BenchReport(
        environment="native_vm",
        duration_sec=None if args.pgbench_transactions is not None else args.duration,
        load_levels=list(load_levels),
        linpack_process_env=dict(linpack_env_map),
        pgbench_transactions_per_client=args.pgbench_transactions,
    )
    stress = NativeStressController()

    def _pgbench_cmd() -> list[str]:
        cmd = [
            pgbench_path,
            "-c",
            str(args.clients),
            "-j",
            str(args.jobs),
        ]
        if args.pgbench_transactions is not None:
            cmd.extend(["-t", str(args.pgbench_transactions)])
        else:
            cmd.extend(["-T", str(args.duration)])
        cmd.extend(
            _pg_conn_args(args.pg_host, args.pg_port, args.pg_user, args.pg_database)
        )
        return cmd

    def _pgbench_timeout_sec() -> float:
        if args.pgbench_transactions is None:
            return float(args.duration) + 120.0
        c = max(1, args.clients)
        t = max(1, args.pgbench_transactions)
        return max(300.0, min(86400.0, float(t) * float(c) * 0.02 + 120.0))

    pgbench_sudo_prefix: Optional[list[str]] = (
        ["sudo", "-E", "-u", pgbench_os_user, "--"] if pgbench_os_user else None
    )

    try:
        for load in load_levels:
            print(f"--- Load {load}%: pgbench ---", file=sys.stderr, flush=True)
            if not args.no_stress:
                stress.start(load)
            try:
                net_p: Optional[dict[str, Any]] = None
                if network_iface:
                    rx0, tx0 = read_net_rx_tx_bytes(network_iface)
                    t0 = time.time()
                def _full_pgbench_argv() -> list[str]:
                    return (pgbench_sudo_prefix or []) + _pgbench_cmd()

                p_target = (args.pgbench_perf_target or "server").lower()
                coloc = _pg_host_colocated_for_server_perf(args.pg_host)
                use_server_perf = not args.no_perf and p_target == "server" and coloc
                use_remote_ssh_perf = (
                    not args.no_perf
                    and p_target == "server"
                    and not coloc
                    and bool(perf_ssh_target)
                )
                to = _pgbench_timeout_sec()
                if args.no_perf:
                    p = _run_cmd(_full_pgbench_argv(), timeout=to, env=pg_env)
                    code, out, err, perf_m = p.returncode, p.stdout, p.stderr, None
                elif use_remote_ssh_perf:
                    print(
                        f"vm_benchmark: remote server perf: opening SSH to {perf_ssh_target!r} "
                        "(see --pgbench-perf-ssh / ssh BatchMode + timeouts), "
                        "then starting pgbench.",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        code, out, err, perf_m = run_with_perf_monitor_pids_remote_ssh(
                            _full_pgbench_argv(),
                            ssh_target=perf_ssh_target,
                            ssh_extra=perf_ssh_extra,
                            remote_use_sudo=not args.pgbench_perf_ssh_no_sudo,
                            timeout=to,
                            env=pg_env,
                        )
                    except OSError as exc:
                        print(
                            f"pgbench: remote server perf failed ({exc}); "
                            "running without perf.",
                            file=sys.stderr,
                        )
                        p = _run_cmd(_full_pgbench_argv(), timeout=to, env=pg_env)
                        code, out, err, perf_m = (
                            p.returncode,
                            p.stdout,
                            p.stderr,
                            None,
                        )
                elif use_server_perf:
                    pids = _native_postgres_host_pids()
                    if not pids:
                        print(
                            "pgbench: no local postgres PIDs (pgrep); running without perf.",
                            file=sys.stderr,
                        )
                        p = _run_cmd(_full_pgbench_argv(), timeout=to, env=pg_env)
                        code, out, err, perf_m = p.returncode, p.stdout, p.stderr, None
                    else:
                        try:
                            code, out, err, perf_m = run_with_perf_monitor_pids(
                                _full_pgbench_argv(),
                                pids,
                                timeout=to,
                                env=pg_env,
                            )
                        except OSError as exc:
                            print(
                                f"pgbench: server perf failed ({exc}); "
                                "running without perf.",
                                file=sys.stderr,
                            )
                            p = _run_cmd(_full_pgbench_argv(), timeout=to, env=pg_env)
                            code, out, err, perf_m = (
                                p.returncode,
                                p.stdout,
                                p.stderr,
                                None,
                            )
                elif not coloc and p_target == "server":
                    print(
                        f"pgbench: --pg-host {args.pg_host!r} is not on this machine; "
                        "using perf on the pgbench client.",
                        file=sys.stderr,
                    )
                    code, out, err, perf_m = run_with_perf(
                        _pgbench_cmd(),
                        use_perf=True,
                        timeout=to,
                        env=pg_env,
                        prefix_cmd=pgbench_sudo_prefix,
                        counter_target="pgbench_client",
                    )
                else:
                    code, out, err, perf_m = run_with_perf(
                        _pgbench_cmd(),
                        use_perf=True,
                        timeout=to,
                        env=pg_env,
                        prefix_cmd=pgbench_sudo_prefix,
                        counter_target="pgbench_client",
                    )
                if network_iface:
                    t1 = time.time()
                    rx1, tx1 = read_net_rx_tx_bytes(network_iface)
                    net_p = net_throughput_dict(
                        network_iface, t0, t1, rx0, tx0, rx1, tx1
                    )
                txt = out + err
                tps = parse_pgbench_tps(txt)
                err_p = None if code == 0 else f"pgbench exit {code}"
                report.rows.append(
                    asdict(
                        BenchRow(
                            phase="pgbench",
                            load_percent=load,
                            tps=tps,
                            perf=perf_m.to_dict() if perf_m else None,
                            stdout_tail=txt[-2000:],
                            error=err_p,
                            exit_code=code,
                            exit_note=None,
                            network=net_p,
                        )
                    )
                )
            finally:
                stress.stop()

            print(f"--- Load {load}%: linpack ---", file=sys.stderr, flush=True)
            if not args.no_stress:
                stress.start(load)
            try:
                net_l: Optional[dict[str, Any]] = None
                if network_iface:
                    rx0, tx0 = read_net_rx_tx_bytes(network_iface)
                    t0 = time.time()
                code, out, err, perf_m = run_with_perf(
                    [linpack_path],
                    use_perf=not args.no_perf,
                    timeout=600.0,
                    env=linpack_run_env,
                    counter_target="linpack",
                )
                if network_iface:
                    t1 = time.time()
                    rx1, tx1 = read_net_rx_tx_bytes(network_iface)
                    net_l = net_throughput_dict(
                        network_iface, t0, t1, rx0, tx0, rx1, tx1
                    )
                text = out + err
                mflops = parse_linpack_mflops(text)
                ok = _linpack_run_ok(code, text, mflops)
                err_l = None if ok else f"linpack exit {code}"
                lp_note = (
                    LINPACK_NONZERO_EXIT_NOTE if ok and code != 0 else None
                )
                if lp_note:
                    print(
                        f"linpack: exit_code={code} (see JSON exit_note); "
                        "treating run as successful.",
                        file=sys.stderr,
                    )
                report.rows.append(
                    asdict(
                        BenchRow(
                            phase="linpack",
                            load_percent=load,
                            mflops=mflops,
                            perf=perf_m.to_dict() if perf_m else None,
                            stdout_tail=text[-2000:],
                            error=err_l,
                            exit_code=code,
                            exit_note=lp_note,
                            network=net_l,
                        )
                    )
                )
            finally:
                stress.stop()
    finally:
        stress.stop()

    text = report_to_json(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
