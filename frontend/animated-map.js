const DATA_BASE = "https://raw.githubusercontent.com/BLFSORENKNUDSEN/Vandstand/main/data/waterlevel-tiles";
const METADATA_URL = `${DATA_BASE}/metadata.json`;

const map = L.map("waterlevelMap", {
  zoomControl: true,
  attributionControl: true,
  minZoom: 5,
  maxZoom: 12,
}).setView([55.25, 11.75], 7);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 12,
  attribution: "&copy; OpenStreetMap bidragydere",
}).addTo(map);

const slider = document.getElementById("frameSlider");
const playButton = document.getElementById("playButton");
const forecastTime = document.getElementById("forecastTime");
const legend = document.getElementById("legend");
// Also works in an existing Strandvejr template without the new summary markup.
let summary = document.getElementById("forecastSummary");
if (!summary) {
  summary = document.createElement("section");
  summary.id = "forecastSummary";
  summary.className = "forecast-summary";
  document.querySelector(".map-wrap").before(summary);
}
const legacyAlert = document.getElementById("highWaterAlert");
if (legacyAlert) legacyAlert.hidden = true;
let peakMarker = null;

let metadata = null;
let currentLayer = null;
let preparedLayer = null;
let preparedIndex = null;
let frameIndex = 0;
let playTimer = null;
let transitionToken = 0;

function formatTime(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Tidspunkt ukendt";
  return new Intl.DateTimeFormat("da-DK", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit", timeZone: "Europe/Copenhagen",
  }).format(new Date(value)) + " dansk tid";
}

