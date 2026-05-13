#!/usr/bin/env bash
#
# remote_pgbench_perf_bundle.sh — ручная проверка схемы «один сеанс: perf на PID
# PostgreSQL + pgbench на этой же машине», с разделителями в stdout для разбора на клиенте.
#
# Запускать НА ХОСТЕ С БД, под пользователем, который может:
#   - выполнять pgbench к локальной (или указанной) БД;
#   - запускать perf -p <pid postgres…> (часто через «sudo -n -- perf …»).
#
# Примеры:
#   ./remote_pgbench_perf_bundle.sh --pg-host local --clients 20 --jobs 2 --transactions 2000
#   ./remote_pgbench_perf_bundle.sh --pg-host 127.0.0.1 --pg-database bench --duration 30
#
# Через SSH с машины-клиента:
#   ssh -T postgres@192.168.122.235 'bash -s' < remote_pgbench_perf_bundle.sh -- --pg-host local -c 4 -t 100
#   (удобнее скопировать скрипт на сервер и вызывать там)
#   scp remote_pgbench_perf_bundle.sh postgres@db:/tmp/ && ssh postgres@db 'bash /tmp/remote_pgbench_perf_bundle.sh …'
#

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"

PGBENCH_BIN="${PGBENCH_BIN:-/usr/bin/pgbench}"
PGHOST="${PGHOST:-local}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-postgres}"
PGDATABASE="${PGDATABASE:-postgres}"
CLIENTS="${CLIENTS:-20}"
JOBS="${JOBS:-2}"
TRANSACTIONS="${TRANSACTIONS:-2000}"
DURATION_SEC=""
PERF_EVENTS="${PERF_EVENTS:-duration_time,page-faults,context-switches}"
PERF_SLEEP_SEC="${PERF_SLEEP_SEC:-86400}"

usage() {
	sed -n '1,28p' "$SCRIPT_PATH" | tail -n +2
	exit 0
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	--help)
		usage
		;;
	--pg-host)
		PGHOST="$2"
		shift 2
		;;
	--pg-port)
		PGPORT="$2"
		shift 2
		;;
	--pg-user)
		PGUSER="$2"
		shift 2
		;;
	--pg-database)
		PGDATABASE="$2"
		shift 2
		;;
	--pgbench-bin)
		PGBENCH_BIN="$2"
		shift 2
		;;
	--clients|-c)
		CLIENTS="$2"
		shift 2
		;;
	--jobs|-j)
		JOBS="$2"
		shift 2
		;;
	--transactions|-t)
		TRANSACTIONS="$2"
		DURATION_SEC=""
		shift 2
		;;
	--duration|-T)
		DURATION_SEC="$2"
		shift 2
		;;
	--perf-events)
		PERF_EVENTS="$2"
		shift 2
		;;
	--perf-sleep)
		PERF_SLEEP_SEC="$2"
		shift 2
		;;
	*)
		echo "Неизвестный аргумент: $1" >&2
		echo "См. $SCRIPT_PATH --help" >&2
		exit 2
		;;
	esac
done

if [[ -n "$DURATION_SEC" && "$DURATION_SEC" != "0" ]]; then
	PGBENCH_TIME_ARGS=(-T "$DURATION_SEC")
else
	PGBENCH_TIME_ARGS=(-t "$TRANSACTIONS")
fi

PERF_LOG=$(mktemp)
PGB_STDOUT=$(mktemp)
PGB_STDERR=$(mktemp)
trap 'rm -f "$PERF_LOG" "$PGB_STDOUT" "$PGB_STDERR"' EXIT

mapfile -t PG_PIDS < <(pgrep -x postgres 2>/dev/null || true)
if [[ ${#PG_PIDS[@]} -eq 0 ]]; then
	echo "Нет процессов postgres (pgrep -x postgres). Запущен ли кластер?" >&2
	exit 3
fi
IFS=,
PID_CSV="${PG_PIDS[*]}"
unset IFS

if [[ "$(id -u)" -eq 0 ]]; then
	PERF_CMD=(perf stat -e "$PERF_EVENTS" -B -p "$PID_CSV" -- sleep "$PERF_SLEEP_SEC")
else
	PERF_CMD=(sudo -n -- perf stat -e "$PERF_EVENTS" -B -p "$PID_CSV" -- sleep "$PERF_SLEEP_SEC")
fi

perf_pid=""
cleanup_perf() {
	if [[ -n "$perf_pid" ]] && kill -0 "$perf_pid" 2>/dev/null; then
		kill -INT "$perf_pid" 2>/dev/null || true
		wait "$perf_pid" 2>/dev/null || true
	fi
}
trap 'cleanup_perf' EXIT INT TERM

"${PERF_CMD[@]}" >"$PERF_LOG" 2>&1 &
perf_pid=$!

sleep 0.3
if ! kill -0 "$perf_pid" 2>/dev/null; then
	echo "---ERROR---" >&2
	echo "perf не удержался в фоне (sudo/права/параноид?). Хвост лога:" >&2
	tail -n 50 "$PERF_LOG" >&2 || true
	exit 4
fi

pgbench_cmd=("$PGBENCH_BIN")
h="${PGHOST,,}"
if [[ "$h" == "local" || "$h" == "unix" || -z "${PGHOST// }" ]]; then
	:
else
	pgbench_cmd+=(-h "$PGHOST")
fi
pgbench_cmd+=(-p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -c "$CLIENTS" -j "$JOBS")
pgbench_cmd+=("${PGBENCH_TIME_ARGS[@]}")

set +e
"${pgbench_cmd[@]}" >"$PGB_STDOUT" 2>"$PGB_STDERR"
pgbench_rc=$?
set -e

cleanup_perf
trap - EXIT INT TERM

echo "---META---"
echo "pid_csv=$PID_CSV"
echo "pgbench_rc=$pgbench_rc"
echo "---PGBENCH_CMD---"
printf '%q ' "${pgbench_cmd[@]}"
echo
echo "---PGBENCH_STDOUT---"
cat "$PGB_STDOUT"
echo "---PGBENCH_STDERR---"
cat "$PGB_STDERR"
echo "---PERF_STAT---"
cat "$PERF_LOG"
echo "---END---"

exit "$pgbench_rc"
