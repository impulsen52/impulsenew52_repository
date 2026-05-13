# Статья №4

Материалы для четвёртой научной статьи: бенчмарк виртуальных машин и контейнеров (QEMU/KVM, Docker).

## Содержимое

- **`TASK.md`** — постановка задачи и описание метрик.
- **`TASK_APPLE_ARM.md`** — заметки по сценарию на Apple ARM.
- **Скрипты:** `deploy_vm.py`, `container_benchmark.py`, `vm_benchmark.py`, `benchmark_core.py`, `format_benchmark_output.py`.
- **Примеры JSON:** `bench.json`, `bench_distant.json`, `output.json` и др. (при необходимости обновляйте или добавляйте файлы с понятными именами).

---

## Общее

Команды ниже предполагают каталог `article_4/` (из корня клона: `cd article_4`). Контейнерный драйвер импортирует `benchmark_core.py` из того же каталога.

```bash
cd article_4
python3 <скрипт>.py --help
```

**Python:** 3.10+ (используются аннотации современного стиля).

---

## 1. `deploy_vm.py` — ВМ под QEMU/KVM (libvirt / virt-install)

**Назначение:** создать гостевую ОС с установочного ISO, virtio-диск и сеть; опционально вывести IPv4 гостя для доступа к PostgreSQL с хоста.

**На машине-домене:** `virt-install`, `qemu-img`, при `--print-ip` — `virsh`. Для `qemu:///session` скрипт может переключить сеть на `user` (slirp), если не задана своя `--network`. В госте для надёжного `--print-ip` желателен **qemu-guest-agent**.

**Создание ВМ** (установка ОС из ISO — интерактивно, как в `TASK.md`):

```bash
python3 deploy_vm.py --name bench-guest \
  --iso /путь/к/установочному.iso \
  --disk "$HOME/libvirt-images/bench-guest.qcow2" \
  --connect qemu:///session
```

**Сухой прогон** (только печать команды `virt-install`):

```bash
python3 deploy_vm.py --name bench-guest --iso /путь/к.iso \
  --disk "$HOME/libvirt-images/bench-guest.qcow2" --dry-run
```

**IP гостя** (ВМ уже установлена и запущена; `--iso` не нужен):

```bash
python3 deploy_vm.py --print-ip --name bench-guest --connect qemu:///session
```

Используйте тот же `--connect`, что при создании. Полученный IP передаётся в **`container_benchmark.py`** как **`--pg-host`**, если PostgreSQL в госте, а Docker с бенчмарком — на хосте.

**Сеть (кратко):** `default` (NAT libvirt, обычно `qemu:///system`), `user` (slirp для сессии), `bridge:br0`, `sriov:имя_pf`. Подробности — `python3 deploy_vm.py --help`.

---

## 2. `container_benchmark.py` — бенчмарк на хосте с Docker

**Назначение:** в контейнере поднимается PostgreSQL (`postgres-bench` по умолчанию); `pgbench` и `psql` выполняются через **`docker exec`** в этот же контейнер (или к БД на `--pg-host`, например IP ВМ). На **хосте** в Docker крутится `stress-ng` на ступенях нагрузки CPU **0, 20, …, 100%** (или свой список `--loads`). Затем для каждого уровня: **pgbench ? linpack** в отдельном контейнере-образе. Опционально **`perf stat`**; опционально счётчики **RX/TX** с выбранного интерфейса (`/sys/class/net/...`). Отчёт — JSON в stdout и/или в `-o`.

**Зависимости:** Docker; `git` (первая сборка образа linpack из `ereyes01/linpack`); опционально **`perf`** на хосте. Для режима **`--pgbench-perf-target server`** (по умолчанию) выборка **PID процессов postgres в контейнере** и `perf -p` часто требуют **root** или **`sudo -n`** на хосте (см. ниже).

**Типичный прогон** (БД в контейнере на `localhost`, учёт трафика на `eth0`):

```bash
python3 container_benchmark.py --network-iface eth0 -o bench_docker.json
```

