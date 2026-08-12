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

let metadata = null;
let tileLayer = null;
let frameIndex = 0;
let timer = null;

function formatTime(value) {
  const date = new Date(value);
  return new Intl.DateTimeFormat("da-DK", {
    weekday: "long",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Europe/Copenhagen",
  }).format(date);
}

function rgbaToCss(rgba) {
  return `rgba(${rgba[0]}, ${rgba[1]}, ${rgba[2]}, ${rgba[3] / 255})`;
}

function buildLegend(stops) {
  if (!Array.isArray(stops) || stops.length < 2) return;

  const ordered = [...stops].sort((a, b) => b.cm - a.cm);
  const min = Math.min(...ordered.map(stop => stop.cm));
  const max = Math.max(...ordered.map(stop => stop.cm));

  const gradient = ordered.map(stop => {
    const pct = ((max - stop.cm) / (max - min)) * 100;
    return `${rgbaToCss(stop.rgba)} ${pct}%`;
  }).join(", ");

  const labelStops = ordered.filter(
    (stop, index) => index % 2 === 0 || index === ordered.length - 1
  );

  legend.innerHTML = `
    <div class="legend-scale">
      <div class="legend-bar" style="background: linear-gradient(to bottom, ${gradient})"></div>
      <div class="legend-labels">
        ${labelStops.map(stop => `<span>${stop.cm}</span>`).join("")}
      </div>
    </div>
    <div class="legend-unit">cm</div>
  `;
}

function frameTileUrl(frame) {
  const version = encodeURIComponent(metadata.generated || "1");
  return `${DATA_BASE}/${frame.tileTemplate}?v=${version}`;
}

function removeTileLayer() {
  if (tileLayer) {
    map.removeLayer(tileLayer);
    tileLayer = null;
  }
}

function showFrame(index) {
  if (!metadata || !Array.isArray(metadata.frames) || metadata.frames.length === 0) {
    return;
  }

  frameIndex = Math.max(0, Math.min(index, metadata.frames.length - 1));
  slider.value = String(frameIndex);

  const frame = metadata.frames[frameIndex];
  removeTileLayer();

  tileLayer = L.tileLayer(frameTileUrl(frame), {
    tileSize: metadata.tileSize || 256,
    minNativeZoom: metadata.nativeZoom,
    maxNativeZoom: metadata.nativeZoom,
    minZoom: 5,
    maxZoom: 12,
    opacity: 0.92,
    noWrap: true,
    bounds: metadata.bounds,
    attribution: "DMI DKSS",
    crossOrigin: true,
  }).addTo(map);

  forecastTime.textContent =
    `${formatTime(frame.time)} · ${frameIndex + 1} af ${metadata.frames.length} · kun dkss_idw XYZ`;

  preloadFrame(frameIndex + 1);
}

function preloadFrame(index) {
  if (!metadata || index < 0 || index >= metadata.frames.length) return;

  const frame = metadata.frames[index];
  const zoom = metadata.nativeZoom;
  const bounds = map.getBounds();

  const northWest = map.project(bounds.getNorthWest(), zoom).divideBy(metadata.tileSize || 256).floor();
  const southEast = map.project(bounds.getSouthEast(), zoom).divideBy(metadata.tileSize || 256).floor();
  const version = encodeURIComponent(metadata.generated || "1");

  for (let x = northWest.x; x <= southEast.x; x += 1) {
    for (let y = northWest.y; y <= southEast.y; y += 1) {
      const image = new Image();
      image.src = `${DATA_BASE}/${frame.directory}/${zoom}/${x}/${y}.webp?v=${version}`;
    }
  }
}

function stopPlayback() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  playButton.textContent = "▶";
  playButton.setAttribute("aria-label", "Afspil prognose");
}

function startPlayback() {
  if (!metadata || timer) return;

  playButton.textContent = "❚❚";
  playButton.setAttribute("aria-label", "Stop prognose");

  timer = setInterval(() => {
    const next = frameIndex + 1;
    showFrame(next >= metadata.frames.length ? 0 : next);
  }, 700);
}

playButton.addEventListener("click", () => {
  if (timer) stopPlayback();
  else startPlayback();
});

slider.addEventListener("input", event => {
  stopPlayback();
  showFrame(Number(event.target.value));
});

async function init() {
  forecastTime.textContent = "Henter IDW XYZ prognose…";

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

  buildLegend(metadata.colorStops || []);

  if (Array.isArray(metadata.bounds) && metadata.bounds.length === 2) {
    map.fitBounds(metadata.bounds, { padding: [10, 10] });
  }

  showFrame(0);
}

init().catch(error => {
  console.error(error);
  forecastTime.textContent = `IDW tiledata kunne ikke indlæses: ${error.message}`;
});
