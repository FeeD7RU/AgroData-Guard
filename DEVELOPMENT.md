# Разработка

## Карта кода

Начните с `schemas.py` (что приходит), затем `service.py` (что происходит с записью),
`detectors.py`, `correctors.py`, `policy.py` (как принимается решение),
`database.py` (что сохраняется). `api.py` связывает use cases с HTTP.
Не импортируйте FastAPI/SQLAlchemy в математические алгоритмы.

Локальный LLM-агент находится в `agent.py`, разрешённые инструменты — в
`agent_tools.py`. Он использует Ollama/Qwen отдельно от QualityEngine. Инструменты
должны иметь строгую схему аргументов, ограниченный объём результата и не изменять
измерения. Подготовленные рекомендации передаются в существующий ручной review.
См. [устройство и запуск](docs/AI_AGENT.md).

## Конфигурация

Скопируйте `agroguard/defaults.json` в `config/local.json`, измените нужные поля:

```powershell
Copy-Item agroguard/defaults.json config/local.json
$env:AGRO_CONFIG = (Resolve-Path config/local.json).Path
.\.venv\Scripts\agroguard.exe serve --demo
```

Новый тип добавляется ключом в `features`. Если физических границ нет, опустите
min/max — range detector не применяется. `noise_floor` задаётся в единицах измерения,
`max_rate` — units/minute, `period_seconds` — ожидаемая частота. Для синтетики добавьте
этот ключ также в `simulation.features` с base/amplitude/noise/phase.
Тип `unknown` и произвольные новые feature допустимы без изменения кода.

Изменения конфигурации применяются после перезапуска. Старые решения сохраняют
policy snapshot и algorithm_version. При изменении формул увеличивайте версию алгоритма.

Параметры правил находятся в `detection`:

| Параметр | По умолчанию | Назначение |
|---|---:|---|
| zscore_threshold / robust_z_threshold | 3.5 / 4 | Пороги стандартного и robust Z |
| iqr_multiplier | 1.5 | Ширина IQR fence |
| max_gap_periods | 3 | Допустимый разрыв истории в интервалах cadence; null отключает reset |
| rate_recovery_guard | true | Распознавать восстановление после одиночного выброса |
| recovery_band | 2.5 | Полоса возврата в единицах robust scale |
| recovery_reference_fraction | 0.8 | Минимальная доля устойчивой reference history |
| recovery_stable_samples | 3 | Число стабильных точек непосредственно перед выбросом |
| trend_require_change | true | Сравнивать новый наклон с прошлым |
| trend_samples / trend_direction | 12 / 0.85 | Размер недавнего участка и доля согласованных приращений |
| trend_change_scale | 8 | Минимальное избыточное смещение относительно reference residual scale |

При включённом сравнении тренда `window >= min_history + trend_samples - 1`.
Пороги — эвристики; изменение требует повторной оценки, а не только снижения числа тревог.
Версия алгоритма задана в `engine.ALGORITHM_VERSION`.

## Добавление detector / corrector

Detector реализует `detect(Point, history, Feature) -> Detection`. History содержит
только прошлые raw values. Возвращайте evidence и score, но не записывайте ничего в БД.
Добавьте factory в `DETECTORS`, имя в config.detectors и тест с healthy/fault cases.
Factory принимает `(min_history, detection_parameters)`. Для временных правил
используйте `continuous_history`, чтобы не захватывать будущее и контекст до разрыва.
Поле `reason_code` даёт машинно-читаемую причину срабатывания или отказа.
Для обучаемой модели fit должен использовать только прошлое; не использовать ground truth.
Isolation Forest пока переобучается на каждом ограниченном окне и по умолчанию
выключен в live pipeline: это дорогой экспериментальный baseline.

Corrector реализует `correct(point, trusted_history, feature, future=None, offline=False)`.
Никогда не берите будущего соседа без offline=True. Добавьте factory в CORRECTORS,
тест на gap и описание информационных предположений метода.
Linear интерполирует позиции на сетке cadence; time использует расстояния между
timestamp и работает также с нерегулярными рядами. На регулярной сетке результаты совпадают.
`diagnose` не обязан уверенно классифицировать каждый statistical outlier: unknown допустим.

## Источники и транспорт

Source возвращает SensorMeasurement. CSVSource умеет задаваемые mapping/defaults.
Simulator отдаёт пары (measurement, truth); в транспорт идёт только measurement.
HTTPTransport умеет ограниченные retries для 5xx/network error. ID не меняется при retry.
MQTT gateway сохраняет сообщение на диск до ACK, затем передаёт HTTP и удаляет spool
только после успешного ответа. `.rejected` файлы требуют разбора оператором.
Transport-neutral `Guard.ingest` используется HTTP и встроенным demo source.

