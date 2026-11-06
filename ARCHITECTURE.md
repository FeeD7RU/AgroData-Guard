# AgroData Guard — архитектура MVP

## Цель и границы

Модульный монолит для десятков/сотен сенсоров: приём, неизменяемое сырьё,
каузальный анализ, предложения коррекции, ручная проверка и воспроизводимый benchmark.
Сеть и физические датчики находятся за границей приложения. Локальный Qwen3 через
Ollama образует отдельный слой расследования с ограниченными инструментами и
сохранённой историей диалогов. Он не заменяет потоковый QualityEngine. Распределённые
очереди и автоматическое обучение на production-данных в MVP не входят.

LLM-слой: `agent.py → Ollama/Qwen → agent_tools.py → SQLite → результат инструмента →
Qwen → ответ / предложение review`. Инструменты не применяют изменения к измерениям.
Подробности, запуск и ограничения: [ИИ-агент](docs/AI_AGENT.md).

## Решения

* Python 3.12+, FastAPI/Pydantic: единая типизированная схема и OpenAPI.
* SQLAlchemy 2: SQLite по умолчанию, PostgreSQL через DATABASE_URL и psycopg.
* Один процесс API; синхронная атомарная транзакция на batch. SQLite использует WAL.
  Для нескольких writers сначала перейти на PostgreSQL и добавить per-sensor locks.
* Dashboard: локальные HTML/CSS/JS и SVG, данные только из API, без CDN и fake data.
* HTTP — основной транспорт. MQTT — отдельный процесс-адаптер с QoS 1,
  ручным ACK после durable spool и reconnect. ML не знает о транспорте.
* NumPy и scikit-learn: статистические baselines и Isolation Forest. Победитель заранее не выбран.
* pytest: unit, integration и полный simulator → API → DB → review тест.

## Модули и поток

`sources / simulator → SensorMeasurement v1 → ingestion → raw storage →
detectors → diagnosis → correctors → policy → analysis storage → API → dashboard`

`agroguard/`: schemas, config, database, detectors, correctors, policy, service,
api, simulator, sources, benchmark, cli; `static/`: dashboard;
`config/`: типы датчиков и policy; `tests/`, `docs/`: проверки и контракты.

Детекторы и correctors — небольшие Protocol-интерфейсы с registry.
Диагноз отделён от обнаружения. Неоднозначные сдвиги получают unknown.
Ground truth сохраняется отдельно и никогда не подаётся в production-анализ.
`QualityEngine` объединяет detection, diagnosis, correction и policy; его вызывают
и ingestion, и `evaluation.replay_pipeline`. Replay воспроизводит доверенный контекст
normal/auto_approved, без имитации ручных решений. Формулы не дублируются в benchmark.

## Контракт v1

Обязательные поля: schema_version="1", timestamp (ISO 8601 с timezone), sensor_id,
value (конечное число или null). Необязательные: measurement_id, node_id, feature,
unit, location_id, quality, source, metadata, sequence_number.
Все времена нормализуются в UTC. Naive timestamps, NaN/Infinity и лишние поля отклоняются.
При отсутствии measurement_id вычисляется UUID5 по sensor_id + UTC timestamp.
Повтор того же ID и того же нормализованного payload — успешный no-op;
тот же ID с другим payload — 409. Также уникальны sensor_id + timestamp.
Один timestamp соответствует одному измерению одного sensor_id: для нескольких
каналов нужны разные sensor_id. Менять единицы существующего сенсора без нового ID нельзя.

Batch ограничен, валидируется целиком, сортируется по timestamp и сохраняется атомарно.
Late messages сохраняются и анализируются только по предшествующему контексту;
старые решения не пересчитываются автоматически. Analysis хранит is_late.
Нормализованный payload сохраняется вместе с raw_value без потери исходного числа.
Отсутствующее сообщение отличается от явного null: offline определяется по cadence,
но невидимые сообщения не выдумываются в raw-таблице. Gateway может прислать null.

## Хранение

