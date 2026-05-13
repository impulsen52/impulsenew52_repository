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
# Если «нет процессов postgres»: БД в Docker — pgrep на хосте пустой (perf в контейнере / другой сценарий);
#   смонтирован /proc с hidepid — нужен root или sudo для чужих PID; редко имя процесса postmaster.
#   Обход: явно передать PID главного постмастера: --pids "$(pgrep -xo postgres)" или со списка ps.
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
PIDS_OVERRIDE=""

usage() {
	cat <<EOF
Запуск на хосте с PostgreSQL (см. комментарии в начале файла).

  $SCRIPT_PATH [опции]

Опции:
  --pg-host HOST       local|unix — без -h; иначе TCP-хост для pgbench (по умолчанию: local)
  --pg-port N          (по умолчанию: 5432)
  --pg-user USER
  --pg-database NAME
  --pgbench-bin PATH
  --clients N / -c N
  --jobs N    / -j N
  --transactions N / -t N
  --duration SEC / -T SEC   взаимоисключающе с -t
  --perf-events LIST
  --perf-sleep SEC       аргумент sleep под perf (по умолчанию: 86400)
  --pids CSV             явные PID сервера, например 1244,1304 (если авто-поиск пустой)
  --help

Пример с явными PID:
  $SCRIPT_PATH --pg-host local --pids "\$(pgrep -xo postgres | paste -sd,)" -c 4 -t 100
EOF
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
	--pids)
		PIDS_OVERRIDE="$2"
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

declare -A _pid_seen=()
PG_PIDS=()
note_pid() {
	local p="$1"
	[[ "$p" =~ ^[0-9]+$ ]] || return 0
	[[ -n "${_pid_seen[$p]:-}" ]] && return 0
	_pid_seen[$p]=1
	PG_PIDS+=("$p")
}

slurp_pgrep() {
	local line
	while IFS= read -r line; do
		[[ -n "$line" ]] && note_pid "$line"
	done
}

discover_postgres_pids() {
	PG_PIDS=()
	_pid_seen=()
	if [[ -n "$PIDS_OVERRIDE" ]]; then
		local IFS=,
		local _p
		for _p in $PIDS_OVERRIDE; do
			note_pid "${_p//[[:space:]]/}"
		done
		return 0
	fi
	slurp_pgrep < <(pgrep -x postgres 2>/dev/null || true)
	slurp_pgrep < <(pgrep -x postmaster 2>/dev/null || true)
	# PGDG/RPM: argv ".../postmaster -D ..."; Debian: ".../postgres -D ...".
	# На части установок comm не совпадает с именем "postgres" для pgrep -x.
	slurp_pgrep < <(pgrep -f 'postmaster -D' 2>/dev/null || true)
	slurp_pgrep < <(pgrep -f '/postgres -D' 2>/dev/null || true)
	slurp_pgrep < <(pgrep -f 'postgres: ' 2>/dev/null || true)
	local po
	po=$(pidof postgres 2>/dev/null || true)
	[[ -n "$po" ]] && for _p in $po; do note_pid "$_p"; done
	po=$(pidof postmaster 2>/dev/null || true)
	[[ -n "$po" ]] && for _p in $po; do note_pid "$_p"; done
	if [[ "$(id -u)" -ne 0 ]]; then
		slurp_pgrep < <(sudo -n pgrep -x postgres 2>/dev/null || true)
		slurp_pgrep < <(sudo -n pgrep -x postmaster 2>/dev/null || true)
		slurp_pgrep < <(sudo -n pgrep -f 'postmaster -D' 2>/dev/null || true)
		slurp_pgrep < <(sudo -n pgrep -f '/postgres -D' 2>/dev/null || true)
		slurp_pgrep < <(sudo -n pgrep -f 'postgres: ' 2>/dev/null || true)
		po=$(sudo -n pidof postgres 2>/dev/null || true)
		[[ -n "$po" ]] && for _p in $po; do note_pid "$_p"; done
		po=$(sudo -n pidof postmaster 2>/dev/null || true)
		[[ -n "$po" ]] && for _p in $po; do note_pid "$_p"; done
	fi
}

discover_postgres_pids
IFS=$'\n'
PG_PIDS=($(printf '%s\n' "${PG_PIDS[@]}" | sort -nu))
unset IFS

if [[ ${#PG_PIDS[@]} -eq 0 ]]; then
	cat >&2 <<'EOF'
Не найдены PID сервера PostgreSQL (пусто после pgrep -x/-f и pidof).

Частые причины:
  • PostgreSQL в контейнере — на хосте нет процесса postgres; perf нужно внутри контейнера
    или укажите PID на хосте вручную, если так задумано.
  • /proc с hidepid — пользователь не видит чужие процессы; попробуйте root или sudo -n.
  • PGDG/RPM (/usr/pgsql-*): главный процесс в ps — «postmaster -D …»; скрипт ищет его через
    pgrep -f, а не только pgrep -x postgres.

Обход: передайте PID вручную, например:
  ./remote_pgbench_perf_bundle.sh --pg-host local --pids "1244,1306" -c 4 -t 100
Подбор PID (на сервере): ps -eo pid,user,comm:20,args | grep -E '[p]ostgres:'
EOF
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
if [[ -n "$PIDS_OVERRIDE" ]]; then
	echo "pid_source=override"
else
	echo "pid_source=auto"
fi
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
