# Статья №4

Каталог с материалами для четвёртой научной статьи: комплексный бенчмарк виртуальных машин и контейнеров (Astra Linux, РЕД ОС, QEMU-KVM, Docker).

## Содержимое

- **`TASK.md`** — постановка задачи и описание метрик.
- **`TASK_APPLE_ARM.md`** — заметки по сценарию на Apple ARM.
- **Скрипты:** `deploy_vm.py`, `vm_benchmark.py`, `container_benchmark.py`, `benchmark_core.py`, `format_benchmark_output.py`.
- **Данные:** `bench.json`, `bench_distant.json`, `output.json`, `OUTPUT.json` (при необходимости обновляйте или добавляйте версии с понятными именами).

## Общее

Все команды ниже предполагают, что вы уже в каталоге `article_4/` (из корня клона репозитория: `cd article_4`). Так корректны относительные пути к JSON и импорт `benchmark_core` в контейнерном драйвере.

```bash
cd article_4
```

Справка по аргументам у любого скрипта: `python3 <имя_файла>.py --help`.

---

## 1. `deploy_vm.py` — ВМ под QEMU/KVM (libvirt)

Назначение: развернуть гостевую ОС с ISO (`virt-install`) и при необходимости узнать IPv4 гостя для доступа к PostgreSQL с хоста с Docker.

**На машине:** установлены `virt-install`, `qemu-img`, `virsh`; для `qemu:///session` скрипт сам переключит сеть на `user` (slirp), если не задана своя `--network`.

**Создание ВМ** (после установки ОС из ISO гость должен быть настроен вручную, как в `TASK.md`):

```bash
python3 deploy_vm.py --name bench-guest \
  --iso /путь/к/установочному.iso \
  --disk "$HOME/libvirt-images/bench-guest.qcow2" \
  --connect qemu:///session
```

Путь `--disk` для обычного пользователя лучше задавать в домашнем каталоге: каталог по умолчанию под `/var/lib/libvirt/images/` часто недоступен на запись.

**Проверка сухого прогона** (только команда `virt-install`):

```bash
python3 deploy_vm.py --name bench-guest --iso /путь/к.iso --disk "$HOME/libvirt-images/bench-guest.qcow2" --dry-run
```

**IP гостя** (когда ВМ уже установлена и запущена; `--iso` не нужен):

```bash
python3 deploy_vm.py --print-ip --name bench-guest --connect qemu:///session
```

Тот же URI `--connect`, что и при создании. Надёжнее, если в госте установлен `qemu-guest-agent`; иначе смотрите подсказки скрипта и `virsh domifaddr`.

Полученный IP далее передаётся в **`container_benchmark.py`** как `--pg-host` (см. раздел 2), если PostgreSQL крутится внутри ВМ, а Docker с `pgbench` — на хосте.

---

## 2. `container_benchmark.py` — бенчмарк с Docker на хосте

Назначение: на машине с Docker поднимается контейнер `postgres:latest`, при необходимости собирается образ linpack из репозитория `ereyes01/linpack`, на хосте крутится `stress-ng` для ступеней нагрузки CPU (0–100?%), замеряются pgbench (TPS) и linpack (MFLOPS), опционально оборачивается `perf stat`, в JSON пишутся фазы и метрики.

**Зависимости:** работающий Docker; `git` (для первичной сборки образа linpack); по желанию `perf` на хосте.

**Типичный полный прогон на одной машине** (БД в контейнере на `localhost`, результат в файл, учёт трафика на интерфейсе `eth0`):

```bash
python3 container_benchmark.py \
  --network-iface eth0 \
  -o bench_docker.json
```

Скрипт всё равно дублирует JSON в stdout; основной артефакт для статьи — файл из `-o`.

**Параметры, которые чаще всего меняют:**