sensor_nodes → sensors → measurements → analysis_results → human_reviews.
Analysis включает anomaly_type, evidence, suggestion, оба confidence, решение,
версию алгоритма и snapshot policy. Аномалия и предложение — части одного результата:
нет необходимости в трёх таблицах с отношением 1:1. Reviews — append-only журнал.
experiments хранит seed, config, split, версии библиотек и результаты; файлы
benchmark содержат ground_truth и отчёт. synthetic_truth — отдельная таблица демо.
Schema revision фиксируется; несовместимая версия БД останавливает запуск.

## Время, confidence, решения

Online-окно содержит только timestamp < текущего. Статистики detection используют
сырые прошлые наблюдения; correction использует только normal/approved прошлые значения.
Interpolation с правым соседом разрешена только явно в offline-режиме; live fallback
использует median/forward fill с пониженным confidence. Длинные gaps не исправляются.
Обе оценки confidence — heuristic score [0,1], не вероятность.

В `baseline-v2` временные детекторы используют непрерывный прошлый контекст:
разрыв более `detection.max_gap_periods` интервалов cadence обрывает окно. Явные null
не скрывают разрыв. Range и missing работают и без накопленной истории.
Защита rate от повторной тревоги требует изолированного выброса и возврата в устойчивый
диапазон; серии шумовых скачков не получают это исключение. Trend сравнивает наклон
последнего участка с прошлым, используя реальные timestamp и масштаб остатков.
После разрыва статистике нужно накопить историю; допустимый по физическим границам
сдвиг уровня сразу после восстановления может остаться незамеченным.
Параметры detection входят в snapshot результата. Новая версия применяется только
к новым записям; исторические analysis и review сохраняются без пересчёта.

normal → no_action; слабый сигнал → observation; уверенная аномалия и хорошая
коррекция → suggested_correction; иначе → manual_review. Политика может разрешить
auto_approved. Человек может approve (предложение или собственное значение), reject,
ignore или отправить на review. Версия результата защищает от потерянного обновления.
Raw никогда не обновляется; accepted_value хранится отдельно. Reject оставляет raw
в отображаемом effective value, но не делает его доверенным для correction.

## Benchmark без leakage

Хронологический train prefix и test suffix; никаких shuffle.
Isolation Forest fit только на прошлом. Пороговые статистики используют прошлое.
Corruptions в test, train clean; это контролируемый сценарий, а не доказательство
качества на реальной сети. Сравниваются detection precision/recall/F1/FPR,
drift delay, correction MAE/RMSE и coverage. Невосстановленные точки нельзя скрывать:
coverage и число пропусков выводятся рядом с ошибкой. Отдельно online и offline
correction, чтобы interpolation с будущим не сравнивалась как causal.
В `pipeline` дополнительно оценивается общий production engine, включая предложения
коррекции здоровых точек и нагрузку на review. Truth используется только после replay
для подсчёта метрик. MAE/RMSE считаются на неисправных точках с предложением;
coverage показывает долю всех неисправных точек, для которых оно появилось.
Отдельные corrector-таблицы по-прежнему используют oracle fault mask и не заменяют
эту оценку. `quality_suite` сравнивает старые правила (выключенные v2 guards)
и текущие на фиксированных сценариях; это не отдельная сохранённая сборка v1.

## Эксплуатация и риски

Локальный запуск на loopback; для удалённого доступа API_KEY обязателен,
TLS и identity reviewer обеспечивает reverse proxy. API key не заменяет роли.
Structured logging, bounded body/batch, rollback и health/readiness.
MVP не гарантирует multi-worker serializability анализа и не калибрует confidence.
Автокоррекция disabled. MQTT требует persistent broker/session для доставки
после отключения. Реальные данные требуют проверки единиц, cadence и benchmark.

## Источники решений

* https://fastapi.tiangolo.com/tutorial/sql-databases/
* https://docs.sqlalchemy.org/en/20/core/engines.html
* https://scikit-learn.org/stable/modules/generated/sklearn.ensemble.IsolationForest.html
