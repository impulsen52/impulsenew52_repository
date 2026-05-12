# Статья №4

Каталог с материалами для четвёртой научной статьи: комплексный бенчмарк виртуальных машин и контейнеров (Astra Linux, РЕД ОС, QEMU-KVM, Docker).

## Содержимое

- **`TASK.md`** — постановка задачи и описание метрик.
- **`TASK_APPLE_ARM.md`** — заметки по сценарию на Apple ARM.
- **Скрипты:** `deploy_vm.py`, `vm_benchmark.py`, `container_benchmark.py`, `benchmark_core.py`, `format_benchmark_output.py`.
- **Данные:** `bench.json`, `bench_distant.json`, `output.json`, `OUTPUT.json` (при необходимости обновляйте или добавляйте версии с понятными именами).

## Запуск

Рабочий каталог — этот (`article_4/`), чтобы пути к JSON по умолчанию и импорт `benchmark_core` работали как раньше. Из корня репозитория сначала выполните `cd article_4`.

```bash
python3 container_benchmark.py --help
```
