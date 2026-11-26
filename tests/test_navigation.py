import threading
import unittest
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

    def test_no_connected_path_or_road(self):
        with patch("lepao.navigation.urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaisesRegex(ValueError, "步行路线"):
                plan_return(self.far, self.home, None, [self.far], self.cancel)

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
