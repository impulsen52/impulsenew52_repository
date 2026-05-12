"""
Shared sequential benchmark logic: pgbench then linpack at CPU load levels 0-100%,
with optional perf stat metrics (duration, page-faults, context-switches).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional


DEFAULT_LOAD_LEVELS = tuple(range(0, 101, 20))
DEFAULT_PGBENCH_SCALE = 50
DEFAULT_PGBENCH_CLIENTS = 80
DEFAULT_PGBENCH_JOBS = 8
STRESS_IMAGE = os.environ.get("STRESS_NG_IMAGE", "alexeiled/stress-ng")
LINPACK_REPO = os.environ.get(
    "LINPACK_REPO", "https://github.com/ereyes01/linpack.git"
)
DEFAULT_LINPACK_IMAGE = "linpack-benchmark"
# Passed into the linpack container as -e LINPACK_ARRAY_SIZE (smaller => shorter runs).
LINPACK_ARRAY_SIZE_ENV = "LINPACK_ARRAY_SIZE"


@dataclass
class PerfMetrics:
    duration_sec: Optional[float] = None
    page_faults: Optional[int] = None
    context_switches: Optional[int] = None
    raw_stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_sec": self.duration_sec,
            "page_faults": self.page_faults,
            "context_switches": self.context_switches,
        }


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
    #: Raw exit code of the benchmarked command (after perf, if used).
    exit_code: Optional[int] = None
    #: Set when exit_code != 0 but the row is still treated as success (e.g. linpack quirk).
    exit_note: Optional[str] = None
    #: Optional /sys/class/net/<iface> throughput during this phase (all traffic on NIC).
    network: Optional[dict[str, Any]] = None


@dataclass
class BenchReport:
    environment: str
    duration_sec: int
    load_levels: list[int] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    linpack_docker_env: dict[str, str] = field(default_factory=dict)


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


def _run_cmd(
    cmd: list[str],
    *,
    timeout: Optional[float] = None,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    env_use: Optional[dict[str, str]] = None
    if env is not None:
        env_use = os.environ.copy()
        env_use.update(env)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
        env=env_use,
    )


def _docker_exec_cmd(
    container: str,
    inner: list[str],
    *,
    exec_env: Optional[dict[str, str]] = None,
) -> list[str]:
    """docker exec [ -e K=V ... ] CONTAINER INNER... (env vars visible inside container)."""
    cmd: list[str] = ["docker", "exec"]
    if exec_env:
        for key in sorted(exec_env.keys()):
            cmd.extend(["-e", f"{key}={exec_env[key]}"])
    cmd.append(container)
    cmd.extend(inner)
    return cmd


def docker_available() -> bool:
    p = _run_cmd(["docker", "version"])
    return p.returncode == 0


def ensure_docker_image(image: str) -> None:
    p = _run_cmd(["docker", "image", "inspect", image])
    if p.returncode != 0:
        print(f"Pulling Docker image {image!r}...", file=sys.stderr)
        p = _run_cmd(["docker", "pull", image])
        if p.returncode != 0:
            raise RuntimeError(f"docker pull {image} failed: {p.stderr}")


def ensure_linpack_image(
    image_name: str,
    *,
    clone_dir: Optional[str] = None,
    skip_build: bool = False,
) -> None:
    p = _run_cmd(["docker", "image", "inspect", image_name])
    if p.returncode == 0:
        return
    if skip_build:
        raise RuntimeError(
            f"Linpack image {image_name!r} missing and --skip-linpack-build set."
        )
    import tempfile

    tmp = clone_dir or tempfile.mkdtemp(prefix="linpack-src-")
    if not os.path.isdir(os.path.join(tmp, ".git")):
        print(f"Cloning linpack into {tmp}...", file=sys.stderr)
        r = _run_cmd(["git", "clone", "--depth", "1", LINPACK_REPO, tmp])
        if r.returncode != 0:
            raise RuntimeError(f"git clone failed: {r.stderr}")
    print(f"Building Docker image {image_name!r}...", file=sys.stderr)
    r = _run_cmd(["docker", "build", "-t", image_name, tmp])
    if r.returncode != 0:
        raise RuntimeError(f"docker build linpack failed: {r.stderr}")


def wait_postgres_ready(
    container: str,
    timeout: float = 120.0,
    *,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    exec_env: Optional[dict[str, str]] = None,
) -> None:
    deadline = time.time() + timeout
    inner = [
        "pg_isready",
        "-U",
        "postgres",
        "-h",
        pg_host.strip(),
        "-p",
        str(pg_port),
    ]
    while time.time() < deadline:
        p = _run_cmd(
            _docker_exec_cmd(container, inner, exec_env=exec_env),
        )
        if p.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError(
        f"PostgreSQL at {pg_host!r}:{pg_port} (via container {container!r}) did not "
        "become ready."
    )


def ensure_benchmark_database(
    container: str,
    *,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    exec_env: Optional[dict[str, str]] = None,
) -> None:
    """Create database 'benchmark' if missing (required for pgbench per TASK)."""
    p = _run_cmd(
        _docker_exec_cmd(
            container,
            [
                "psql",
                "-U",
                "postgres",
                "-h",
                pg_host.strip(),
                "-p",
                str(pg_port),
                "-tAc",
                "SELECT 1 FROM pg_database WHERE datname='benchmark'",
            ],
            exec_env=exec_env,
        )
    )
    if p.returncode == 0 and p.stdout.strip() == "1":
        return
    r = _run_cmd(
        _docker_exec_cmd(
            container,
            [
                "psql",
                "-U",
                "postgres",
                "-h",
                pg_host.strip(),
                "-p",
                str(pg_port),
                "-c",
                "CREATE DATABASE benchmark;",
            ],
            exec_env=exec_env,
        )
    )
    if r.returncode != 0 and "already exists" not in (r.stderr or "").lower():
        raise RuntimeError(f"CREATE DATABASE benchmark failed: {r.stderr}")


def init_pgbench(
    container: str,
    scale: int = DEFAULT_PGBENCH_SCALE,
    *,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    exec_env: Optional[dict[str, str]] = None,
) -> None:
    cmd = _docker_exec_cmd(
        container,
        [
            "pgbench",
            "-i",
            "-s",
            str(scale),
            "-U",
            "postgres",
            "-h",
            pg_host.strip(),
            "-p",
            str(pg_port),
            "benchmark",
        ],
        exec_env=exec_env,
    )
    p = _run_cmd(cmd, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"pgbench -i failed: {p.stderr}")


def parse_pgbench_tps(text: str) -> Optional[float]:
    m = re.search(r"tps\s*=\s*([\d.]+)", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def _linpack_bench_text_only(text: str) -> str:
    """Strip perf stat footer so counter lines cannot confuse LINPACK parsing."""
    head, sep, _ = text.partition("Performance counter stats")
    if sep:
        return head
    return text


def parse_linpack_mflops(text: str) -> Optional[float]:
    """
    Parse ereyes01/linpack table (KFLOPS column). Returns MFLOPS (KFLOPS / 1000).
    """
    bench = _linpack_bench_text_only(text)
    patterns = [
        r"([\d.eE+-]+)\s*MFLOPS",
        r"MFLOPS\s*[=:]\s*([\d.eE+-]+)",
        r"mflops\s*[=:]\s*([\d.eE+-]+)",
        r"Speed\s*:?\s*([\d.eE+-]+)",
    ]
    for pat in patterns:
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
    # ereyes01/linpack: int main() historically ended without `return 0`; the
    # process exit code can be non-zero even when the benchmark completed.
    return True


LINPACK_NONZERO_EXIT_NOTE = (
    "Exit status is non-zero but the LINPACK table looks complete. "
    "Upstream ereyes01/linpack typically ends main() without 'return 0', "
    "so Docker often reports exit code 10 even after a successful run."
)


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
    return val


def _parse_perf_stat(stderr: str) -> PerfMetrics:
    metrics = PerfMetrics(raw_stderr=stderr)
    m = re.search(
        r"([0-9][0-9\u202f\s,.]*)\s+(msec|seconds|usec)\s+time elapsed",
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


def run_with_perf(
    base_cmd: list[str],
    *,
    use_perf: bool = True,
    timeout: Optional[float] = None,
) -> tuple[int, str, str, Optional[PerfMetrics]]:
    if use_perf:
        perf_check = _run_cmd(["perf", "stat", "true"])
        if perf_check.returncode != 0:
            use_perf = False
    if use_perf:
        cmd = [
            "perf",
            "stat",
            "-e",
            "duration_time,page-faults,context-switches",
            "-B",
            "--",
        ] + base_cmd
    else:
        cmd = list(base_cmd)
    p = _run_cmd(cmd, timeout=timeout)
    perf_metrics: Optional[PerfMetrics] = None
    if use_perf:
        perf_metrics = _parse_perf_stat(p.stderr)
    return p.returncode, p.stdout, p.stderr, perf_metrics


class StressController:
    """Runs stress-ng in Docker to create host CPU load."""

    def __init__(self, image: str = STRESS_IMAGE) -> None:
        self.image = image
        self._name: Optional[str] = None

    def start(self, load_percent: int) -> None:
        self.stop()
        if load_percent <= 0:
            return
        ensure_docker_image(self.image)
        self._name = f"stress-load-{uuid.uuid4().hex[:8]}"
        cmd = [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            self._name,
            self.image,
            "stress-ng",
            "--cpu",
            "0",
            "--cpu-load",
            str(load_percent),
            "--timeout",
            "0",
        ]
        p = _run_cmd(cmd)
        if p.returncode != 0:
            raise RuntimeError(f"Failed to start stress-ng container: {p.stderr}")

    def stop(self) -> None:
        if not self._name:
            return
        _run_cmd(["docker", "stop", self._name], timeout=30)
        self._name = None


def run_pgbench_iter(
    container: str,
    duration: int,
    *,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    clients: int = DEFAULT_PGBENCH_CLIENTS,
    jobs: int = DEFAULT_PGBENCH_JOBS,
    use_perf: bool = True,
    exec_env: Optional[dict[str, str]] = None,
) -> tuple[Optional[float], Optional[PerfMetrics], str, str, Optional[str], int]:
    base = _docker_exec_cmd(
        container,
        [
            "pgbench",
            "-h",
            pg_host.strip(),
            "-p",
            str(pg_port),
            "-U",
            "postgres",
            "-c",
            str(clients),
            "-j",
            str(jobs),
            "-T",
            str(duration),
            "benchmark",
        ],
        exec_env=exec_env,
    )
    code, out, err, perf_m = run_with_perf(
        base, use_perf=use_perf, timeout=float(duration) + 120
    )
    tps = parse_pgbench_tps(out + err)
    err_msg = None if code == 0 else f"pgbench exit {code}"
    return tps, perf_m, out, err, err_msg, code


def build_linpack_run_env(
    *,
    array_size: Optional[int] = None,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """
    Environment variables for `docker run` of ereyes01/linpack.

    - LINPACK_ARRAY_SIZE: problem size (even integer >= 10). Smaller usually
      shortens wall-clock time; default in the image is 200 when unset.
    Host export LINPACK_ARRAY_SIZE is honored if `array_size` is None and
    `extra` does not already set it.
    """
    env: dict[str, str] = {}
    if extra:
        env.update(extra)
    if array_size is not None:
        env[LINPACK_ARRAY_SIZE_ENV] = str(array_size)
    elif (
        LINPACK_ARRAY_SIZE_ENV not in env
        and os.environ.get(LINPACK_ARRAY_SIZE_ENV)
    ):
        env[LINPACK_ARRAY_SIZE_ENV] = os.environ[LINPACK_ARRAY_SIZE_ENV]
    return env


def run_linpack_iter(
    image: str,
    *,
    use_perf: bool = True,
    timeout: float = 600.0,
    linpack_env: Optional[dict[str, str]] = None,
) -> tuple[
    Optional[float],
    Optional[PerfMetrics],
    str,
    str,
    Optional[str],
    int,
    Optional[str],
]:
    cmd: list[str] = ["docker", "run", "--rm"]
    if linpack_env:
        for key in sorted(linpack_env.keys()):
            cmd.extend(["-e", f"{key}={linpack_env[key]}"])
    cmd.append(image)
    code, out, err, perf_m = run_with_perf(cmd, use_perf=use_perf, timeout=timeout)
    text = out + err
    mflops = parse_linpack_mflops(text)
    ok = _linpack_run_ok(code, text, mflops)
    err_msg = None if ok else f"linpack exit {code}"
    exit_note = None
    if ok and code != 0:
        exit_note = LINPACK_NONZERO_EXIT_NOTE
    return mflops, perf_m, out, err, err_msg, code, exit_note


def run_sequential_suite(
    *,
    environment: str,
    pg_container: str,
    linpack_image: str,
    duration: int,
    load_levels: Iterable[int] = DEFAULT_LOAD_LEVELS,
    use_perf: bool = True,
    stress_image: str = STRESS_IMAGE,
    linpack_array_size: Optional[int] = None,
    linpack_extra_env: Optional[dict[str, str]] = None,
    network_iface: Optional[str] = None,
    pg_host: str = "localhost",
    pg_port: int = 5432,
    docker_pg_exec_env: Optional[dict[str, str]] = None,
) -> BenchReport:
    levels = list(load_levels)
    linpack_env = build_linpack_run_env(
        array_size=linpack_array_size,
        extra=linpack_extra_env,
    )
    report = BenchReport(
        environment=environment,
        duration_sec=duration,
        load_levels=levels,
        linpack_docker_env=dict(linpack_env),
    )
    stress = StressController(image=stress_image)
    if network_iface and not _net_iface_exists(network_iface):
        print(
            f"Warning: --network-iface {network_iface!r} not found; skipping NIC stats.",
            file=sys.stderr,
        )
        network_iface = None

    try:
        for load in levels:
            print(f"--- Load {load}%: pgbench ---", file=sys.stderr)
            stress.start(load)
            try:
                net_p: Optional[dict[str, Any]] = None
                if network_iface:
                    rx0, tx0 = read_net_rx_tx_bytes(network_iface)
                    t0 = time.time()
                tps, perf_m, out, err, err_p, pg_code = run_pgbench_iter(
                    pg_container,
                    duration,
                    use_perf=use_perf,
                    pg_host=pg_host,
                    pg_port=pg_port,
                    exec_env=docker_pg_exec_env,
                )
                if network_iface:
                    t1 = time.time()
                    rx1, tx1 = read_net_rx_tx_bytes(network_iface)
                    net_p = net_throughput_dict(
                        network_iface, t0, t1, rx0, tx0, rx1, tx1
                    )
                report.rows.append(
                    asdict(
                        BenchRow(
                            phase="pgbench",
                            load_percent=load,
                            tps=tps,
                            perf=perf_m.to_dict() if perf_m else None,
                            stdout_tail=(out + err)[-2000:],
                            error=err_p,
                            exit_code=pg_code,
                            exit_note=None,
                            network=net_p,
                        )
                    )
                )
            finally:
                stress.stop()

            print(f"--- Load {load}%: linpack ---", file=sys.stderr)
            stress.start(load)
            try:
                net_l: Optional[dict[str, Any]] = None
                if network_iface:
                    rx0, tx0 = read_net_rx_tx_bytes(network_iface)
                    t0 = time.time()
                mflops, perf_m, out, err, err_l, lp_code, lp_note = run_linpack_iter(
                    linpack_image,
                    use_perf=use_perf,
                    linpack_env=linpack_env if linpack_env else None,
                )
                if network_iface:
                    t1 = time.time()
                    rx1, tx1 = read_net_rx_tx_bytes(network_iface)
                    net_l = net_throughput_dict(
                        network_iface, t0, t1, rx0, tx0, rx1, tx1
                    )
                if lp_note:
                    print(
                        f"linpack: reported exit_code={lp_code} (see JSON exit_note); "
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
                            stdout_tail=(out + err)[-2000:],
                            error=err_l,
                            exit_code=lp_code,
                            exit_note=lp_note,
                            network=net_l,
                        )
                    )
                )
            finally:
                stress.stop()
    finally:
        stress.stop()

    return report


def ensure_postgres_container(
    name: str,
    *,
    password: str = "1",
) -> None:
    p = _run_cmd(["docker", "inspect", "-f", "{{.State.Running}}", name])
    if p.returncode == 0:
        if p.stdout.strip() == "True":
            return
        print(f"Starting existing postgres container {name!r}...", file=sys.stderr)
        r = _run_cmd(["docker", "start", name])
        if r.returncode != 0:
            raise RuntimeError(f"docker start postgres failed: {r.stderr}")
        return

    ensure_docker_image("postgres:latest")
    print(f"Creating postgres container {name!r}...", file=sys.stderr)
    r = _run_cmd(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "postgres:latest",
        ]
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker run postgres failed: {r.stderr}")


def report_to_json(report: BenchReport) -> str:
    d = {
        "environment": report.environment,
        "duration_sec": report.duration_sec,
        "load_levels": report.load_levels,
        "rows": report.rows,
        "linpack_docker_env": report.linpack_docker_env,
    }
    return json.dumps(d, indent=2)