## База

По умолчанию `data/agroguard.db`. Для PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[postgres]'
$env:DATABASE_URL = 'postgresql+psycopg://user:password@localhost/agroguard'
.\.venv\Scripts\agroguard.exe serve
```

Это смена driver/URL, но PostgreSQL путь требует отдельной integration-проверки
в вашей среде. MVP запускается одним worker. Не добавляйте `--workers` без
транзакционных блокировок по sensor_id и выделенного scheduler для demo.
Schema revision сейчас 1. До изменения модели сделайте backup; напишите явную
миграцию (следующий этап — Alembic) и обновите revision. create_all не является миграцией.
SQLite trigger защищает raw/reviews/truth даже от UPDATE/DELETE SQL.
Для PostgreSQL требуется отдельный DDL trigger или роль без UPDATE/DELETE.

## Проверки

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check agroguard tests
.\.venv\Scripts\python.exe -m pip check
node --check agroguard/static/app.js
```

Реальный MQTT smoke с временным loopback broker и недоступным/восстановленным HTTP:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[mqtt-test]'
.\.venv\Scripts\python.exe -m pytest tests/test_mqtt.py
```

Без optional dependency этот один тест помечается skipped; основной suite не требует broker.

`tests/test_agent.py` проверяет цикл, протокол Ollama, историю, ограничения инструментов,
отсутствие облачных адресов и неизменность измерений с управляемым ответом модели.
Он не доказывает качество реального Qwen. Отдельная проверка с установленной моделью
и запущенным локальным приложением:

```powershell
.\.venv\Scripts\python.exe tests/smoke_local_agent.py
```

Она создаёт явно обозначенный тестовый датчик `llm-smoke.*`, отправляет реальный
запрос Qwen и проверяет вызовы инструментов и отсутствие изменений raw/review.
Отчёт сохраняется в `artifacts/local-agent/live-smoke.json`.

Тесты используют временные базы, не трогают пользовательские данные.
UI проверяется браузером: инжектировать spike, открыть details, approve/reject,
проверить изменение статуса/истории, затем запустить benchmark.
Запуск `agroguard simulate --scenario --steps 120 --interval 0` проверяет внешний HTTP
источник; truth сохраняется в artifacts/network-truth.jsonl.

requirements.lock фиксирует проверенное окружение, включая dev tools. При обновлении:
обновите зависимости в отдельном venv, выполните suite и ручной UI smoke, затем
`python -m pip freeze --exclude-editable > requirements.lock`. Не включайте локальный editable URL.

## Benchmark на собственных данных

Подготовьте проверенный clean CSV, задайте mapping и выберите один sensor_id:

```powershell
.\.venv\Scripts\agroguard.exe benchmark --csv clean.csv --mapping config/csv-mapping.json --sensor-id soil-ph-1 --output artifacts/real-benchmark
```

Нужно 200..1500 непустых измерений одного сенсора с уникальными timestamp.
Они сортируются по времени. Амплитуда faults определяется только по train prefix,
ground truth и исходные clean значения сохраняются в JSON. На реальных нерегулярных
рядах установите cadence/noise_floor в config перед сравнением методов.
Метод не проверяет, что clean действительно чистый: это ответственность подготовки dataset.

Сравнение production pipeline на контролируемых сценариях:

```powershell
.\.venv\Scripts\agroguard.exe evaluate --seeds 11,29,47,83,101 --output artifacts/quality
.\.venv\Scripts\agroguard.exe evaluate --seeds 211,307,419,523,631 --output artifacts/quality-holdout
```

JSON содержит полную конфигурацию, truth, метрики каждого seed и сценария;
Markdown — сводную таблицу. Независимые seed проверяют устойчивость на тех же
семействах синтетики, но не заменяют новые реальные датасеты. [Результаты v2](docs/QUALITY_V2.md).

## Где что хранится

* `data/` — SQLite и MQTT spool; не коммитится.
* `artifacts/` — CLI benchmark и synthetic truth; не коммитится.
* experiments в БД — отчёты dashboard, включая ground truth.
* stdout/stderr — structured application logs плюс стандартный uvicorn access log.

Секреты только через env: API_KEY, DATABASE_URL, MQTT_USERNAME/PASSWORD.
Для ручной идентификации reviewer пока self-reported; при подключении SSO нужно
брать его из проверенной identity, не из тела запроса.
