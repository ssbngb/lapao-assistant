"""从当前位置沿已知路径或步行路网返回共同起点，坐标均为 WGS-84。"""
import json
import math
import threading
import time
import urllib.error
import urllib.request

from .geo import haversine, meters_per_degree
from .track import Track


COURSE_JOIN_METERS = 10


def _along_course(current, home, course):
    if course is None or course.path[0] != home:
        return None, math.inf
    sy, sx = meters_per_degree(current[0])
    nearest_gap = math.inf
    shortest_return = math.inf
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
        back = course.cum[i] + t * (course.cum[i+1] - course.cum[i])
        forward = course.length - back
        length = min(back, forward)
        if (gap, length) < (nearest_gap, shortest_return):
            nearest_gap, shortest_return = gap, length
            choice = i, join, back <= forward
    if choice is None or nearest_gap > COURSE_JOIN_METERS:
        return None, nearest_gap
    i, join, reverse = choice
    tail = course.path[i::-1] if reverse else course.path[i+1:]
    return Track([current, join, *tail], closed=False, corner_radius_m=0), nearest_gap


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
        try:
            response = urllib.request.urlopen(req, timeout=8)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                exc.close()
                raise ValueError(f"步行路网服务返回 HTTP {exc.code}，请稍后重试") from exc
            response = exc  # 路网也会用 HTTP 400 返回规划结果
        with response:
            data = json.load(response)
    except OSError as exc:
        raise ValueError("无法连接步行路网，请检查网络后重试") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("步行路网响应格式异常，请稍后重试") from exc
    if isinstance(data, dict) and data.get("code") in ("NoRoute", "NoSegment"):
        raise ValueError("步行路网未找到连接当前位置与起点的道路")
    try:
        if data["code"] != "Ok":
            raise ValueError("无效的路网结果")
        points = [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]
        if len(points) < 2 or any(not (-90 <= lat <= 90 and -180 <= lon <= 180) for lat, lon in points):
            raise ValueError("路线点无效")
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("步行路网响应格式异常，请稍后重试") from exc
    if haversine(*current, *points[0]) > 25 or haversine(*home, *points[-1]) > 25:
        raise ValueError("步行路网返回的道路与当前位置或起点相距超过 25 米")
    return Track([current, *points, home], closed=False, corner_radius_m=0)


def plan_return(current, home, course, trail, cancel):
    home_gap = haversine(*current, *home)
    if home_gap < 0.001:
        return None, "已到起点"
    if home_gap <= 2.5:
        return Track([current, home], closed=False, corner_radius_m=0), "起点附近"
    route, course_gap = _along_course(current, home, course)
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
    try:
        return _walking_route(current, home, cancel), "步行路网"
    except ValueError as exc:
        where = f"当前位置距起点 {home_gap:.0f} 米"
        if math.isfinite(course_gap):
            where += f"，距手绘路线最近 {course_gap:.1f} 米"
        raise ValueError(f"{where}。{exc}") from exc