**PostgreSQL во ВМ, Docker на хосте:**

```bash
export PGPASSWORD='пароль_роли'   # если нужен для подключения из контейнера
python3 container_benchmark.py \
  --pg-host 192.168.122.45 \
  --network-iface eth0 \
  -o bench_remote_db.json
```

### Режимы pgbench по времени и по числу транзакций

- **`--duration`** — лимит времени pgbench (**`-T`**), по умолчанию **30** с, если не задан **`--pgbench-transactions`**.
- **`--pgbench-transactions N`** — режим **`-t N`** (число транзакций **на клиента**). С **`pgbench`** не передаётся **`-T`**. В JSON **`duration_sec`** на верхнем уровне будет **`null`** (нет лимита по времени в pgbench); есть поле **`pgbench_transactions_per_client`**. Если указаны оба флага, **`--duration` для pgbench не используется** (в stderr — короткое напоминание).

### `--pgbench-perf-target server | client`

- **`server`** (по умолчанию): **`perf`** не оборачивает **`pgbench`**. Собираются **хостовые PID** процессов, в командной строке которых есть **`postgres`**, из **`docker top <контейнер>`**; параллельно запускается **`perf stat -p …`** (см. `benchmark_core.py`). Нужен **loopback** **`--pg-host`** с точки зрения контейнера (`localhost`, `127.0.0.1`, `::1`). Иначе — предупреждение и замер **`pgbench`** как у режима **`client`**.
- **`client`**: классически **`perf stat … docker exec … pgbench`** — метрики ближе к процессу-клиенту.

Для **не-root** на хосте монитор **`perf -p`** к процессам БД часто выполняется как **`sudo -n perf …`**; без passwordless sudo метрики **perf** для фазы pgbench могут быть пустыми — см. поле **`perf_raw_tail`** в JSON при ошибке парсинга.

### Полезные параметры (`container_benchmark.py`)

| Параметр | Смысл |
|----------|--------|
| `-o` / `--output` | Файл JSON-отчёта |
| `--duration` | Секунды **`pgbench -T`** (по умолчанию 30, если нет `--pgbench-transactions`) |
| `--pgbench-transactions` | **`pgbench -t`** на клиента |
| `--pgbench-perf-target` | `server` или `client` (см. выше) |
| `--pg-container` | Имя контейнера Postgres (по умолчанию `postgres-bench`) |
| `--postgres-image` / образ из окружения | Образ Postgres на нестандартных архитектурах |
| `--pg-host` / `--pg-port` | Куда подключаться из контейнера |
| `--network-iface` | Интерфейс для полей **`network`** в строках JSON |
| `--loads` | Уровни нагрузки, напр. `0,50,100` |
| `--no-perf` | Отключить **perf** |
| `--skip-pgbench-init` | Не выполнять **`pgbench -i`** |
| `--linpack-array-size` | Env **`LINPACK_ARRAY_SIZE`** для образа linpack (? 10) |
| `--linpack-src` / `--skip-linpack-build` | Клон / отказ от сборки linpack |

### Формат JSON (сокращённо)

- **`environment`:** `docker_on_host`
- **`duration_sec`:** лимит **`-T`** в отчёте или **`null`** в режиме **`-t`**
- **`pgbench_transactions_per_client`:** при **`--pgbench-transactions`**
- **`rows[]`:** фазы **`pgbench`** и **`linpack`**, поля **`tps`**, **`mflops`**, **`perf`** (в т.ч. **`counter_target`**, **`monitored_pid_count`**, при сбое разбора — **`perf_raw_tail`**), **`network`**, **`exit_code`**, **`exit_note`** (для linpack с ненулевым кодом выхода при «успешном» прогоне)

---

## 3. `vm_benchmark.py` — бенчмарк внутри гостевой ОС (без Docker)

