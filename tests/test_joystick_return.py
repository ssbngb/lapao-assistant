import time
import tkinter as tk
import unittest
from queue import Empty, SimpleQueue
from unittest.mock import Mock, patch

from app import DEFAULTS, JoystickTab, RouteTab, Runner
from lepao.geo import haversine, offset_meters


class JoystickReturnTests(unittest.TestCase):
    def test_hold_joystick_after_either_return_button(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.geometry("920x680+10000+10000")
        home = (30.0, 120.0)
        config = dict(DEFAULTS, lat=home[0], lon=home[1], push_interval=0.05)
        with patch("app.MuMuLocationClient"), patch.object(Runner, "_load_cfg", return_value=config):
            runner = Runner()
        self.addCleanup(runner._thread.join, 2)
        self.addCleanup(runner.shutdown)
        runner.feeder = Mock()
        queue = SimpleQueue()
        with patch.object(RouteTab, "_auto_load"):
            route = RouteTab(root, runner, queue.put)
        self.addCleanup(route.map.close)
        joy = JoystickTab(root, runner, route)
        joy.pack(fill="both", expand=True)
        root.update()

        def wait_until(condition):
            deadline = time.monotonic() + 3
            while not condition():
                self.assertLess(time.monotonic(), deadline, route.status.get())
                try:
                    callback = queue.get(timeout=0.01)
                except Empty:
                    pass
                else:
                    callback()
                root.update()

        for button in (joy.reset_btn, route.home_btn):
            with self.subTest(button=button.cget("text")):
                nearby = offset_meters(*home, 1, 0)
                runner.client.gps_status.return_value = dict(location=nearby, enabled=True, active=True)
                button.invoke()
                wait_until(lambda: not route.running)
                self.assertEqual(route.status.get(), "已回到共同起点")
                self.assertEqual(runner.trail[-1], home)

                x, y, radius = joy._joystick_geometry()
                joy.cv.event_generate("<ButtonPress-1>", x=int(x + radius), y=int(y))
                try:
                    wait_until(lambda: haversine(*runner.trail[-1], *home) > 0.1)
                    joy.show_speed()
                    joy.cv.event_generate("<Configure>")
                    self.assertTrue(joy.dragging)
                    self.assertTrue(joy.active)
                    previous = runner.trail[-1]
                    wait_until(lambda: haversine(*runner.trail[-1], *previous) > 0.3)
                finally:
                    joy.cv.event_generate("<ButtonRelease-1>")
                self.assertIsNone(runner.bearing)
