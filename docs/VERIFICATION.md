# Проверка реализации — 18 октября 2026

## Локальный ИИ-агент (18 октября)

Усиление расследований: добавлены контекст до/после события, расчёты по raw,
поиск незакрытых аномалий и промежуточное сохранение trace. Полный suite после
изменений и проверки обязательных рекомендаций: **135 passed**, 20.21 s.
Ruff, синтаксис JavaScript и повторная сборка wheel пройдены.
Реальный прогон выявил ошибку аргументов list_anomalies с последующим неверным
обоснованием незакрытости через accepted_value. Добавлены адресные ошибки валидации
(названия полей и типы ошибок без содержимого пользовательских данных) и инструкция
повторять неверный вызов. Незакрытость определяется отсутствием human review.
Финальный реальный прогон после добавления prepare_review прошёл:
`3dd0abd7a16e4956845e2ad00d5bb5d4`, 7 вызовов Qwen, четыре инструмента расследования,
сохранённое предложение и неизменность записи анализа. Отдельно прошли продолжение
диалога, вызов list_anomalies и получение ожидаемого ID без нового предложения.
Результаты: `artifacts/local-agent/live-smoke.json` и `live-discovery.json`.
Стабильность на широком наборе вопросов и качество причинных объяснений не измерены;
успешный прогон не означает гарантированную точность всех последующих ответов.

* Установлены portable Ollama v0.40.6 и локальная qwen3:14b; cloud отключён.
* Полный suite: **129 passed**, 20.56 s; Ruff пройден. Wheel пересобран успешно.
* Выполнен `python tests/smoke_local_agent.py` с настоящей моделью, без mock:
  run `f6f93d795445412b9696e4ab73d37e31`, статус completed, 6 вызовов модели.
  Агент вызвал inspect_anomaly, inspect_sensor, get_analysis_rules, propose_review.
  На явно синтетическом датчике исследован скачок с примерно 20 до 45 °C;
  предложение сохранено, исходная запись анализа и review не изменились.
  Полный trace: `artifacts/local-agent/live-smoke.json`.
* Первый реальный прогон обнаружил преждевременный ответ без истории. Добавлены
  проверка полноты расследования и регрессионный тест; повторный прогон прошёл.
* Это проверка интеграции на одном сценарии, а не оценка точности Qwen на реальных
  сенсорах. Тесты протокола используют ScriptedProvider; указанный smoke — реальную модель.
* Визуальная проверка готового ответа в этой сессии не выполнена: политика встроенного
  браузера заблокировала доступ к вкладке. Предыдущие проверки dashboard ниже относятся к v2.

## Предыдущая проверка baseline-v2 (7 октября)

Среда: Windows, Python 3.12.14, локальный SQLite. Запуск с реальной файловой БД,
HTTP на 127.0.0.1:8000 и браузер Codex. Зависимости зафиксированы в requirements.lock.

## Автоматические проверки

* Полный suite после v2: **100 passed**, 18.25 s. Включён optional MQTT integration с aMQTT.
* Ruff: все проверки пройдены.
* JavaScript: `node --check agroguard/static/app.js` пройден.
* `pip check`: несовместимых зависимостей нет.
* Wheel build: успешно, HTML/CSS/JS/defaults входят в пакет.

Suite включает schema/NaN/UTC, неизвестные типы, idempotency и конкурентные дубли,
rollback batch, late messages без будущего контекста, immutable raw, history/review,
optimistic version, все baseline detectors/correctors, irregular interpolation,
раздельные confidence, policy, API key, request limits, DB errors, persistence после
перезапуска, deterministic simulator/faults, CSV, historical и synthetic benchmark.
Добавлены изолированный spike/возврат, серии выбросов, устойчивый и новый тренд,
cadence, разрыв/явные null, валидация порогов, совпадение replay с сохранёнными API
решениями и независимость решений от truth. [Сравнение качества v2](QUALITY_V2.md).

Предупреждение dependency: Starlette сообщает о будущей смене TestClient с httpx
на httpx2. Это deprecation warning, не ошибка приложения или тестов. Зафиксированные
версии сейчас совместимы; при плановом обновлении dependencies нужно проверить TestClient.

## Реальные end-to-end сценарии

* После обновления v2 приложение перезапущено с существующей БД. Сохранены 103886
  исторических анализов baseline-v1; новые измерения получают baseline-v2.
  Перед перезапуском создана согласованная SQLite backup `data/backups/before-quality-v2.db`.
* HTTP smoke v2: 40 здоровых точек → spike 45 → возврат 20.1. Spike получил
  suggested_correction, возврат — normal с evidence rate_recovery. Результат:
  `artifacts/quality/http-smoke.json`.
* Через dashboard выполнен новый benchmark seed 42, 300 точек. Раздел полного агента:
  precision 1, recall 50/61, FP 0, FN 11, correction coverage 30/61. Это один
  синтетический сценарий; сравнительный suite описан в QUALITY_V2.md.
  Browser console без ошибок; скриншот `quality-v2-dashboard.jpg`.
* Проверен переход со старых отчётов: они остаются доступны и помечены как отчёты
  без оценки полного агента. URL JavaScript содержит версию для обновления browser cache.
* simulator → HTTP → ingestion → analysis → SQLite → API выполнен также отдельным CLI
  `simulate --scenario --steps 120 --interval 0` (spike, missing, stuck, drift).
* Через браузер инжектирован spike; raw около 49.5, suggestion около 24.2,
  anomaly score 0.99 и correction score 0.89. Approve и Reject сохранены как две
  записи review; raw не изменился. Затем проверен повторный запуск приложения.
* Dashboard показывает реальные БД-измерения, графики и таблицы; в browser console
  ошибок JavaScript не обнаружено. Скриншот: dashboard.jpg.
* Benchmark запущен через CLI и через dashboard; report и ground truth сохранены.
* MQTT: настоящий loopback broker, QoS1 → durable spool во время недоступности HTTP
  → восстановление HTTP → одна запись в БД. Повтор не создаёт дубль, malformed JSON
  сохраняется в quarantine. Это не mock broker.

Исторический CLI report MVP до v2: `artifacts/benchmark/3fc6d2044f374bcb99e04e95556c6ce4.json`
и соответствующий `.md`. Seed 42, 300 точек, temporal train/test 120/180.
Robust Z получил F1 ≈ 0.849, Isolation Forest ≈ 0.611 в этом конкретном сценарии.
Это не вывод о качестве на физических сенсорах и не настройка BEST_MODEL.

## Что не проверено здесь

* Docker image/Compose runtime: Docker не установлен в среде.
* PostgreSQL driver, immutable permissions/DDL и конкурентность нескольких workers.
* Физические датчики, реальная mesh/Zigbee сеть, broker TLS и production ACL.
* Нагрузочная ёмкость на сотнях постоянно работающих сенсоров.
* Калибровка confidence, cross-sensor conflict и качество на реальном датасете.

Эти ограничения не мешают локальному HTTP/MQTT/SQLite/demo сценарию, но остаются
отдельными проверками перед реальным развёртыванием. Подробности в LIMITATIONS.md.
