# Контракт Sensor Network → AgroData Guard v1

Второй участник владеет сетью, nodes, очередью пакетов, clock sync и gateway.
AgroData Guard владеет quality analysis, хранением, correction и review.
Транспорт не влияет на алгоритмы.

```json
{
  "schema_version": "1",
  "measurement_id": "node-01-boot42-seq100",
  "timestamp": "2026-10-06T20:30:00Z",
  "sensor_id": "greenhouse1.temperature.1",
  "node_id": "node01",
  "feature": "temperature",
  "value": 24.2,
  "unit": "°C",
  "location_id": "greenhouse1",
  "quality": "good",
  "source": "zigbee-gateway",
  "sequence_number": 100,
  "metadata": {"firmware": "1.0"}
}
```

timestamp, sensor_id, value обязательны. schema_version при отсутствии = "1";
рекомендуется всегда передавать явно. feature при отсутствии = unknown.
value должен быть числом JSON или null. NaN/Infinity, bool и строковые числа запрещены.
Timezone обязателен; timestamp epoch числом не принимается. UTC — формат хранения.
Дополнительные поля идут в metadata (до 16 KiB), неизвестные top-level поля запрещены.
Значение вне physical range принимается как измерение и становится аномалией,
а не теряется на validation. Единицы должны совпасть с configuration.

## HTTP

`POST /api/v1/measurements` — один объект (201, {id, duplicate}).
`POST /api/v1/measurements/batch` — массив 1..500 (200, {items}).
Один batch атомарный; возвращаемые элементы соответствуют порядку запроса.
Body <= 1 MiB. `X-API-Key` обязателен, если сервер запущен с API_KEY.

* 409: ID/payload conflict, sensor identity conflict; не повторять бесконечно.
* 422: malformed schema; исправить на gateway, сохранить failed payload локально.
* 413: уменьшить batch/body.
* 401: неверный ключ.
* 5xx/timeout: retry с теми же IDs, exponential backoff; данные остаются в gateway spool.

HTTP response после commit означает, что raw и analysis сохранены атомарно.
Payload hash вычисляется из нормализованной модели: equivalent UTC offsets одинаковы.
При отсутствии ID используется UUID5(sensor_id + UTC timestamp).
ID, timestamp и payload должны оставаться стабильными при повторе.
Уникальны также sensor_id+timestamp. Два канала в один момент — разные sensor_id.
Принять исправленное исходное raw под тем же ID нельзя; используйте human review.

## Ordering / loss

Gateway может передавать не по порядку. Batch сортируется по timestamp; late данные
принимаются с is_late, анализируются только по ранним timestamp. Старые решения не
переписываются и benchmark не запускается автоматически.
sequence_number сохраняется, но не заменяет ID и не является глобальным порядком:
после reboot начинайте новый boot prefix в measurement_id.

Отсутствующий пакет не равен измерению null. Если точно известно, что выборка не
состоялась, gateway может отправить null на scheduled timestamp с пояснением metadata.
Иначе UI покажет offline по cadence, но система не будет изобретать raw записи.
Задержки, потери, отключение nodes и метрики сети — ответственность второго участника.

## MQTT

Topic `agroguard/v1/measurements`, один JSON объект на сообщение, QoS 1,
retain=false. Запуск gateway:

```powershell
.\.venv\Scripts\agroguard.exe mqtt --host localhost --port 1883 --url http://127.0.0.1:8000
```

Persistent client ID (`MQTT_CLIENT_ID`) должен быть уникален для каждого gateway.
ACK следует после durable local spool; HTTP commit может произойти позднее.
MQTT ACK не является подтверждением semantic validation. Невалидные сообщения
попадают в `.rejected`, валидные повторяются до успеха HTTP. Следите за свободным диском.
Доставка at-least-once; exactly-once effect обеспечивают DB constraints и idempotency.
Сетевые отказы до доставки на gateway требуют persistence broker и publisher queue.

## REST для потребителей

* GET /api/v1/health, /status
* GET /api/v1/sensors — metadata, status, latest
* GET /api/v1/measurements?sensor_id=...&limit=100&offset=0 — latest/history
* GET /api/v1/anomalies?decision=manual_review&limit=30&offset=0
* GET /api/v1/anomalies/{id} — analysis, context before/after, reviews
* POST /api/v1/anomalies/{id}/reviews — action/reviewer/note/value/expected_version
* GET/POST /api/v1/experiments; GET /api/v1/experiments/{id}
* POST /api/v1/demo/start, /demo/stop, /demo/faults

GET measurements поддерживает `order=timestamp` (history, default) и
`order=received_at` (последние поступления). Anomalies по умолчанию сортируются по
received_at. Это сохраняет видимость новых сообщений после replay данных с будущими
event timestamps. Sensor status измеряется по последнему receive time, с cadence
из config; у встроенного demo cadence учитывает ускорение ×30.

`accepted_value` существует только для approved/auto_approved. `effective_value`
выбирает accepted или raw, не уничтожая raw. Повторный review требует свежей version.

## При подключении реальной сети

Сначала contract tests HTTP/CSV, затем replay реальных исторических рядов,
проверка единиц/cadence/часов, оценка false positives и ручная калибровка policy.
Auto-correct держите выключенным, пока реконструкция не проверена на ваших данных.
