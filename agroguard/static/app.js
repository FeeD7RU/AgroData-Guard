'use strict';
const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
} [c]));
const fmt = v => v == null ? '—' : Number(v).toLocaleString('ru-RU', {
    maximumFractionDigits: 2
});
const stamp = v => new Date(v).toLocaleString('ru-RU', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
});
let apiKey = '',
    page = 'overview',
    selected = '',
    detail = null,
    offset = 0,
    totalAnomalies = 0,
    refreshing = false;

function notice(message, error = false) {
    $('notice').hidden = false;
    $('notice').textContent = message;
    $('notice').className = error ? 'error' : '';
}
async function api(path, body) {
    const response = await fetch('/api/v1' + path, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-API-Key': apiKey
        },
        body: body === undefined ? undefined : JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) {
        if (response.status === 401 && !$('key-dialog').open) $('key-dialog').showModal();
        throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    }
    return data;
}
const action = fn => async event => {
    try {
        await fn(event);
    } catch (error) {
        notice(error.message, true);
    }
};

function badge(v) {
    const cls = ['manual_review', 'suggested_correction', 'observation'].includes(v) ? 'warn' : ['rejected', 'ignored'].includes(v) ? 'neutral' : '';
    return `<span class="tag ${cls}">${esc(v)}</span>`;
}

