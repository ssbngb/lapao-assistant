import unittest
from unittest.mock import patch

from app import DEFAULTS, Runner, _random_speeds
from lepao.geo import haversine


class RandomSpeedTests(unittest.TestCase):
    def test_holds_speed_for_30_seconds_regardless_of_update_count(self):
        with patch("app.time.monotonic") as clock, patch(
                "app.random.uniform", side_effect=[2.7, 3.2, 2.9]):
            speeds = _random_speeds(3.0)
            for tick in range(300):
                clock.return_value = tick / 10
                self.assertEqual(next(speeds), 2.7)
            clock.return_value = 30.0
            self.assertEqual(next(speeds), 3.2)
            clock.return_value = 59.99
            self.assertEqual(next(speeds), 3.2)
            clock.return_value = 61.0
            self.assertEqual(next(speeds), 2.9)

    def test_joystick_displacement_when_turning_changing_speed_and_restarting(self):
        with (patch("app.MuMuLocationClient"), patch.object(Runner, "_load_cfg", return_value=DEFAULTS),
              patch("app.threading.Thread.start")):
            runner = Runner()
        origin = (30.0, 120.0)
        runner.trail.append(origin)

        # Each command is issued between two GPS updates.
        commands = iter([
            (0, 0, 3.0, True),
            (29, 90, 3.0, True),
            (30, 90, 3.0, True),
            (31, 90, 3.0, False),
            (32, 90, 4.0, True),
            (33, None, 4.0, True),
            (34, 180, 4.0, True),
        ])
        with patch("app.time.monotonic") as clock, patch(
                "app.random.uniform", side_effect=[2.7, 3.2, 4.4, 3.6]):
            def next_command(_delay=None):
                command = next(commands, None)
                if command is None:
                    runner.shutdown()
                    return
                clock.return_value, bearing, base, vary = command
                if bearing is None:
                    runner.halt()
                else:
                    runner.set_speed(base, vary)
                    runner.go(bearing)

            next_command()
            with patch.object(runner._stopped, "wait", side_effect=next_command):
                runner._loop()

        points = list(runner.trail)
        distances = [haversine(*a, *b) for a, b in zip(points, points[1:])]
        self.assertEqual(len(distances), 6)
        for distance, expected in zip(distances, [2.7, 2.7, 3.2, 3.0, 4.4, 3.6]):
            self.assertAlmostEqual(distance, expected, delta=0.02)
