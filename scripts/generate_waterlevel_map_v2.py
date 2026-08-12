#!/usr/bin/env python3
"""Strandvejr water level map generator with strict regular_ll grid decoding.

This wrapper reuses generate_waterlevel_map.py but replaces its GRIB grid reader
for regular latitude/longitude fields. Values are laid out from the GRIB scanning
metadata instead of relying on latLonValues triplets.
"""

import importlib.util
from pathlib import Path
from typing import Tuple

import numpy as np
from eccodes import codes_get, codes_get_array, codes_grib_new_from_file, codes_release

BASE_PATH = Path(__file__).with_name("generate_waterlevel_map.py")
spec = importlib.util.spec_from_file_location("strandvejr_waterlevel_base", BASE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Kunne ikke indlæse {BASE_PATH}")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def get_int(gid: int, key: str, default: int = 0) -> int:
    try:
        return int(codes_get(gid, key))
    except Exception:
        return default


def read_regular_ll(gid: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray, int, bool]:
    ni = int(codes_get(gid, "Ni"))
    nj = int(codes_get(gid, "Nj"))
    expected = ni * nj

    values = np.asarray(codes_get_array(gid, "values"), dtype=np.float64)
    if values.size != expected:
        raise RuntimeError(
            f"regular_ll values har {values.size} punkter, forventede {expected}"
        )

    missing_value = base.get_missing_value(gid)
    valid = np.isfinite(values) & (np.abs(values) < 100.0)
    if missing_value is not None:
        valid &= ~np.isclose(values, missing_value, rtol=0.0, atol=1e-9)

    bitmap_present = bool(get_int(gid, "bitmapPresent", 0))
    bitmap_masked = 0
    if bitmap_present:
        bitmap = np.asarray(codes_get_array(gid, "bitmap"), dtype=np.int8)
        if bitmap.size != expected:
            raise RuntimeError(
                f"Bitmap har {bitmap.size} celler, forventede {expected}"
            )
        bitmap_valid = bitmap.astype(bool)
        bitmap_masked = int(np.count_nonzero(~bitmap_valid))
        valid &= bitmap_valid

    values = np.where(valid, values, np.nan)

    j_points_consecutive = get_int(gid, "jPointsAreConsecutive", 0)
    alternative_rows = get_int(gid, "alternativeRowScanning", 0)
    i_scans_negatively = get_int(gid, "iScansNegatively", 0)
    j_scans_positively = get_int(gid, "jScansPositively", 0)

    if j_points_consecutive:
        value_grid = values.reshape((ni, nj)).T
    else:
        value_grid = values.reshape((nj, ni))

    # In alternating-row scanning every second i-row is stored in reverse.
    # Convert all rows to the same west/east orientation before georeferencing.
    if alternative_rows:
        value_grid[1::2] = value_grid[1::2, ::-1]

    first_lat = float(codes_get(gid, "latitudeOfFirstGridPointInDegrees"))
    first_lon = float(codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
    last_lat = float(codes_get(gid, "latitudeOfLastGridPointInDegrees"))
    last_lon = float(codes_get(gid, "longitudeOfLastGridPointInDegrees"))

    # For this DMI regular_ll field the first and last points are opposite grid
    # corners. Use the endpoints and point counts, which retain the actual
    # 30/50 arc-second geometry better than GRIB1's rounded di/dj keys.
    lat_axis = np.linspace(first_lat, last_lat, nj, dtype=np.float64)
    lon_axis = np.linspace(first_lon, last_lon, ni, dtype=np.float64)

    # Keep the value array aligned with ascending axes for downstream rastering.
    if lat_axis[0] > lat_axis[-1]:
        lat_axis = lat_axis[::-1]
        value_grid = value_grid[::-1, :]
    if lon_axis[0] > lon_axis[-1]:
        lon_axis = lon_axis[::-1]
        value_grid = value_grid[:, ::-1]

    lon_grid, lat_grid = np.meshgrid(lon_axis, lat_axis)

    print(
        "[regular_ll] scanning: "
        f"iNegative={i_scans_negatively} "
        f"jPositive={j_scans_positively} "
        f"jConsecutive={j_points_consecutive} "
        f"alternating={alternative_rows}; "
        f"first=({first_lat:.6f},{first_lon:.6f}) "
        f"last=({last_lat:.6f},{last_lon:.6f})"
    )

    return lat_grid, lon_grid, value_grid, bitmap_masked, bitmap_present


def read_grid(grib_path: Path):
    with grib_path.open("rb") as handle:
        while True:
            gid = codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                if not base.is_waterlevel_message(gid):
                    continue

                try:
                    grid_type = str(codes_get(gid, "gridType"))
                except Exception:
                    grid_type = ""

                if grid_type == "regular_ll":
                    return read_regular_ll(gid)

                # Fallback for any future non-regular collection. This is the
                # original iterator-based logic, kept isolated from IDW.
                triples = np.asarray(codes_get_array(gid, "latLonValues"), dtype=np.float64)
                if triples.size % 3 != 0:
                    raise RuntimeError("latLonValues har uventet længde")

                points = triples.reshape((-1, 3))
                lats = points[:, 0]
                lons = points[:, 1]
                values = points[:, 2]
                missing_value = base.get_missing_value(gid)
                valid = np.isfinite(values) & (np.abs(values) < 100.0)
                if missing_value is not None:
                    valid &= ~np.isclose(values, missing_value, rtol=0.0, atol=1e-9)

                bitmap_present = bool(get_int(gid, "bitmapPresent", 0))
                bitmap_masked = 0
                if bitmap_present:
                    bitmap = np.asarray(codes_get_array(gid, "bitmap"), dtype=np.int8)
                    if bitmap.size != values.size:
                        raise RuntimeError("Bitmap matcher ikke gridstørrelsen")
                    valid &= bitmap.astype(bool)
                    bitmap_masked = int(np.count_nonzero(~bitmap.astype(bool)))

                values = np.where(valid, values, np.nan)
                ni = int(codes_get(gid, "Ni"))
                nj = int(codes_get(gid, "Nj"))
                if ni * nj != values.size:
                    raise RuntimeError("Ni*Nj matcher ikke antal gridpunkter")

                return (
                    lats.reshape((nj, ni)),
                    lons.reshape((nj, ni)),
                    values.reshape((nj, ni)),
                    bitmap_masked,
                    bitmap_present,
                )
            finally:
                codes_release(gid)

    raise RuntimeError(f"Fandt ikke DSLM/parameter 82 i {grib_path.name}")


base.read_grid = read_grid

if __name__ == "__main__":
    raise SystemExit(base.main())