function formatCm(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("da-DK", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + " cm"
    : "Ingen data";
}

function rgbaToCss(rgba) {
  return `rgba(${rgba[0]}, ${rgba[1]}, ${rgba[2]}, ${rgba[3] / 255})`;
}

function addText(parent, tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  parent.append(element);
  return element;
}

function getLocalForecast() {
  return metadata.localForecasts?.find(item => item.id === "sortsoe_groensund");
}

function addForecastCard(title, indicator) {
  const card = document.createElement("article");
  card.className = "forecast-card";
  addText(card, "h2", title);
  const valid = typeof indicator?.peakCm === "number" && Number.isFinite(indicator.peakCm);
  addText(card, "p", valid
    ? `Maksimum i prognoseperioden: ${formatCm(indicator.peakCm)} i forhold til middelvandstand.`
    : "Prognose mangler for dette område.", "forecast-peak");
  if (valid) addText(card, "p", `Prognosetidspunkt for maksimum: ${formatTime(indicator.peakTime)}`);
  if (valid && typeof indicator.thresholdCm === "number" && Number.isFinite(indicator.thresholdCm)) {
    const exceeded = indicator.peakCm > indicator.thresholdCm;
    addText(card, "p", `Sidens modelgrænse på ${formatCm(indicator.thresholdCm)} er ${exceeded ? "overskredet" : "ikke overskredet"}.`,
      exceeded ? "model-threshold exceeded" : "model-threshold");
  }
  summary.append(card);
  return card;
}

function updateHighWaterAlert() {
  if (!metadata) return;
  summary.replaceChildren();
  const model = metadata.highWaterAlert;
  const local = getLocalForecast();
  const modelCard = addForecastCard("Samlet kortområde", model);
  addText(modelCard, "p", "Omfatter også udenlandske farvande. Maksimum er ikke en prognose for alle danske kyster. Lokal kystpåvirkning er ikke vurderet.");
  // Legacy data contain no coordinates. Never infer a coastal warning from them.
  const location = model?.peakLocation;
  if (location && Number.isFinite(location.lat) && Number.isFinite(location.lon)) {
    addText(modelCard, "p", `Position: ${location.lat.toFixed(4)}° N, ${location.lon.toFixed(4)}° Ø · ${location.source || "DMI DKSS"}`);
    const button = addText(modelCard, "button", "Vis maksimum på kortet");
    button.type = "button";
    button.addEventListener("click", async () => {
      stopPlayback();
      const index = metadata.frames.findIndex(frame => frame.time === model.peakTime);
      if (index >= 0) await transitionTo(index, false);
      map.setView([location.lat, location.lon], 9);
      peakMarker?.openPopup();
    });
    if (peakMarker) map.removeLayer(peakMarker);
    const popup = document.createElement("div");
    popup.textContent = `Samlet modelmaksimum: ${formatCm(model.peakCm)} · ${formatTime(model.peakTime)}. Ikke et myndighedsvarsel.`;
    peakMarker = L.circleMarker([location.lat, location.lon], { radius: 8, color: "#7d304b", fillOpacity: 0.8 })
      .addTo(map).bindPopup(popup);
  } else {
    addText(modelCard, "p", "Position for maksimum findes ikke i dette datasæt. Kystområde kan derfor ikke fastslås ud fra metadata.");
  }
  const localCard = addForecastCard("Grønsund ved Sortsø Strand", local?.alert);
  addText(localCard, "p", "Lokal modelprognose for punktet ved Sortsø Strand. Kan afvige fra forholdene helt inde ved kysten.");
  if (local && Number.isFinite(local.lat) && Number.isFinite(local.lon)) {
    const popup = document.createElement("div");
    popup.textContent = `Grønsund ved Sortsø Strand · lokalt modelmaksimum ${formatCm(local.alert?.peakCm)} · ${formatTime(local.alert?.peakTime)}`;
    L.circleMarker([local.lat, local.lon], { radius: 6, color: "#006c99", fillOpacity: 0.8 }).addTo(map).bindPopup(popup);
  }
  const notes = document.createElement("div");
  notes.className = "forecast-notes";
  addText(notes, "p", `Datastempel: ${formatTime(metadata.generated)}`);
  for (const [name, time] of Object.entries(metadata.modelRuns || {})) {
    addText(notes, "p", `Modelkørsel ${name}: ${formatTime(time)}`);
  }
  const first = metadata.frames[0];
  const last = metadata.frames[metadata.frames.length - 1];
  addText(notes, "p", `Prognoseperiode: ${formatTime(first.time)} til ${formatTime(last.time)}.`);
  const generated = Date.parse(metadata.generated);
  if (!Number.isFinite(generated) || Date.now() - generated > 12 * 3600000) {
    addText(notes, "p", "Datastemplet er ukendt eller mere end 12 timer gammelt. Prognosen kan være forældet.", "data-age");
  }
  if (Date.parse(last.time) < Date.now()) {
    addText(notes, "p", "Hele prognoseperioden er udløbet.", "data-age");
  }
  addText(notes, "p", "Kilde: DMI DKSS modeldata. Automatisk modelberegning, ikke et officielt myndighedsvarsel. Grænsen på siden er ikke en officiel varslingsgrænse.");
  const link = addText(notes, "a", "Se officielle varsler hos DMI");
  link.href = "https://www.dmi.dk/varsler";
  summary.append(notes);
}

function buildLegend(stops) {
  if (!Array.isArray(stops) || stops.length === 0) return;

  const ordered = [...stops].sort((a, b) => b.cm - a.cm);
  const under = metadata && metadata.underColor
    ? metadata.underColor.rgba
    : [71, 96, 160, 255];

  const rows = ordered.map(stop => {
    return `<div style="width:30px;height:20px;display:flex;align-items:center;justify-content:center;background:${rgbaToCss(stop.rgba)};color:#fff;font-size:11px;font-weight:700;line-height:1;text-shadow:0 1px 2px rgba(0,0,0,.75)">${stop.cm}</div>`;
  }).join("");

  legend.innerHTML = `
    <div style="width:30px">
      ${rows}
      <div style="width:30px;height:20px;background:${rgbaToCss(under)}"></div>
    </div>
    <div class="legend-unit">cm</div>
  `;
}

function frameTileUrl(frame) {
  const version = encodeURIComponent(metadata.generated || "1");
  return `${DATA_BASE}/${frame.tileTemplate}?v=${version}`;
}

function createFrameLayer(index, opacity) {
  const frame = metadata.frames[index];
  return L.tileLayer(frameTileUrl(frame), {
    tileSize: metadata.tileSize || 256,
    minNativeZoom: metadata.nativeZoom,
    maxNativeZoom: metadata.nativeZoom,
    minZoom: 5,
    maxZoom: 12,
    opacity,
    noWrap: true,
    bounds: metadata.bounds,
    attribution: "DMI DKSS",
    crossOrigin: true,
    updateWhenIdle: false,
    updateWhenZooming: false,
    keepBuffer: 4,
  });
}

function updateText(index) {
  const frame = metadata.frames[index];
  forecastTime.textContent =
    `Kortets prognosetidspunkt: ${formatTime(frame.time)} · ${index + 1} af ${metadata.frames.length}`;
  const localValue = getLocalForecast()?.series?.find(item => item.time === frame.time)?.waterLevelCm;
  const current = document.getElementById("currentValues");
  if (current) current.textContent = `På valgt tidspunkt: samlet kortmaksimum ${formatCm(frame.maxWaterLevelCm)} · Sortsø Strand ${formatCm(localValue)} i forhold til middelvandstand.`;
}

function clearPrepared() {
  if (preparedLayer) {
    map.removeLayer(preparedLayer);
  }
  preparedLayer = null;
  preparedIndex = null;
}

function prepareFrame(index) {
  if (!metadata || index < 0 || index >= metadata.frames.length) return;
  if (preparedIndex === index && preparedLayer) return;

  clearPrepared();

  preparedIndex = index;
  preparedLayer = createFrameLayer(index, 0);
  preparedLayer.addTo(map);
}

function transitionTo(index, smooth = true) {
  if (!metadata || metadata.frames.length === 0) return Promise.resolve();

  index = Math.max(0, Math.min(index, metadata.frames.length - 1));
  const token = ++transitionToken;

  let incoming;
  if (preparedLayer && preparedIndex === index) {
    incoming = preparedLayer;
    preparedLayer = null;
    preparedIndex = null;
  } else {
    clearPrepared();
    incoming = createFrameLayer(index, 0);
    incoming.addTo(map);
  }

  return new Promise(resolve => {
    let done = false;

    const finish = () => {
      if (done || token !== transitionToken) return;
      done = true;

      const outgoing = currentLayer;
      currentLayer = incoming;
      frameIndex = index;
      slider.value = String(index);
      updateText(index);

      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          incoming.setOpacity(0.92);
          if (outgoing && outgoing !== incoming) outgoing.setOpacity(0);

          window.setTimeout(() => {
            if (outgoing && map.hasLayer(outgoing)) map.removeLayer(outgoing);
            const next = frameIndex + 1 < metadata.frames.length ? frameIndex + 1 : 0;
            prepareFrame(next);
            resolve();
          }, smooth ? 280 : 20);
        });
      });
    };

    incoming.once("load", finish);
    window.setTimeout(finish, 1800);
  });
}

