const DATA_BASE = "https://raw.githubusercontent.com/BLFSORENKNUDSEN/Vandstand/main/data/waterlevel-map";
const METADATA_URL = `${DATA_BASE}/metadata.json`;

const map = L.map("waterlevelMap", {
  zoomControl: true,
  attributionControl: true,
}).setView([56.0, 11.2], 6);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 12,
  attribution: "&copy; OpenStreetMap bidragydere",
}).addTo(map);

const slider = document.getElementById("frameSlider");
const playButton = document.getElementById("playButton");
const forecastTime = document.getElementById("forecastTime");
const legend = document.getElementById("legend");

let metadata = null;
let overlays = [];
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
  const ordered = [...stops].sort((a, b) => b.cm - a.cm);
  const min = Math.min(...ordered.map(stop => stop.cm));
  const max = Math.max(...ordered.map(stop => stop.cm));
  const gradient = ordered.map(stop => {
    const pct = ((max - stop.cm) / (max - min)) * 100;
    return `${rgbaToCss(stop.rgba)} ${pct}%`;
  }).join(", ");

  const labelStops = ordered.filter((stop, index) => index % 2 === 0 || index === ordered.length - 1);
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

function clearOverlays() {
  overlays.forEach(layer => map.removeLayer(layer));
  overlays = [];
}

function showFrame(index) {
  if (!metadata || !metadata.frames.length) return;
  frameIndex = Math.max(0, Math.min(index, metadata.frames.length - 1));
  slider.value = String(frameIndex);

  const frame = metadata.frames[frameIndex];
  clearOverlays();

  frame.layers.forEach(layer => {
    const url = `${DATA_BASE}/${layer.image}`;
    const overlay = L.imageOverlay(url, layer.bounds, {
      opacity: layer.collection === "dkss_idw" ? 0.93 : 0.82,
      interactive: false,
      crossOrigin: true,
    }).addTo(map);
    overlays.push(overlay);
  });

  forecastTime.textContent = `${formatTime(frame.time)} · prognosetrin ${frameIndex + 1} af ${metadata.frames.length}`;
  preloadFrame(frameIndex + 1);
}

function preloadFrame(index) {
  if (!metadata || index >= metadata.frames.length) return;
  metadata.frames[index].layers.forEach(layer => {
    const image = new Image();
    image.src = `${DATA_BASE}/${layer.image}`;
  });
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
  if (!metadata) return;
  playButton.textContent = "❚❚";
  playButton.setAttribute("aria-label", "Stop prognose");
  timer = setInterval(() => {
    const next = frameIndex + 1;
    if (next >= metadata.frames.length) {
      showFrame(0);
    } else {
      showFrame(next);
    }
  }, 650);
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
  const response = await fetch(`${METADATA_URL}?v=${Date.now()}`, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Kunne ikke hente metadata: HTTP ${response.status}`);
  }

  metadata = await response.json();
  if (!Array.isArray(metadata.frames) || metadata.frames.length === 0) {
    throw new Error("Metadata indeholder ingen kortframes");
  }

  slider.min = "0";
  slider.max = String(metadata.frames.length - 1);
  slider.value = "0";
  buildLegend(metadata.colorStops || []);
  showFrame(0);
}

init().catch(error => {
  console.error(error);
  forecastTime.textContent = `Kortdata kunne ikke indlæses: ${error.message}`;
});
