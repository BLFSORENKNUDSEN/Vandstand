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
const highWaterAlert = document.getElementById("highWaterAlert");
const highWaterAlertText = document.getElementById("highWaterAlertText");

let metadata = null;
let currentLayer = null;
let preparedLayer = null;
let preparedIndex = null;
let frameIndex = 0;
let playTimer = null;
let transitionToken = 0;

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

function updateHighWaterAlert() {
  if (!highWaterAlert || !highWaterAlertText || !metadata) return;

  const local = Array.isArray(metadata.localForecasts)
    ? metadata.localForecasts.find(item => item.id === "sortsoe_groensund")
    : null;

  const alert = local && local.alert ? local.alert : metadata.highWaterAlert;

  if (!alert || !alert.active || alert.peakCm == null || !alert.peakTime) {
    highWaterAlert.hidden = true;
    return;
  }

  const peak = Math.round(Number(alert.peakCm));
  const placeName = local && local.name
    ? local.name
    : "de danske farvande";

  highWaterAlertText.textContent =
    `${placeName}: Prognosen viser op til ${peak} cm over daglig vande. Vandstanden ventes at toppe ${formatTime(alert.peakTime)}.`;
  highWaterAlert.hidden = false;
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
    `${formatTime(frame.time)} · ${index + 1} af ${metadata.frames.length} · DMI DKSS kombineret`;
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
