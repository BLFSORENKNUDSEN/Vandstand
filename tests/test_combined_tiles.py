import importlib.util
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).parents[1] / 'scripts' / 'generate_combined_tiles.py'
spec = importlib.util.spec_from_file_location('combined_tiles', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_peak_excludes_land_and_missing_values_and_keeps_source():
    values = np.array([[9., np.nan], [1.7274, 1.6]])
    water = np.array([[False, True], [True, True]])
    lats = np.array([[54., 54.], [53.87, 53.87]])
    lons = np.array([[9., 9.1], [9.26, 9.3]])
    idw_valid = np.array([[True, False], [False, True]])
    peak = module.tile_peak(values, water, lats, lons, idw_valid)
    assert np.isclose(peak['valueCm'], 172.74)
    assert (peak['lat'], peak['lon']) == (53.87, 9.26)
    assert peak['source'] == 'dkss_nsbs'
    assert module.tile_peak(values, np.zeros_like(water), lats, lons, idw_valid) is None


def test_boundary_pixels_cannot_become_invisible_maxima():
    lats = np.array([[55., 55., 55.]])
    lons = np.array([[9.238, 9.34, 10.]])
    inside = module.within_bounds(lats, lons, [[54., 9.34], [56., 10.]])
    assert inside.tolist() == [[False, True, True]]
    peak = module.tile_peak(np.array([[9., 0.4, 0.3]]), inside, lats, lons, inside)
    assert peak['valueCm'] == 40.
    assert peak['lon'] == 9.34


def test_map_extent_includes_west_coast_bornholm_and_elbe_context():
    # Esbjerg, Thyborøn, Skagen, Bornholm, Elbe. No national boundary implied.
    lats = np.array([55.47, 56.70, 57.72, 55.10, 53.87])
    lons = np.array([8.42, 8.21, 10.58, 14.70, 9.26])
    assert module.within_bounds(lats, lons, module.MAP_BOUNDS).all()


def test_idw_priority_and_nsbs_fallback_preserve_peak_provenance():
    geom = {'mode': 'bilinear', 'latAxis': np.array([54., 55.]), 'lonAxis': np.array([11., 12.])}
    sampler = module.build_sampler_for_tile(geom, np.array([[54.5]]), np.array([[11.5]]))
    idw = module.sample_model(np.full((2, 2), .476), geom, sampler)
    nsbs = module.sample_model(np.full((2, 2), 1.7), geom, sampler)
    idw_valid = np.isfinite(idw)
    combined = np.where(idw_valid, idw, nsbs)
    assert np.isclose(combined[0, 0], .476)
    missing = module.sample_model(np.full((2, 2), np.nan), geom, sampler)
    assert not np.isfinite(missing[0, 0])


def test_generation_keeps_local_and_area_peaks_independent(tmp_path, monkeypatch):
    import json
    import sys
    from datetime import datetime, timezone
    import mercantile

    moment = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    item = {'properties': {'datetime': module.base.iso_z(moment)}}
    monkeypatch.setattr(module.base, 'fetch_stac_items', lambda *args: [item])
    monkeypatch.setattr(module.base, 'choose_run', lambda *args: ('2026-10-21T00:00:00Z', [item]))
    monkeypatch.setattr(module.base, 'select_steps', lambda *args: [item])
    monkeypatch.setattr(module.base, 'asset_href', lambda *args: 'unused')
    monkeypatch.setattr(module.base, 'download_file', lambda *args: None)
    lat, lon = np.meshgrid([54.9, 55.0], [11.9, 12.0], indexing='ij')
    def grid(path):
        values = np.full((2, 2), .476 if path.name.startswith('idw') else 1.727)
        # NSBS has a wider domain and supplies the second target column.
        return lat, lon if path.name.startswith('idw') else lon + .2, values, {}
    monkeypatch.setattr(module, 'read_model_grid', grid)
    monkeypatch.setattr(module, 'MAP_BOUNDS', [[54.9, 11.9], [55., 12.3]])
    monkeypatch.setattr(mercantile, 'tiles', lambda *args, **kwargs: [mercantile.Tile(0, 0, 1)])
    monkeypatch.setattr(module, 'tile_pixel_lonlat', lambda tile: (
        np.full((2, 2), 54.95), np.array([[11.95, 12.25], [11.95, 12.25]])))
    monkeypatch.setattr(module, 'fetch_water_mask', lambda *args: np.full((2, 2), 255))
    output = tmp_path / 'tiles'
    monkeypatch.setattr(sys, 'argv', ['generate', '--output-dir', str(output), '--zoom', '1'])
    assert module.main() == 0
    result = json.loads((output / 'metadata.json').read_text())
    assert result['highWaterAlert']['active'] is True
    assert result['highWaterAlert']['peakCm'] == 172.7
    assert result['highWaterAlert']['peakLocation']['source'] == 'dkss_nsbs'
    assert result['highWaterAlert']['peakLocation']['lon'] == 12.25
    assert result['localForecasts'][0]['alert']['active'] is False
    assert result['localForecasts'][0]['alert']['peakCm'] == 47.6
    assert list(output.rglob('*.webp'))