function table(headers, rows) {
    return rows.length ? `<table><thead><tr>${headers.map(v=>`<th>${esc(v)}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${v}</td>`).join('')}</tr>`).join('')}</tbody></table>` : '<div class="empty">Пока нет данных. Запустите датчики или отправьте измерения через API.</div>';
}

function chart(target, rows) {
    if (!rows.length) {
        target.innerHTML = '<div class="empty">История появится после приёма измерений</div>';
        return;
    }
    rows = [...rows].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    const nums = rows.flatMap(r => [r.raw_value, r.accepted_value]).filter(v => v != null);
    if (!nums.length) {
        target.textContent = 'Все значения отсутствуют';
        return;
    }
    let low = Math.min(...nums),
        high = Math.max(...nums);
    const pad = Math.max((high - low) * .15, 1);
    low -= pad;
    high += pad;
    const t0 = +new Date(rows[0].timestamp),
        t1 = +new Date(rows.at(-1).timestamp);
    const x = r => 55 + (+new Date(r.timestamp) - t0) / Math.max(1, t1 - t0) * 690,
        y = v => 235 - (v - low) / (high - low) * 205;
    let path = '',
        pen = false;
    for (const r of rows) {
        if (r.raw_value == null) {
            pen = false;
            continue;
        }
        path += `${pen?'L':'M'}${x(r).toFixed(1)},${y(r.raw_value).toFixed(1)} `;
        pen = true;
    }
    const grid = Array.from({
        length: 5
    }, (_, i) => {
        const v = low + (high - low) * i / 4;
        return `<line x1="55" x2="745" y1="${y(v)}" y2="${y(v)}" stroke="#e9eee5"/><text x="45" y="${y(v)+4}" text-anchor="end" fill="#879581" font-size="10">${fmt(v)}</text>`;
    }).join('');
    const dots = rows.filter(r => r.is_anomaly).map(r => `<circle cx="${x(r)}" cy="${r.raw_value==null?235:y(r.raw_value)}" r="4" fill="#cf8647"><title>${esc(r.anomaly_type)} · ${esc(stamp(r.timestamp))}</title></circle>`).join('');
    const accepted = rows.filter(r => r.accepted_value != null).map(r => `<circle cx="${x(r)}" cy="${y(r.accepted_value)}" r="4" fill="#537ea9"/>`).join('');
    target.innerHTML = `<svg viewBox="0 0 780 280" role="img" aria-label="История raw-значений, аномалии и принятые коррекции">${grid}<path d="${path}" fill="none" stroke="#397b50" stroke-width="1.8"/>${dots}${accepted}<text x="55" y="267" font-size="10" fill="#879581">${esc(stamp(rows[0].timestamp))}</text><text x="745" y="267" text-anchor="end" font-size="10" fill="#879581">${esc(stamp(rows.at(-1).timestamp))}</text></svg>`;
}

function options(target, sensors, old) {
    target.innerHTML = sensors.map(s => `<option value="${esc(s.id)}">${esc(s.id)}</option>`).join('');
    if (sensors.some(s => s.id === old)) target.value = old;
}
async function history() {
    if (!selected) return;
    const data = await api('/measurements?sensor_id=' + encodeURIComponent(selected) + '&limit=180');
    chart($('main-chart'), data.items);
    chart($('sensor-chart'), data.items);
    $('sensor-history-title').textContent = selected;
    $('chart-caption').textContent = 'Последние ' + data.items.length + ' точек · время браузера ' + Intl.DateTimeFormat().resolvedOptions().timeZone;
}
async function anomalies() {
    const data = await api('/anomalies?limit=30&offset=' + offset + '&decision=' + encodeURIComponent($('decision-filter').value));
    totalAnomalies = data.total;
    $('anomaly-table').innerHTML = table(['Время', 'Датчик', 'Raw', 'Тип', 'Anomaly score', 'Предложение', 'Correction score', 'Метод', 'Решение'], data.items.map(r => [`<button data-detail="${esc(r.id)}">${esc(stamp(r.timestamp))} ↗</button>`, esc(r.sensor_id), fmt(r.raw_value), badge(r.anomaly_type), fmt(r.anomaly_confidence), fmt(r.suggested_value), fmt(r.correction_confidence), esc(r.detector || 'quality'), badge(r.decision)]));
    $('anomaly-count').textContent = `${data.total?offset+1:0}–${Math.min(offset+30,data.total)} из ${data.total}`;
    $('prev-anomalies').disabled = offset === 0;
    $('next-anomalies').disabled = offset + 30 >= data.total;
}
function pipelineSummary(p) {
    if (!p) return '<p class="caption">Старый отчёт: оценка полного агента появится при новом запуске benchmark.</p>';
    const d = p.detection, c = p.correction;
    return `<h3>Агент целиком · ${esc(p.algorithm_version)}</h3>
        <p class="caption">Тот же алгоритм, что принимает live-решения. Только прошлые данные; ground truth используется после анализа для подсчёта ошибок.</p>
        <div class="table-wrap">${table(['Precision', 'Recall', 'F1', 'Ложные тревоги', 'Пропущенные ошибки', 'На проверку'],
            [[fmt(d.precision), fmt(d.recall), fmt(d.f1), fmt(d.fp), fmt(d.fn), fmt(p.review_items)]])}</div>
        <div class="table-wrap">${table(['Предложено исправлений ошибок', 'Coverage', 'MAE', 'RMSE', 'Предложения на исправных точках'],
            [[`${c.fault_suggestions}/${c.fault_targets}`, c.coverage == null ? '—' : fmt(c.coverage * 100) + '%', fmt(c.mae), fmt(c.rmse), fmt(c.healthy_suggestions)]])}</div>
        <p class="caption">Правильный тип ошибки и сам факт обнаружения оцениваются отдельно в JSON. Coverage учитывает все ошибки, включая необнаруженные.</p>
        <h3>Отдельные детекторы</h3>`;
}
async function experiments() {
    const data = await api('/experiments');
    $('experiment-list').innerHTML = data.items.length ? data.items.map(e => {
        const r = e.report;
        return `<div class="experiment"><div class="panel-heading"><h3>${esc(stamp(e.created_at))} · seed ${r.seed}</h3><button class="quiet" data-download="${esc(e.id)}">Скачать JSON + ground truth ↓</button></div>
            ${pipelineSummary(r.pipeline)}
            <div class="bars">${r.detectors.map(d=>`<div class="bar-row"><span>${esc(d.method)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.max(0,Math.min(100,d.f1*100))}%"></div></div><b>${fmt(d.f1)} F1</b></div>`).join('')}</div>
            <div class="table-wrap">${table(['Detector','Precision','Recall','F1','FPR','Drift delay, s'],r.detectors.map(d=>[esc(d.method),fmt(d.precision),fmt(d.recall),fmt(d.f1),fmt(d.false_positive_rate),d.drift_delay_seconds.map(v=>v==null?'не обнаружен':fmt(v)).join(', ')]))}</div>
            <h3>Восстановление · условно на известной маске ошибок</h3>
            <div class="table-wrap">${table(['Corrector','Режим','MAE','RMSE','Coverage','Точек'],r.correctors.map(c=>[esc(c.method),badge(c.mode),fmt(c.mae),fmt(c.rmse),fmt(c.coverage*100)+'%',`${c.reconstructed}/${c.targets}`]))}</div></div>`;
    }).join('') : '<div class="empty">Запустите первый эксперимент, чтобы сравнить baselines.</div>';
}
async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
        const [status, sensors, latest] = await Promise.all([api('/status'), api('/sensors'), api('/measurements?limit=8&order=received_at')]);
        $('health').textContent = '● Система работает';
        $('updated').textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU');
        for (const [id, key] of [
                ['sensors', 'active_sensors'],
                ['measurements', 'measurements'],
                ['anomalies', 'anomalies'],
                ['review', 'manual_review']
            ]) $('stat-' + id).textContent = fmt(status[key]);
        $('auto-policy').textContent = status.policy.auto_correct ? 'on' : 'off';
        if (!selected && sensors.items.length) selected = (sensors.items.find(s => status.demo.sensor_ids.includes(s.id) && s.feature === 'temperature') || sensors.items.find(s => s.status === 'active') || sensors.items[0]).id;
        options($('chart-sensor'), sensors.items, selected);
        options($('fault-sensor'), status.demo.sensor_ids.map(id => ({
            id
        })), $('fault-sensor').value);
        $('start-demo').disabled = status.demo.running;
        $('stop-demo').disabled = !status.demo.running;
        $('demo-state').textContent = status.demo.error ? 'Ошибка: ' + status.demo.error : status.demo.running ? '● Работает · ' + stamp(status.demo.clock) : 'Симулятор остановлен';
        $('latest-table').innerHTML = table(['Время', 'Датчик', 'Показатель', 'Raw', 'Решение'], latest.items.map(r => [esc(stamp(r.timestamp)), esc(r.sensor_id), esc(r.payload.feature), fmt(r.raw_value) + ' ' + esc(r.payload.unit || ''), badge(r.decision)]));
        $('sensor-cards').innerHTML = sensors.items.map(s => `<button class="sensor-card" data-sensor="${esc(s.id)}"><div class="panel-heading">${badge(s.status)}<span class="caption">${esc(s.node_id||'—')}</span></div><h2>${esc(s.id)}</h2><strong>${fmt(s.latest?.raw_value)}</strong> ${esc(s.unit||'')}<small>${esc(s.feature)} · ${esc(s.location_id||'без локации')}</small><span class="caption">Открыть историю →</span></button>`).join('') || '<p class="empty">Датчики регистрируются при первом измерении.</p>';
        if (page === 'overview' || page === 'sensors') await history();
        if (page === 'anomalies') await anomalies();
    } catch (e) {
        $('health').textContent = '● Нет связи';
        notice(e.message, true);
    } finally {
        refreshing = false;
    }
}
async function openDetail(id) {
    detail = await api('/anomalies/' + encodeURIComponent(id));
    const r = detail.analysis;
    $('detail-title').textContent = r.sensor_id + ' · ' + r.anomaly_type;
    $('detail-content').innerHTML = `<p class="caption">${esc(stamp(r.timestamp))} · ${esc(r.detector||'source quality')} · ${esc(r.decision)} · version ${r.version}${r.is_late?' · late message':''}</p><div class="detail-grid"><div><small>Raw</small><strong>${fmt(r.raw_value)}</strong></div><div><small>Предложение</small><strong>${fmt(r.suggested_value)}</strong></div><div><small>Anomaly score</small><strong>${fmt(r.anomaly_confidence)}</strong></div><div><small>Correction score</small><strong>${fmt(r.correction_confidence)}</strong></div></div><div id="detail-chart" class="chart"></div><details><summary>Объяснение и policy</summary><pre>${esc(JSON.stringify({evidence:r.evidence,policy:r.policy},null,2))}</pre></details>`;
    chart($('detail-chart'), detail.context);
    $('detail-content').insertAdjacentHTML('beforeend', `<button class="secondary" data-agent-anomaly="${esc(r.id)}">✦ Разобрать с ИИ-агентом</button>`);
    $('manual-value').value = '';
    $('review-history').innerHTML = detail.reviews.length ? table(['Время', 'Reviewer', 'Решение', 'Значение', 'Комментарий'], detail.reviews.map(r => [esc(stamp(r.created_at)), esc(r.reviewer), badge(r.action), fmt(r.accepted_value), esc(r.note)])) : '<p class="caption">Эту аномалию ещё никто не проверял.</p>';
    if (!$('detail-dialog').open) $('detail-dialog').showModal();
}
document.querySelectorAll('.nav').forEach(button => button.addEventListener('click', action(async () => {
    page = button.dataset.page;
    document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b === button));
    document.querySelectorAll('.page').forEach(p => p.hidden = p.id !== 'page-' + page);
    const titles = {
        overview: 'Обзор',
        sensors: 'Сенсорная сеть',
        anomalies: 'Каждая аномалия — объяснима.',
        experiments: 'Проверяем методы на данных.',
        agent: 'Разберёмся вместе с ИИ.'
    };
    $('page-title').textContent = titles[page];
    $('section-name').textContent = button.textContent.trim().toUpperCase();
    if (page === 'experiments') await experiments();
    if (page === 'agent') await loadAgent();
    await refresh();
})));
$('chart-sensor').addEventListener('change', action(async () => {
    selected = $('chart-sensor').value;
    await history();
}));
$('start-demo').addEventListener('click', action(async () => {
    $('start-demo').disabled = true;
    await api('/demo/start', {});
    notice('Датчики запущены. История прогрева прошла ingestion и сохранена.');
    await refresh();
}));
$('stop-demo').addEventListener('click', action(async () => {
    await api('/demo/stop', {});
    await refresh();
}));
$('fault-kind').addEventListener('change', () => {
    $('fault-duration').value = $('fault-kind').value === 'spike' ? 1 : 12;
});
$('fault-form').addEventListener('submit', action(async e => {
    e.preventDefault();
    await api('/demo/faults', {
        sensor_id: $('fault-sensor').value,
        kind: $('fault-kind').value,
        duration: Number($('fault-duration').value),
        magnitude: Number($('fault-magnitude').value)
    });
    notice('Неисправность запланирована. Следующие измерения пройдут обычный pipeline.');
    selected = $('fault-sensor').value;
    $('chart-sensor').value = selected;
    await history();
}));
$('decision-filter').addEventListener('change', action(async () => {
    offset = 0;
    await anomalies();
}));
$('prev-anomalies').addEventListener('click', action(async () => {
    offset = Math.max(0, offset - 30);
    await anomalies();
}));
$('next-anomalies').addEventListener('click', action(async () => {
    offset += 30;
    await anomalies();
}));
document.addEventListener('click', action(async e => {
    const d = e.target.closest('[data-detail]');
    if (d) await openDetail(d.dataset.detail);
    const s = e.target.closest('[data-sensor]');
    if (s) {
        selected = s.dataset.sensor;
        await history();
    }
    const dl = e.target.closest('[data-download]');
    if (dl) {
        const report = await api('/experiments/' + dl.dataset.download);
        const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {
            type: 'application/json'
        }));
        const a = document.createElement('a');
        a.href = url;
        a.download = 'benchmark-' + report.id + '.json';
        a.click();
        URL.revokeObjectURL(url);
    }
}));
$('close-detail').addEventListener('click', () => $('detail-dialog').close());
$('review-form').addEventListener('submit', action(async e => {
    e.preventDefault();
    const actionName = e.submitter.value;
    const value = $('manual-value').value;
    await api('/anomalies/' + encodeURIComponent(detail.analysis.id) + '/reviews', {
        action: actionName,
        reviewer: $('reviewer').value,
        note: $('review-note').value,
        expected_version: detail.analysis.version,
        value: actionName === 'approve' && value !== '' ? Number(value) : null
    });
    notice('Решение сохранено в журнале. Raw-значение осталось неизменным.');
    await openDetail(detail.analysis.id);
    await refresh();
}));
$('run-benchmark').addEventListener('click', action(async () => {
    const b = $('run-benchmark');
    b.disabled = true;
    b.textContent = 'Выполняется…';
    try {
        await api('/experiments', {
            seed: 42,
            size: 300
        });
        await experiments();
        notice('Эксперимент завершён и сохранён.');
    } finally {
        b.disabled = false;
        b.textContent = 'Запустить benchmark';
    }
}));
$('key-button').addEventListener('click', () => $('key-dialog').showModal());
$('key-form').addEventListener('submit', action(async e => {
    e.preventDefault();
    apiKey = $('api-key').value;
    $('api-key').value = '';
    $('key-dialog').close();
    await refresh();
}));
refresh();
setInterval(refresh, 3000);

let agentParent = null, agentBusy = false, agentConfigured = false;
async function loadAgent() {
    const [state, history] = await Promise.all([api('/agent/status'), api('/agent/runs')]);
    agentConfigured = state.configured;
    $('agent-model').textContent = state.model;
    $('agent-connection').textContent = state.message;
    $('agent-setup').hidden = state.configured;
    $('agent-setup').open = !state.configured;
    $('agent-send').disabled = !agentConfigured || agentBusy;
    $('agent-history').innerHTML = history.items.map(r => `<button class="agent-history-item" data-agent-run="${esc(r.id)}"><strong>${esc(r.question)}</strong><small>${esc(stamp(r.created_at))} · ${esc(r.status)}</small></button>`).join('') || '<p class="caption">Запросов пока нет.</p>';
}
function renderAgent(r) {
    agentParent = r.id;
    $('agent-context').textContent = 'Продолжение: ' + r.question;
    $('agent-result').innerHTML = `<h3>${esc(r.question)}</h3><p class="caption">${esc(r.model)} · ${esc(r.status)} · ${fmt(r.usage.model_calls || 0)} обращений к модели</p>
        ${r.error ? `<p class="error">${esc(r.error)}</p>` : ''}
        ${r.status === 'running' ? '<p>Запрос ещё выполняется или был прерван перезапуском сервера. Откройте запись позже либо повторите вопрос.</p>' : ''}
        <div class="agent-answer">${esc(r.answer)}</div>
        ${r.trace.filter(t => t.tool === 'inspect_anomaly' && t.result.event_context).map(t => {
            const f = t.result.event_context.computed_facts;
            return `<div class="agent-proposal"><strong>Проверенные расчёты · ${esc(t.result.id)}</strong><p class="caption">${esc(t.result.timestamp)} · исходные значения, без подстановки исправлений</p><p>Медиана до события: ${fmt(f.prior_median)} · точек: ${fmt(f.prior_count)}<br>Изменение к предыдущей точке: ${fmt(f.delta_from_previous_raw)}<br>Следующее измерение: ${fmt(f.next_raw)}</p><small>Это контекст события, а не доказательство причины. Последующие точки доступны только для ретроспективного анализа.</small></div>`;
        }).join('')}
        ${r.proposals.map(p => `<div class="agent-proposal"><strong>Предложение: ${esc(p.action)}</strong><p>${esc(p.reason)}</p><p class="caption">${esc(p.analysis_id)} · версия ${p.expected_version} · значение ${fmt(p.value)}</p><button class="secondary" data-detail="${esc(p.analysis_id)}">Открыть для ручной проверки</button><small>Предложение ещё не применено. В карточке показано текущее состояние записи.</small></div>`).join('')}
        <details class="agent-trace"><summary>Инструменты и использованные данные (${r.trace.length})</summary>${r.trace.map(t => `<h4>${esc(t.tool)}</h4><pre>${esc(JSON.stringify({arguments:t.arguments,result:t.result},null,2))}</pre>`).join('')}</details>`;
}
$('agent-form').addEventListener('submit', action(async e => {
    e.preventDefault();
    if (agentBusy || !agentConfigured) return;
    agentBusy = true;
    $('agent-send').disabled = true;
    $('agent-new').disabled = true;
    $('agent-progress').hidden = false;
    try {
        const result = await api('/agent/runs', {question: $('agent-question').value, parent_run_id: agentParent, prepare_review: $('agent-prepare-review').checked});
        renderAgent(result);
        if (result.status === 'completed') $('agent-question').value = '';
    } finally {
        agentBusy = false;
        $('agent-progress').hidden = true;
        $('agent-new').disabled = false;
        $('agent-send').disabled = !agentConfigured;
        await loadAgent();
    }
}));
$('agent-new').addEventListener('click', () => {
    agentParent = null;
    $('agent-prepare-review').checked = false;
    $('agent-context').textContent = 'Новый разговор';
    $('agent-question').value = '';
    $('agent-result').innerHTML = '<div class="empty">Новый разговор. История предыдущих запросов сохранена.</div>';
});
document.addEventListener('click', action(async e => {
    const example = e.target.closest('[data-agent-example]');
    const anomaly = e.target.closest('[data-agent-anomaly]');
    if (anomaly && !agentBusy) {
        $('detail-dialog').close();
        agentParent = null;
        $('agent-context').textContent = 'Новый разговор об аномалии';
        document.querySelector('[data-page="agent"]').click();
        $('agent-question').value = `Разбери аномалию ${anomaly.dataset.agentAnomaly}. Проверь историю датчика и причины срабатывания. Какие факты подтверждают сбой, а какие допускают ложную тревогу? Предложи следующий шаг.`;
    }
    if (example && !agentBusy) {
        $('agent-question').value = example.dataset.agentExample;
        $('agent-question').focus();
    }
    const run = e.target.closest('[data-agent-run]');
    if (run && !agentBusy) renderAgent(await api('/agent/runs/' + encodeURIComponent(run.dataset.agentRun)));
}));
