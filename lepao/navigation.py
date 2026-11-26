"""从当前位置沿已知路径或步行路网返回共同起点，坐标均为 WGS-84。"""
import json
import math
import threading
import time
import urllib.request

from .geo import haversine, meters_per_degree
from .track import Track


def _along_course(current, home, course):
    if course is None or course.path[0] != home:
        return None
    sy, sx = meters_per_degree(current[0])
    best = math.inf
    choice = None
    for i, (a, b) in enumerate(zip(course.path, course.path[1:])):
        dx, dy = (b[1] - a[1]) * sx, (b[0] - a[0]) * sy
        length_sq = dx * dx + dy * dy
        if not length_sq:
            continue
        t = ((current[1] - a[1]) * sx * dx + (current[0] - a[0]) * sy * dy) / length_sq
        t = max(0.0, min(1.0, t))
        join = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        gap = haversine(*current, *join)
        if gap > 3:
            continue
        back = course.cum[i] + t * (course.cum[i+1] - course.cum[i])
        forward = course.length - back
        length = gap + min(back, forward)
        if length < best:
            best = length
            choice = i, join, back <= forward
    if choice is None:
        return None
    i, join, reverse = choice
    tail = course.path[i::-1] if reverse else course.path[i+1:]
    return Track([current, join, *tail], closed=False, corner_radius_m=0)


_request_lock = threading.Lock()
_last_request = -math.inf


def _walking_route(current, home, cancel):
    global _last_request
    with _request_lock:
        if cancel.wait(max(0, _last_request + 1 - time.monotonic())):
            return None
        _last_request = time.monotonic()
    coords = ";".join(f"{lon:.7f},{lat:.7f}" for lat, lon in (current, home))
    url = ("https://routing.openstreetmap.de/routed-foot/route/v1/driving/" + coords
           + "?overview=full&geometries=geojson&radiuses=25;25")
    req = urllib.request.Request(url, headers={"User-Agent": "budao-lepao-desktop/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("code") != "Ok":
            raise ValueError("缺少步行路线")
        points = [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]
        if (len(points) < 2 or any(not (-90 <= lat <= 90 and -180 <= lon <= 180)
                                   for lat, lon in points)
                or haversine(*current, *points[0]) > 25 or haversine(*home, *points[-1]) > 25):
            raise ValueError("道路与端点不相连")
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("未取得相连的步行路线，请检查网络或在地图上绘制经过当前位置的路线") from exc
    return Track([current, *points, home], closed=False, corner_radius_m=0)


def plan_return(current, home, course, trail, cancel):
    gap = haversine(*current, *home)
    if gap < 0.001:
        return None, "已到起点"
    if gap <= 2.5:
        return Track([current, home], closed=False, corner_radius_m=0), "起点附近"
    route = _along_course(current, home, course)
    best = route.length if route else math.inf
    source = "手绘路线"
    if trail and trail[-1] == current:
        walked = 0.0
        for i in range(len(trail) - 1, -1, -1):
            if walked >= best:
                break
            gap = haversine(*trail[i], *home)
            if gap <= 2.5 and walked + gap < best:
                route = Track([*reversed(trail[i:]), home],
                              closed=False, corner_radius_m=0)
                source = "已跑轨迹"
                break
            if i:
                walked += haversine(*trail[i], *trail[i-1])
    if route is not None:
        return route, source
    return _walking_route(current, home, cancel), "步行路网"
