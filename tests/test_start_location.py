import threading
import unittest
from unittest.mock import Mock, patch

from app import RouteTab, Runner
from lepao.geo import offset_meters
from lepao.mumu import MuMuGateway


class StartLocationTests(unittest.TestCase):
    home = (30.0, 120.0)
    old = offset_meters(*home, 924, 0)

    def setUp(self):
        with patch("app.MuMuLocationClient"), patch.object(Runner, "_load_cfg", return_value={
                "lat": self.home[0], "lon": self.home[1], "push_interval": 1}):
            self.runner = Runner()
        self.addCleanup(self.runner._thread.join, 2)
        self.addCleanup(self.runner.shutdown)
        self.cancel = threading.Event()
        self.runner.trail.append(self.old)

    def test_idle_cached_fix_then_active_home_does_not_add_924_metres(self):
        self.runner.client.gps_status.side_effect = [
            {"enabled": True, "active": False, "location": self.old},
            {"enabled": True, "active": True, "location": self.home},
        ]
        waiting = Mock()
        with patch.object(self.cancel, "wait", return_value=False):
            self.assertTrue(self.runner.sync_start(self.home, self.cancel, waiting))
        waiting.assert_called_once()
        self.runner.client.set_wgs84.assert_called_once_with(*self.home)
        self.assertEqual(list(self.runner.trail), [self.home])
        self.assertEqual(self.runner.total_m, 0)

    def test_active_gps_that_stays_far_cannot_confirm_start(self):
        self.runner.client.gps_status.return_value = {"enabled": True, "active": True, "location": self.old}
        with patch("app.START_FIX_TIMEOUT_S", 0), self.assertRaisesRegex(ValueError, "GPS 仍距起点"):
            self.runner.sync_start(self.home, self.cancel, Mock())
        self.assertFalse(self.runner.trail)

    def test_cancel_while_waiting_keeps_start_unconfirmed(self):
        self.runner.client.gps_status.return_value = {"enabled": True, "active": False, "location": self.home}
        self.assertFalse(self.runner.sync_start(self.home, self.cancel, self.cancel.set))
        self.assertFalse(self.runner.trail)

    def test_only_first_departure_synchronizes(self):
        # Run the UI callbacks inline. Leave the movement worker mocked.
        tab = Mock(spec=RouteTab)
        tab.r = self.runner
        tab.map = Mock()
        tab.track = Mock()
        tab.stop_manual = Mock()
        tab.start_wgs = self.home
        tab.running = tab.closing = tab.origin_ready = False
        tab.post = lambda callback: callback()
        self.runner.feeder = Mock()
        self.runner.client.gps_status.return_value = {"enabled": True, "active": True, "location": self.home}
        with patch("app.threading.Thread") as thread, patch("app.plan_return", return_value=(None, "已到起点")) as plan:
            thread.side_effect = lambda *, target, args=(), daemon: Mock(start=lambda: target(*args))
            RouteTab._start_motion(tab, 2, 1, False)
            self.assertTrue(tab.origin_ready)
            plan.assert_not_called()
            tab.running = False
            self.runner.client.gps_status.return_value["location"] = self.old
            RouteTab._start_motion(tab, 2, 1, False)
            self.assertEqual(plan.call_args.args[:2], (self.old, self.home))
            self.runner.client.set_wgs84.assert_called_once_with(*self.home)

    def test_idle_gps_provider_is_distinct_from_an_enabled_switch(self):
        client = Mock(spec=MuMuGateway)
        client.location_enabled.return_value = True
        client.adb_shell.return_value = (
            "  passive provider:\n    service: ProviderRequest[@0]\n"
            "  gps provider:\n    service: ProviderRequest[OFF]\n"
            "    last location=Location[gps 30.001,120.001]\n"
        )
        gps = MuMuGateway.gps_status(client)
        self.assertTrue(gps["enabled"])
        self.assertFalse(gps["active"])
        self.assertEqual(gps["location"], (30.001, 120.001))