function stopPlayback() {
  if (playTimer) {
    clearTimeout(playTimer);
    playTimer = null;
  }
  playButton.textContent = "▶";
  playButton.setAttribute("aria-label", "Afspil prognose");
}

async function playNext() {
  if (!playTimer || !metadata) return;

  const next = frameIndex + 1 >= metadata.frames.length ? 0 : frameIndex + 1;
  await transitionTo(next, true);

  if (playTimer) {
    playTimer = window.setTimeout(playNext, 520);
  }
}

function startPlayback() {
  if (!metadata || playTimer) return;

  playButton.textContent = "❚❚";
  playButton.setAttribute("aria-label", "Stop prognose");
  playTimer = window.setTimeout(playNext, 120);
}

playButton.addEventListener("click", () => {
  if (playTimer) stopPlayback();
  else startPlayback();
});

slider.addEventListener("input", async event => {
  stopPlayback();
  await transitionTo(Number(event.target.value), true);
});

async function init() {
  forecastTime.textContent = "Henter DMI DKSS prognose…";

  const response = await fetch(`${METADATA_URL}?v=${Date.now()}`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`Kunne ikke hente metadata: HTTP ${response.status}`);
  }

  metadata = await response.json();

  if (!Array.isArray(metadata.frames) || metadata.frames.length === 0) {
    throw new Error("Metadata indeholder ingen IDW tileframes");
  }

  slider.min = "0";
  slider.max = String(metadata.frames.length - 1);
  slider.value = "0";

  const labels = document.querySelectorAll(".timeline-labels span");
  if (labels.length === 3) {
    const middle = Math.floor((metadata.frames.length - 1) / 2);
    [0, middle, metadata.frames.length - 1].forEach((index, i) => {
      labels[i].textContent = formatTime(metadata.frames[index].time);
    });
  }
  buildLegend(metadata.colorStops || []);
  updateHighWaterAlert();

  if (Array.isArray(metadata.bounds) && metadata.bounds.length === 2) {
    map.fitBounds(metadata.bounds, { padding: [10, 10] });
  }

  currentLayer = createFrameLayer(0, 0.92).addTo(map);
  frameIndex = 0;
  updateText(0);
  prepareFrame(1);
}

init().catch(error => {
  console.error(error);
  forecastTime.textContent = `Vandstandsdata kunne ikke indlæses: ${error.message}`;
});
