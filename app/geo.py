import json
import logging
import math
import urllib.request

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _point_to_segment_m(lat: float, lon: float, a: list, b: list) -> float:
    # Local equirectangular projection around the point; accurate enough at corridor scale.
    k = math.cos(math.radians(lat))
    ax, ay = (a[1] - lon) * k, a[0] - lat
    bx, by = (b[1] - lon) * k, b[0] - lat
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py) * (math.pi / 180) * EARTH_RADIUS_M


def distance_to_polyline_m(lat: float, lon: float, polyline: list) -> float:
    if not polyline:
        return 0.0
    if len(polyline) == 1:
        return haversine_m(lat, lon, polyline[0][0], polyline[0][1])
    return min(_point_to_segment_m(lat, lon, polyline[i], polyline[i + 1]) for i in range(len(polyline) - 1))


def osrm_route(osrm_url: str, lat1: float, lon1: float, lat2: float, lon2: float):
    """Road route from an OSRM server. Returns (polyline [[lat, lon]], distance_m, duration_s) or None."""
    if not osrm_url:
        return None
    url = (
        f"{osrm_url.rstrip('/')}/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
        "?overview=simplified&geometries=geojson"
    )
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.load(resp)
        route = data["routes"][0]
        coords = [[c[1], c[0]] for c in route["geometry"]["coordinates"]]
        return coords, float(route["distance"]), float(route["duration"])
    except Exception as exc:  # network / bad response: caller falls back to straight line
        log.warning("OSRM route failed: %s", exc)
        return None
