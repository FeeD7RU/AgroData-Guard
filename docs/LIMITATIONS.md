# Границы MVP и дальнейшая работа

1. Потоковый quality-control использует статистические/ML baselines. Отдельный
   локальный LLM-агент Qwen3 расследует ситуации по запросу пользователя; его выводы
   требуют проверки. Confidence детекторов — эвристика, не вероятность.
2. Diagnosis bias/noise/drift по одному временному ряду может быть неоднозначен.
   Bias обычно маркируется unknown; trend — лишь предполагаемый drift. Естественный
   смена естественного тренда может дать false positive. Conflict между связанными sensors ещё требует
   явной модели связей/единиц и отдельного multivariate detector.
3. В v2 возврат после изолированного spike распознаётся при устойчивом прошлом.
   Суточные циклы и загрязнение окна после длительного bias всё ещё дают ложные тревоги.
   Episode grouping пока не реализован. После длинного разрыва контекст сбрасывается:
   статистические правила ждут прогрева, поэтому новый in-range bias может быть пропущен.
4. Isolation Forest на одномерном узком/дискретном распределении может пропустить
   экстремальное значение. Не является заменой физических правил и по умолчанию
   не включён live. Переобучение на каждом окне дороже production fitted model.
5. Benchmark содержит controlled synthetic faults, один temporal holdout и
   walk-forward raw history. Нет calibration set/automatic tuning. Correction
   в отдельных corrector-таблицах использует известную fault mask; offline methods имеют
   доступ к будущему. Раздел pipeline оценивает реальный каузальный агент без truth
   во входах и без ручных review. MAE/RMSE нельзя интерпретировать без coverage.
   Снижение FP на regression-сценариях не доказывает качество на реальных датчиках.
6. Анализ синхронный, batch атомарный, один процесс. Для нескольких процессов нужны
   per-sensor DB locks, отдельный source scheduler и профиль производительности.
   Горизонтальное масштабирование и миллионы событий/с не проверялись.
7. SQLite triggers защищают сырьё. PostgreSQL предусмотрен ORM/driver abstraction,
   но нуждается в отдельной проверке и immutable DDL/permissions. Schema migrations
   пока вручную, revision check предотвращает случайный запуск несовместимой версии.
8. Reviewer вводится человеком. Shared API key ограничивает API, но ролей/SSO нет.
   Для публичного развёртывания нужны TLS, identity proxy, ACL и rate limiting.
   Demo и benchmark endpoints имеют те же права, что остальные API endpoints.
9. MQTT local spool требует мониторинга места на диске и ручного разбора quarantine.
   Network simulator второго участника (packet loss, mesh, задержки) не входит в Guard.
10. Offline heartbeat показывает отсутствие свежих сообщений; пропущенные пакеты
    не превращаются в выдуманные измерения. Явные null распознаются как missing.
11. Simulator начинает новый namespace при старте. Демо ускоряет event time ×30;
    не используйте его временные метки как реальные wall-clock телеметрические часы.
12. Docker и PostgreSQL запуск нужно подтвердить в среде с установленными сервисами.
    Текущий validation status отдельно записан в VERIFICATION.md.

Следующий полезный этап: реальные исторические данные, baseline reports по sensor
families, entity-level validation, сезонная модель и восстановление после bias, cross-sensor
relations, сохранение fitted model artifact и calibration confidence.
