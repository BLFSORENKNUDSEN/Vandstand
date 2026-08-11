const DATA_URL = "https://raw.githubusercontent.com/BLFSORENKNUDSEN/Vandstand/main/data/waterlevel.json";
const OFFSETS = [0, 3, 6, 12, 24];

const map = L.map("map", { zoomControl: true }).setView([54.93, 12.0], 10);

L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 18,
  attribution: "&copy; OpenStreetMap bidragydere"
}).addTo(map);

const statusEl = document.getElementById("status");
const timeButtonsEl = document.getElementById("timeButtons");
const markers = new Map();
let payload = null;
let selectedOffset = 0;

function formatDateTime(value) {
  if (!value) return "Ukendt";
  return new Intl.DateTimeFormat("da-DK", {
    dateStyle: "short",
    timeStyle: "short",
    timeZone: "Europe/Copenhagen"
  }).format(new Date(value));
}

function labelForOffset(hours) {
  return hours === 0 ? "Nu" : `+${hours} t`;
}

function closestForecast(forecast, target) {
  if (!forecast || !forecast.length) return null;
  return forecast.reduce((best, item) => {
    const delta = Math.abs(new Date(item.time).getTime() - target.getTime());
    if (!best || delta < best.delta) return { item, delta };
    return best;
  }, null).item;
}

function trendForForecast(forecast, selected) {
  if (!forecast || forecast.length < 2 || !selected) return "→";
  const index = forecast.findIndex(item => item.time === selected.time);
  const previous = index > 0 ? forecast[index - 1] : forecast[index + 1];
  if (!previous) return "→";
  const diff = selected.levelCm - previous.levelCm;
  if (diff >= 2) return "↑";
  if (diff <= -2) return "↓";
  return "→";
}

function markerIcon(location, selected) {
  const unavailable = !location.available || !selected;
  const text = unavailable ? "Ingen data" : `${selected.levelCm} cm ${trendForForecast(location.forecast, selected)}`;
  return L.divIcon({
    className: "water-marker",
    html: `<div class="water-badge${unavailable ? " unavailable" : ""}">${text}</div>`,
    iconSize: null,
    iconAnchor: [28, 18]
  });
}

function popupHtml(location, selected) {
  if (!location.available || !selected) {
    return `<div class="popup"><h3>${location.name}</h3><p>Ingen brugbar vandstandsprognose.</p><p>${location.reason || ""}</p></div>`;
  }

  const rows = location.forecast.map(item => (
    `<tr><td>${formatDateTime(item.time)}</td><td>${item.levelCm} cm</td></tr>`
  )).join("");

  return `
    <div class="popup">
      <h3>${location.name}</h3>
      <p><strong>${selected.levelCm} cm</strong> ${trendForForecast(location.forecast, selected)}</p>
      <p>${formatDateTime(selected.time)}</p>
      <p>Model: ${location.collection || "ukendt"}</p>
      <p>Modelpunkt: ${location.modelPoint?.distanceKm ?? "?"} km væk</p>
      <table>${rows}</table>
    </div>`;
}

function renderButtons() {
  timeButtonsEl.innerHTML = "";
  for (const offset of OFFSETS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = labelForOffset(offset);
    button.classList.toggle("active", offset === selectedOffset);
    button.addEventListener("click", () => {
      selectedOffset = offset;
      renderButtons();
      renderLocations();
    });
    timeButtonsEl.appendChild(button);
  }
}

function renderLocations() {
  if (!payload) return;
  const target = new Date(Date.now() + selectedOffset * 60 * 60 * 1000);
  const bounds = [];

  for (const location of payload.locations || []) {
    const selected = closestForecast(location.forecast, target);
    let marker = markers.get(location.id);

    if (!marker) {
      marker = L.marker([location.lat, location.lon], {
        icon: markerIcon(location, selected)
      }).addTo(map);
      markers.set(location.id, marker);
      bounds.push([location.lat, location.lon]);
    } else {
      marker.setLatLng([location.lat, location.lon]);
      marker.setIcon(markerIcon(location, selected));
    }

    marker.bindPopup(popupHtml(location, selected), { maxWidth: 340 });
  }

  if (bounds.length) map.fitBounds(bounds, { padding: [40, 40] });
}

async function loadData() {
  try {
    statusEl.textContent = "Henter prognose…";
    const response = await fetch(`${DATA_URL}?v=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    payload = await response.json();
    statusEl.textContent = `Opdateret ${formatDateTime(payload.generated)}`;
    renderButtons();
    renderLocations();
  } catch (error) {
    console.error(error);
    statusEl.textContent = "Kunne ikke hente vandstandsdata";
  }
}

loadData();
setInterval(loadData, 15 * 60 * 1000);
