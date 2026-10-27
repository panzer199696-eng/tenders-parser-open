// Реальный app.js с подменёнными DOM/HTTP: никаких браузеров и сети.
// node --test tests/test_safe_run_ui.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function page() {
  const elements = new Map();
  function element() {
    const classes = new Set(['hidden']);
    return {
      textContent: '', innerHTML: '', style: {}, disabled: false, listeners: {},
      classList: {
        add: (x) => classes.add(x), remove: (x) => classes.delete(x),
        contains: (x) => classes.has(x), toggle: (x) => classes.has(x) ? classes.delete(x) : classes.add(x),
      },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      appendChild() {}, remove() {}, querySelectorAll() { return []; },
    };
  }
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const intervals = [];
  const ctx = vm.createContext({
    document: { getElementById: get, querySelectorAll: () => [],
      addEventListener() {}, createElement: element },
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval() {}, setTimeout() {},
    fetch: async () => ({ ok: true, headers: { get: () => 'application/json' }, json: async () => ({}) }),
    console,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../src/ui/static/app.js'), 'utf8'), ctx);
  await new Promise(setImmediate); // завершить начальное чтение пустых заглушек
  return { ctx, get, intervals };
}

test('ошибка Excel показывает повторное сохранение без повторного сбора', async () => {
  const { ctx, get } = await page();
  const requests = [];
  ctx.api = async (url) => {
    requests.push(url);
    if (url === '/api/excel/save') return { ok: true, path: 'test/report.xlsx' };
    return { running: false, error: 'Лоты сохранены, файл не создан',
      data_saved: true, tenders_count: 7, progress: [], session: {} };
  };
  await ctx.pollStatus();
  assert.equal(get('result-card').classList.contains('hidden'), false);
  assert.equal(get('result-count').textContent, 7);
  assert.equal(get('result-path').textContent, 'файл не создан');
  await get('excel-save').listeners.click();
  assert.equal(get('result-path').textContent, 'test/report.xlsx');
  assert.deepEqual(requests, ['/api/status', '/api/excel/save']);
});

test('ошибка основной записи не предлагает старый отчёт как новый', async () => {
  const { ctx, get } = await page();
  ctx.api = async () => ({ running: false, error: 'Лоты не сохранены',
    data_saved: false, tenders_count: 7, progress: [], session: {} });
  await ctx.pollStatus();
  assert.equal(get('result-card').classList.contains('hidden'), true);
});

test('открытое окно подхватывает автоматический сбор и показывает остановку', async () => {
  const { ctx, get, intervals } = await page();
  ctx.api = async () => ({ running: true, counters: {}, session: {} });
  await ctx.refreshCounters();
  assert.equal(get('btn-parse').disabled, true);
  assert.equal(get('btn-stop').classList.contains('hidden'), false);
  assert.equal(get('btn-stop').disabled, false);
  assert.equal(intervals.length, 1);
  await ctx.refreshCounters();
  assert.equal(intervals.length, 1);
});
