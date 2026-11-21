const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Minimal DOM and Leaflet doubles: exercise the real initialization and text rendering.
class Element {
  constructor() { this.children = []; this.textContent = ''; this.listeners = {}; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners[name] = callback; }
  get text() { return [this.textContent, ...this.children.map(child => child.text)].join(' '); }
}
const source = fs.readFileSync(path.join(__dirname, '../frontend/animated-map.js'), 'utf8');
const baseline = JSON.parse(fs.readFileSync(path.join(__dirname, '../data/waterlevel-tiles/metadata.json')));
async function render(change = () => {}) {
  const data = structuredClone(baseline);
  // Keep regression values independent of the changing automatic data files.
  data.generated = new Date().toISOString();
  data.highWaterAlert = { peakCm: 172.7, peakTime: '2026-10-22T10:00:00Z', thresholdCm: 75, active: true };
  data.localForecasts = [{ id: 'sortsoe_groensund', series: [], alert: {
    peakCm: 47.6, peakTime: '2026-10-21T23:00:00Z', thresholdCm: 75, active: false,
  } }];
  change(data);
  const elements = Object.fromEntries(['frameSlider', 'playButton', 'forecastTime', 'legend',
    'forecastSummary', 'currentValues', 'highWaterAlert'].map(id => [id, new Element()]));
  const labels = [new Element(), new Element(), new Element()];
  const markers = [];
  const map = { setView() { return this; }, fitBounds() {}, removeLayer() {} };
  const context = vm.createContext({ console, Intl, Date, Number, Math, Array, Object,
    document: { getElementById: id => elements[id] || null, createElement: () => new Element(),
      querySelectorAll: () => labels },
    L: { map: () => map, tileLayer: () => ({ addTo() { return this; } }),
      circleMarker(coords) { const marker = { coords, addTo() { return this; }, bindPopup() { return this; } }; markers.push(marker); return marker; } },
    fetch: async () => ({ ok: true, json: async () => data }),
  });
  vm.runInContext(source, context);
  await new Promise(resolve => setImmediate(resolve));
  return { elements, labels, markers, context };
}

test('inactive local indicator cannot hide the area maximum', async () => {
  const { elements, labels } = await render();
  const text = elements.forecastSummary.text;
  assert.match(text, /172,7 cm/);
  assert.match(text, /47,6 cm/);
  assert.match(text, /er overskredet/);
  assert.match(text, /ikke overskredet/);
  assert.match(text, /Position for maksimum findes ikke/);
  assert.match(text, /ikke et officielt myndighedsvarsel/);
  assert.match(text, /Datastempel/);
  assert.match(text, /Modelkørsel/);
  assert.match(text, /22.10.2026.*12.00/);
  assert.equal(elements.highWaterAlert.hidden, true);
  assert.doesNotMatch(labels[2].text, /48 timer/);
});

test('missing local forecast never inherits the area maximum', async () => {
  const { elements } = await render(data => { data.localForecasts = []; });
  const cards = elements.forecastSummary.children;
  assert.match(cards[0].text, /172,7/);
  assert.match(cards[1].text, /Prognose mangler/);
  assert.doesNotMatch(cards[1].text, /172,7/);
});

test('coordinates create an independently labelled maximum marker', async () => {
  const { markers, elements } = await render(data => {
    data.highWaterAlert.peakLocation = { lat: 53.87, lon: 9.26, source: 'dkss_nsbs' };
  });
  assert.equal(markers.length, 1);
  assert.deepEqual(Array.from(markers[0].coords), [53.87, 9.26]);
  assert.match(elements.forecastSummary.text, /dkss_nsbs/);
  assert.match(elements.forecastSummary.text, /Vis maksimum på kortet/);
});

test('missing values and stale data cannot look like a zero forecast', async () => {
  const { elements } = await render(data => {
    data.highWaterAlert.peakCm = null;
    data.generated = '2000-01-01T00:00:00Z';
    data.frames = [{ time: '2000-01-01T01:00:00Z', maxWaterLevelCm: null }];
    data.localForecasts = [];
  });
  assert.match(elements.forecastSummary.text, /Prognose mangler/);
  assert.match(elements.forecastSummary.text, /mere end 12 timer/);
  assert.match(elements.forecastSummary.text, /prognoseperioden er udløbet/);
  assert.match(elements.currentValues.text, /Ingen data/);
});