**Назначение:** один файл, **без** `benchmark_core` и без контейнеров. Для каждого уровня нагрузки: опционально **`stress-ng`** на госте ? **pgbench** ? локальный **linpack**-бинарник. Те же идеи по **`perf`** и **`--network-iface`**, что в контейнерном сценарии, но PID сервера собираются через **`pgrep -x postgres`** / **`pidof`** на **этой** ВМ.

**Зависимости в госте:** PostgreSQL и клиент (`pgbench`, `pg_isready`); **`stress-ng`** (или **`--no-stress`**); **`perf`** (или **`--no-perf`**); путь к собранному **linpack**.

**Минимальный пример:**

```bash
python3 vm_benchmark.py \
  --linpack-binary /usr/local/bin/linpack \
  --duration 30 \
  -o vm_guest.json
```

Часто для сокета и peer-аутентификации:

```bash
python3 vm_benchmark.py --pg-host local --pg-user postgres --pg-database mydb \
  --linpack-binary /path/to/linpack -o vm_guest.json
```

Пароль для TCP:

```bash
export PGPASSWORD='секрет'
python3 vm_benchmark.py --pg-host 127.0.0.1 --pgbench-no-sudo \
  --pg-user postgres --pg-database mydb \
  --linpack-binary /path/to/linpack -o vm_guest.json
```

### Параметры, которые часто меняют

| Параметр | Смысл |
|----------|--------|
| `--linpack-binary` | **Обязателен** — путь к исполняемому linpack |
| `--duration` / `--pgbench-transactions` | Режим **`-T`** или **`-t`** (логика как у контейнерного драйвера) |
| `--pgbench-perf-target` | `server` (PID **postgres** на госте + фоновый **perf**) или `client` (**perf** вокруг **pgbench**) |
| `--pg-host` | `local` / `unix` — без `-h`; `localhost` / `127.0.0.1` — TCP; для **server**-**perf** при удалённом `--pg-host` — откат к **client** |
| `--pgbench-os-user` / `--pgbench-no-sudo` | Запуск **pgbench** от имени пользователя ОС (по умолчанию **sudo -u postgres**) |
| `--scale`, `--clients`, `--jobs` | Параметры **`pgbench -i`** / прогона |
| `--linpack-array-size` | Env для linpack |
| `--network-iface` | Явный интерфейс; для loopback без флага может подставиться **`lo`** |
| `--no-stress`, `--no-perf`, `--skip-pgbench-init`, `--loads`, `-o`, `--verbose` | По смыслу |

**JSON:** **`environment`: `native_vm`**, плюс **`linpack_process_env`**. Остальная структура строк совместима по смыслу с контейнерным отчётом.

---

## 4. `format_benchmark_output.py` — таблицы и CSV из JSON

Читает файл (по умолчанию **`output.json`** в текущем каталоге), печатает метаданные (**в т.ч. режим **`-T`** vs **`-t`**), ASCII-таблицы по фазам **pgbench** и **linpack**, опционально CSV.

```bash
python3 format_benchmark_output.py bench_docker.json
python3 format_benchmark_output.py bench_docker.json -o tables.txt
python3 format_benchmark_output.py bench_docker.json --csv -o bench.csv
```

Пустые колонки RX/TX обычно означают: запуск без **`--network-iface`**, устаревший JSON **без** `network` в строках или открыт **не тот** файл (не тот, что передали в **`-o`** у бенчмарка).

---

## 5. `benchmark_core.py`

Общая логика для **`container_benchmark.py`** (последовательность нагрузок, Docker, **pgbench**/**linpack**, **perf**, сеть). **Отдельно не запускается** — только импорт.

---

## Сводка сценариев

| Где | Скрипт | Роль |
|-----|--------|------|
| Гипервизор | `deploy_vm.py` | Создание ВМ, при необходимости — IP гостя |
| Хост с Docker | `container_benchmark.py` | Postgres в контейнере (или БД на `--pg-host`), linpack в образе, stress в контейнере |
| Внутри гостя | `vm_benchmark.py` | Нативный Postgres + linpack + stress-ng |
| Любой | `format_benchmark_output.py` | Человекочитаемый вывод из JSON |
