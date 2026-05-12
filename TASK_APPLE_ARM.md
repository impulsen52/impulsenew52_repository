# TASK: Separate Apple ARM Benchmark Program

## Role

You are **Developer Agent (Apple ARM Track)**.

## Goal

Create a **separate Python program** for **Apple Silicon (ARM64, M1/M2/M3)** that runs the same benchmark idea (pgbench + LINPACK + optional background load) without relying on Linux KVM/libvirt tooling.

This program must be independent from `deploy_vm.py` and Linux VM provisioning flow.

## Why Separate

- Existing VM deployment path is Linux/KVM-specific (`virt-install`, `qemu-img`, `/var/lib/libvirt/...`).
- Apple ARM hosts need a dedicated workflow and compatibility checks.

## Deliverables

Implement a new script:

- `apple_arm_benchmark.py`

Optional helper module (if needed):

- `apple_arm_core.py`

Update docs:

- Add usage examples to `README.md` (create it if absent) or append to this task file.

## Functional Requirements

1. **No system package manager usage**

- Do not call `apt`, `apt-get`, `yum`, `dnf`, etc.
- Depend only on:
  - Python stdlib
  - Docker CLI available on host
  - existing local scripts/modules

1. **pgbench must come from Docker**

- Use `postgres:latest` container and run pgbench via:
  - `docker exec <container> pgbench ...`
- Do not require host-installed PostgreSQL tools.

1. **LINPACK compiled in Docker**

- If LINPACK image missing:
  - clone source repo
  - build image with `docker build`
- Do not compile LINPACK on host directly.

1. **Apple ARM compatibility**

- Detect host with `uname -s` and `uname -m`.
- Support only:
  - OS: `Darwin`
  - arch: `arm64`
- On unsupported host, exit with clear actionable error.

1. **Docker runtime checks**

- Validate Docker is installed and daemon is running.
- Optionally validate image architecture manifests when possible.
- Provide clear errors for unsupported/missing multi-arch images.

1. **Benchmark flow**

- Keep sequential pattern:
  - background load level N
  - pgbench run
  - LINPACK run
  - repeat for load levels 0..100 step 20 (customizable)
- Output JSON report compatible with existing format as much as practical.

1. **Background load on Apple ARM**

- Prefer containerized load generator (e.g. `stress-ng` image) if compatible.
- If image/flags are unsupported on macOS Docker runtime, implement fallback:
  - run benchmark with warning and no background load,
  - or configurable alternative load strategy.
- Never fail silently.

## CLI Requirements

The new script should support:

- `--duration`
- `--loads`
- `--pg-container`
- `--linpack-image`
- `--linpack-src`
- `--skip-linpack-build`
- `--linpack-array-size`
- `--stress-image`
- `--no-perf` (default true behavior on macOS is acceptable if perf unavailable)
- `--output`
- `--skip-pgbench-init`

## Non-Goals

- Do not implement VM creation/provisioning on macOS in this task.
- Do not modify Linux KVM scripts (`deploy_vm.py`) unless absolutely required for shared reuse.

## Suggested Implementation Approach

1. Reuse `benchmark_core.py` where safe.
2. Add Apple-specific preflight function:
  - verify Darwin + arm64
  - verify docker availability
  - warn/disable unsupported `perf` paths.
3. Keep core benchmark commands Docker-only.
4. Add robust error messages for architecture/image mismatches.

## Acceptance Criteria

- Running `python3 apple_arm_benchmark.py --help` works on Apple ARM.
- Script exits with clear message on non-Darwin or non-arm64.
- No package-manager commands are present.
- pgbench is executed only through Docker container.
- LINPACK build occurs via Docker build path when image absent.
- JSON output is generated and includes per-phase rows.

## Validation Checklist (Agent must run)

- Static command scan confirms no `apt`/`yum`/etc.
- Dry run or actual run demonstrates:
  - postgres container readiness
  - pgbench initialization and benchmark command via `docker exec`
  - linpack image check/build path
- Share short test report with:
  - host info (`uname -s`, `uname -m`)
  - command used
  - result summary

