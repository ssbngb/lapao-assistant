import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from lepao.geo import haversine, offset_meters
from lepao.navigation import plan_return
from lepao.track import Track


class ReturnPlanningTests(unittest.TestCase):
    def setUp(self):
        self.home = (30.0, 120.0)
        self.cancel = threading.Event()
        self.corner = offset_meters(*self.home, 40, 0)
        self.far = offset_meters(*self.corner, 0, 30)

    def test_reverse_recorded_turns(self):
        trail = [self.home, self.corner, self.far]
        track, _ = plan_return(self.far, self.home, None, trail, self.cancel)
        self.assertEqual(track.path, list(reversed(trail)))
        self.assertEqual(track.position_at(track.length), self.home)

    def test_shorter_side_of_a_closed_course_from_between_vertices(self):
        course = Track([self.home, self.corner, self.far], corner_radius_m=0)
        for progress in (0.1, 0.8):
            with self.subTest(progress=progress):
                current = course.position_at(course.length * progress)
                track, _ = plan_return(current, self.home, course, [], self.cancel)
                self.assertLess(haversine(*track.path[0], *current), 0.001)
                self.assertEqual(track.path[-1], self.home)
                self.assertAlmostEqual(track.length, course.length * min(progress, 1-progress), places=3)

    def test_nearby_fix_joins_closing_segment_without_network(self):
        course = Track([self.home, self.corner, self.far, offset_meters(*self.home, 0, 30)],
                       corner_radius_m=0)
        join = offset_meters(*self.home, 0, 15)
        current = offset_meters(*join, -6, 0)
        with patch("lepao.navigation._walking_route") as walking:
            track, source = plan_return(current, self.home, course, [], self.cancel)
            walking.assert_not_called()
        self.assertEqual(source, "手绘路线")
        self.assertEqual(track.path[0], current)
        self.assertLess(haversine(*track.path[1], *join), 0.01)
        self.assertEqual(track.path[-1], self.home)

    def test_disconnected_fix_reports_distances_and_network_failure(self):
        course = Track([self.home, self.corner, self.far], corner_radius_m=0)
        current = offset_meters(*self.home, 0, -20)
        with patch("lepao.navigation.urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(ValueError, r"距起点 20 米，距手绘路线最近 20\.0 米。无法连接步行路网"):
                plan_return(current, self.home, course, [current], self.cancel)

    def test_router_no_segment_is_distinct_from_network_failure(self):
        error = urllib.error.HTTPError("http://localhost", 400, "Bad Request", {},
                                       io.BytesIO(b'{"code":"NoSegment"}'))
        with patch("lepao.navigation.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ValueError, "步行路网未找到连接当前位置与起点的道路"):
                plan_return(self.far, self.home, None, [self.far], self.cancel)

    def test_disconnected_fix_can_still_use_walking_route(self):
        points = [self.far, self.corner, self.home]
        reply = {"code": "Ok", "routes": [{"geometry": {"coordinates": [p[::-1] for p in points]}}]}
        with patch("lepao.navigation.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())):
            track, source = plan_return(self.far, self.home, None, [], self.cancel)
        self.assertEqual(source, "步行路网")
        self.assertEqual(track.path, points)

    def test_already_at_home_or_planning_cancelled(self):
        with patch("lepao.navigation.urllib.request.urlopen") as request:
            track, _ = plan_return(self.home, self.home, None, [], self.cancel)
            self.assertIsNone(track)
            self.cancel.set()
            track, _ = plan_return(self.far, self.home, None, [], self.cancel)
            self.assertIsNone(track)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
