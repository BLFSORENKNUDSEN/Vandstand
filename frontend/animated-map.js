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