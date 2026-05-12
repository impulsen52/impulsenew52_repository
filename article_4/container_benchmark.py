#!/usr/bin/env python3
"""
Docker host benchmark driver: PostgreSQL (pgbench) then linpack at CPU load 0-100%,
with stress-ng on the host and perf stat metrics when available.

Optional --network-iface records RX/TX Mbit/s per phase from Linux /sys counters.

Use --pg-host to point pgbench (inside the client container) at another machine on the
LAN (e.g. a DB VM IP from deploy_vm.py --print-ip). Set PGPASSWORD on the host when the
remote role requires a password (passed into the container via docker exec -e).

Prerequisites: Docker, git (for linpack image build), optional perf (host).
"""
from __future__ import annotations

import argparse
import os
import sys

from benchmark_core import (
    DEFAULT_LINPACK_IMAGE,
    DEFAULT_LOAD_LEVELS,
    STRESS_IMAGE,
    docker_available,
    ensure_benchmark_database,
    ensure_linpack_image,
    ensure_postgres_container,
    init_pgbench,
    report_to_json,
    run_sequential_suite,
    wait_postgres_ready,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=int,
        default=30,
        help="pgbench -T duration in seconds (default 30)",
    )
    parser.add_argument(
        "--pg-container",
        default="postgres-bench",
        help="Docker container name for postgres:latest (pgbench/psql run via docker exec)",
    )
    parser.add_argument(
        "--pg-host",
        default="localhost",
        metavar="HOST",
        help="PostgreSQL host for pgbench/psql/pg_isready (default: localhost in container)",
    )
    parser.add_argument(
        "--pg-port",
        type=int,
        default=5432,
        metavar="N",
        help="PostgreSQL TCP port on --pg-host (default 5432)",
    )
    parser.add_argument(
        "--linpack-image",
        default=DEFAULT_LINPACK_IMAGE,
        help="Local Docker image name for ereyes01/linpack",
    )
    parser.add_argument(
        "--linpack-src",
        default="",
        help="Optional directory with cloned linpack repo (skip clone if it has .git)",
    )
    parser.add_argument(
        "--skip-linpack-build",
        action="store_true",
        help="Fail if linpack image is missing instead of building",
    )
    parser.add_argument(
        "--stress-image",
        default=STRESS_IMAGE,
        help="Docker image for stress-ng (default alexeiled/stress-ng)",
    )
    parser.add_argument(
        "--no-perf",
        action="store_true",
        help="Disable perf stat wrapper",
    )
    parser.add_argument(
        "--loads",
        default="",
        help="Comma load percents (default 0,20,...,100), e.g. 0,50,100",
    )
    parser.add_argument(
        "--linpack-array-size",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Problem size for ereyes01/linpack (env LINPACK_ARRAY_SIZE). "
            "Smaller N shortens wall time; default in image is 200. Must be >= 10."
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        default="",
        help="Write JSON report to this file",
    )
    parser.add_argument(
        "--skip-pgbench-init",
        action="store_true",
        help="Do not run pgbench -i (DB must already be initialized)",
    )
    parser.add_argument(
        "--network-iface",
        default="",
        metavar="IFACE",
        help=(
            "If set (e.g. eth0), record RX/TX throughput from /sys/class/net during "
            "each pgbench/linpack phase (whole-machine NIC counters)."
        ),
    )
    args = parser.parse_args()

    if args.linpack_array_size is not None and args.linpack_array_size < 10:
        print("--linpack-array-size must be >= 10", file=sys.stderr)
        sys.exit(2)

    if not docker_available():
        print("Docker is not available or not running.", file=sys.stderr)
        sys.exit(1)

    load_levels = (
        [int(x) for x in args.loads.split(",") if x.strip()]
        if args.loads
        else list(DEFAULT_LOAD_LEVELS)
    )

    docker_pg_exec_env: dict[str, str] | None = None
    if os.environ.get("PGPASSWORD"):
        docker_pg_exec_env = {"PGPASSWORD": os.environ["PGPASSWORD"]}

    ensure_postgres_container(args.pg_container)
    wait_postgres_ready(
        args.pg_container,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        exec_env=docker_pg_exec_env,
    )
    ensure_benchmark_database(
        args.pg_container,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        exec_env=docker_pg_exec_env,
    )
    if not args.skip_pgbench_init:
        init_pgbench(
            args.pg_container,
            pg_host=args.pg_host,
            pg_port=args.pg_port,
            exec_env=docker_pg_exec_env,
        )

    ensure_linpack_image(
        args.linpack_image,
        clone_dir=args.linpack_src or None,
        skip_build=args.skip_linpack_build,
    )

    report = run_sequential_suite(
        environment="docker_on_host",
        pg_container=args.pg_container,
        linpack_image=args.linpack_image,
        duration=args.duration,
        load_levels=load_levels,
        use_perf=not args.no_perf,
        stress_image=args.stress_image,
        linpack_array_size=args.linpack_array_size,
        network_iface=args.network_iface.strip() or None,
        pg_host=args.pg_host,
        pg_port=args.pg_port,
        docker_pg_exec_env=docker_pg_exec_env,
    )
    text = report_to_json(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