| Параметр | Смысл |
|----------|--------|
| `-o` / `--output` | путь к JSON-отчёту |
| `--duration` | длительность одного прогона pgbench, секунды (`-T` pgbench), по умолчанию 30 |
| `--pg-container` | имя контейнера PostgreSQL (по умолчанию `postgres-bench`) |
| `--pg-host` / `--pg-port` | хост и порт PostgreSQL с точки зрения `docker exec` (например IP ВМ, если БД в госте) |
| `--network-iface` | интерфейс Linux для колонок RX/TX в JSON (например `eth0`) |
| `--loads` | свои уровни нагрузки, например `0,50,100` (по умолчанию 0,20,…,100) |
| `--no-perf` | не вызывать `perf stat` |
| `--skip-pgbench-init` | не выполнять `pgbench -i`, если тестовая БД уже инициализирована |
| `--skip-linpack-build` | не собирать linpack, если образ уже есть (иначе при отсутствии образа будет ошибка) |
| `--linpack-src` | каталог с уже клонированным репозиторием linpack (не клонировать заново) |

**PostgreSQL на другой машине (например в ВМ):** на хосте с Docker:

```bash
export PGPASSWORD='ваш_пароль_роли'   # если нужен пароль для подключения из контейнера
python3 container_benchmark.py \
  --pg-host 192.168.122.45 \
  --network-iface eth0 \
  -o bench_remote_db.json
```

IP подставьте из `deploy_vm.py --print-ip` или из вашей сети. `PGPASSWORD` передаётся в окружение `docker exec` для клиента в контейнере.

**Ускорение экспериментов:** уменьшить размер задачи linpack (если поддерживается образом):

```bash
python3 container_benchmark.py --linpack-array-size 80 -o bench_quick.json
```

---

## 3. `vm_benchmark.py` — бенчмарк внутри гостевой ВМ (без Docker-драйвера)

Назначение: те же фазы pgbench ? linpack и ступени нагрузки `stress-ng`, но скрипт запускается **на установленной гостевой ОС**, PostgreSQL и pgbench — нативно, linpack — указанный бинарник на диске гостя.

**Зависимости в госте:** установленные PostgreSQL (в т.ч. `pgbench`), собранный/установленный исполняемый файл linpack из сценария `TASK.md`, в PATH — `stress-ng`; для пользователя `postgres` и peer-auth обычно нужен `sudo`; опционально `perf`.

Минимальный пример (подставьте реальный путь к linpack):

```bash
sudo python3 vm_benchmark.py \
  --linpack-binary /usr/local/bin/linpack \
  --duration 30 \
  -o vm_guest.json
```

**Полезные флаги:**

| Параметр | Смысл |
|----------|--------|
| `--linpack-binary` | обязательный путь к исполняемому файлу linpack |
| `--pg-host` | `local` — Unix-сокет; `127.0.0.1` — TCP на localhost (для осмысленного учёта трафика на `lo` вместе с `--network-iface`, см. справку скрипта) |
| `--pg-user` / `--pg-database` | роль и БД PostgreSQL |
| `--pg-password` | при необходимости выставляет `PGPASSWORD` для процесса |
| `--pgbench-no-sudo` | не оборачивать pgbench в `sudo` |
| `--no-stress` | без фоновой нагрузки `stress-ng` |
| `--no-perf` | без `perf` |
| `--network-iface` | явно указать интерфейс для `/sys/class/net/...`; иначе для localhost может подставиться `lo` |
| `-o` | файл JSON |

---

## 4. `format_benchmark_output.py` — таблицы и CSV из JSON

Назначение: превратить отчёт из `container_benchmark.py` (`-o`) или `vm_benchmark.py` (`-o`) в читаемые ASCII-таблицы или один CSV.

По умолчанию читается `output.json` в текущем каталоге:

```bash
python3 format_benchmark_output.py
python3 format_benchmark_output.py bench_docker.json
```

Таблицы в терминал; в файл:

```bash
python3 format_benchmark_output.py bench_docker.json -o bench_docker_tables.txt
```

Все фазы одним CSV:

```bash
python3 format_benchmark_output.py bench_docker.json --csv -o bench_docker.csv
```

Если в таблицах пусто по RX/TX, в исходном JSON не было полей `network` (запуск без `--network-iface` в контейнерном драйвере или старый формат файла).

---

## 5. `benchmark_core.py`

Общая логика для контейнерного сценария; **не запускается отдельно** как точка входа — его импортирует `container_benchmark.py`.
