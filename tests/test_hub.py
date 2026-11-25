"""
Tests sin hardware ni GTK:   python3 -m unittest discover -s tests -v
Usan el «hid» simulado de mchose_hub/demo.py.
"""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from mchose_hub.demo import FakeHid
from mchose_hub.demo import DEMO_VENDOR_DESCRIPTOR, _unprofiled_a7
from mchose_hub.device import (DeviceAsleepError, DeviceDisconnectedError, HidBackend, MchoseDevice,
                               State, discover, discover_unprofiled)
from mchose_hub.hidinfo import make_base_profile, parse_report_descriptor, save_base_profile
from mchose_hub.hotplug import HotplugMonitor
from mchose_hub.profile import ProfileError, load_profile, load_profiles, parse_profile
from mchose_hub.protocol import ParamError, build_frame, parse_response
from mchose_hub.udevgen import collect_vids, detect_mchose_vids, existing_vids, render_rules

ROOT = Path(__file__).resolve().parent.parent
MOUSE = ROOT / "profiles" / "mchose_mouse_template.json"
KEYBOARD = ROOT / "profiles" / "mchose_keyboard_template.json"


def fast(profile):
    """Acorta los tiempos del perfil para que los tests no tarden."""
    t = profile.transport
    t.response_timeout_ms, t.retries, t.wake_delay_ms, t.inter_command_delay_ms = 40, 1, 5, 0
    return profile


class ProfileTests(unittest.TestCase):
    def test_templates_are_valid(self):
        for path in (MOUSE, KEYBOARD):
            self.assertFalse(load_profile(path).enabled)

    def test_disabled_templates_are_skipped_unless_requested(self):
        # Las plantillas están deshabilitadas; el perfil real del A7 está habilitado
        disabled = [p for p in load_profiles([ROOT / "profiles"])[0] if not p.enabled]
        self.assertEqual(disabled, [])
        self.assertEqual(len(load_profiles([ROOT / "profiles"], include_disabled=True)[0]), 3)

    def _mutated(self, fn):
        data = json.loads(MOUSE.read_text(encoding="utf-8"))
        fn(data)
        return data

    def test_rejects_undeclared_placeholder(self):
        data = self._mutated(lambda d: d["commands"]["set_dpi"].update(request="07 {nope}"))
        with self.assertRaisesRegex(ProfileError, "no está declarado"):
            parse_profile(data)

    def test_rejects_enabled_template_with_zero_vid(self):
        data = self._mutated(lambda d: d["device"].update(enabled=True))
        with self.assertRaisesRegex(ProfileError, "0x0000"):
            parse_profile(data)

    def test_rejects_checksum_outside_frame(self):
        data = self._mutated(lambda d: d["transport"]["checksum"].update(store_at=99))
        with self.assertRaises(ProfileError):
            parse_profile(data)

    def test_rejects_feature_pointing_to_missing_command(self):
        data = self._mutated(lambda d: d["features"][1].update(command="no_existe"))
        with self.assertRaisesRegex(ProfileError, "no existe"):
            parse_profile(data)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.p = load_profile(MOUSE)

    def frame(self, cmd, **values):
        return build_frame(self.p.transport, self.p.commands[cmd], values)

    def test_dpi_linear_u16le_and_checksum(self):
        f = self.frame("set_dpi", slot=0, dpi=1600)
        self.assertEqual(len(f), 17)
        self.assertEqual(f[:5].hex(), "0807001f00")            # DPI 1600 → 1600/50−1 = 31 = 0x001F
        self.assertEqual(f[16], (0x55 - sum(f[:16])) & 0xFF)    # checksum «sub8»

    def test_choice_uses_wire_value(self):
        self.assertEqual(self.frame("set_polling_rate", rate=4000)[2], 0x20)

    def test_rgb_and_brightness_scaling(self):
        f = self.frame("set_rgb", color="#ff8000", brightness=50)
        self.assertEqual(f[2:8].hex(), "01ff80007f03")          # efecto=1, color, 50 % → 127, velocidad=3

    def test_invalid_values_are_rejected(self):
        for cmd, kw in (("set_dpi", {"dpi": 999999}), ("set_polling_rate", {"rate": 333}),
                        ("set_rgb", {"color": "rojo"}), ("set_dpi", {"nope": 1})):
            with self.assertRaises(ParamError, msg=(cmd, kw)):
                self.frame(cmd, **kw)

    def test_parse_battery_response(self):
        resp = bytes([0x08, 0x04, 0, 0, 0, 0, 87, 1] + [0] * 9)
        self.assertEqual(parse_response(self.p.commands["get_battery"], resp),
                         {"battery_percent": 87, "charging": True})


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.hid = FakeHid()
        self.backend = HidBackend(self.hid)
        self.profile = fast(load_profile(MOUSE))
        infos = discover([self.profile], self.backend)
        self.assertEqual(len(infos), 1)
        self.dev = MchoseDevice(infos[0], self.backend)

    def test_battery_round_trip(self):
        self.assertEqual(self.dev.execute("get_battery"), {"battery_percent": 87, "charging": False})
        self.assertEqual(self.dev.state, State.READY)

    def test_write_only_command(self):
        self.assertIsNone(self.dev.execute("set_dpi", dpi=800))
        self.assertEqual(self.hid.sent[-1][1], 0x07)

    def test_sleeping_device_raises_asleep(self):
        self.hid.mode = "asleep"
        with self.assertRaises(DeviceAsleepError):
            self.dev.execute("get_battery")
        self.assertEqual(self.dev.state, State.ASLEEP)
        self.hid.mode = "ok"                                    # despierta: se recupera solo
        self.assertEqual(self.dev.execute("get_battery")["battery_percent"], 87)
        self.assertEqual(self.dev.state, State.READY)

    def test_unplugged_device_raises_disconnected(self):
        self.dev.execute("get_battery")
        self.hid.mode = "unplugged"
        with self.assertRaises(DeviceDisconnectedError):
            self.dev.execute("get_battery")
        self.assertEqual(self.dev.state, State.DISCONNECTED)

    def test_transient_io_error_is_recovered_by_reopening(self):
        self.dev.execute("get_battery")
        self.hid.io_failures = 1                                # p. ej. el USB se reinicia al despertar
        self.assertEqual(self.dev.execute("get_battery")["battery_percent"], 87)

    def test_concurrent_calls_are_serialized(self):
        results, errors = [], []

        def work():
            try:
                results.append(self.dev.execute("get_battery")["battery_percent"])
            except Exception as exc:                            # pragma: no cover
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((results, errors), ([87] * 8, []))

    def test_follow_up_command_is_sent(self):
        kb = fast(load_profile(KEYBOARD))
        hid = FakeHid([{"path": b"demo:kb", "vendor_id": 0, "product_id": 0, "usage_page": 0xFF00,
                        "usage": 1, "interface_number": 2, "product_string": "KB"}])
        backend = HidBackend(hid)
        dev = MchoseDevice(discover([kb], backend)[0], backend)
        dev.execute("set_rgb", effect=2)
        self.assertEqual([f[1] for f in hid.sent], [0x06, 0xF0])   # set_rgb y después «commit»

    def test_discovery_ignores_other_interfaces(self):
        hid = FakeHid([{"path": b"a", "vendor_id": 0, "product_id": 0, "usage_page": 0x0001, "usage": 2,
                        "interface_number": 0},                     # interfaz del puntero: NO se toca
                       {"path": b"b", "vendor_id": 0, "product_id": 0, "usage_page": 0xFF02, "usage": 1,
                        "interface_number": 1}])
        infos = discover([self.profile], HidBackend(hid))
        self.assertEqual([i.path for i in infos], [b"b"])


class ProfileIdsTests(unittest.TestCase):
    def test_ids_form_supports_different_vids_for_cable_and_dongle(self):
        data = json.loads(MOUSE.read_text(encoding="utf-8"))
        data["device"]["enabled"] = True
        data["match"] = {"ids": ["3837:4018", "5253:1020"], "interface_number": 2}
        p = parse_profile(data)
        self.assertTrue(p.matches_ids(0x3837, 0x4018) and p.matches_ids(0x5253, 0x1020))
        self.assertFalse(p.matches_ids(0x3837, 0x1020))
        self.assertEqual(p.vendor_ids, (0x3837, 0x5253))

    def test_ids_must_be_vid_pid_hex(self):
        data = json.loads(MOUSE.read_text(encoding="utf-8"))
        data["match"] = {"ids": ["3837-4018"]}
        with self.assertRaisesRegex(ProfileError, "VID:PID"):
            parse_profile(data)


class UnprofiledDeviceTests(unittest.TestCase):
    """Escenario real: A7 V2 Pro por cable (3837:4018) sin perfil + otros HID que no son MCHOSE."""

    def setUp(self):
        other = {"path": b"/dev/hidraw3", "vendor_id": 0x046D, "product_id": 0x0AAA, "usage_page": 0xFF99,
                 "usage": 1, "interface_number": 3, "product_string": "PRO X SE Gaming Headset"}
        self.hid = FakeHid(_unprofiled_a7() + [other])
        self.backend = HidBackend(self.hid)

    def test_mchose_is_detected_without_any_profile(self):
        found = discover_unprofiled([], self.backend)
        self.assertEqual([(d.vid_pid, d.display_name) for d in found], [("3837:4018", "MCHOSE A7 V2 Pro")])
        self.assertIn("sin perfil", found[0].reason)
        self.assertEqual(len(found[0].nodes()), 3)                 # hidraw0, hidraw1, hidraw2

    def test_show_all_lists_everything(self):
        self.assertEqual(len(discover_unprofiled([], self.backend, show_all=True)), 2)

    def test_profile_with_wrong_interface_is_explained(self):
        data = json.loads(MOUSE.read_text(encoding="utf-8"))
        data["device"]["enabled"] = True
        data["match"] = {"ids": ["3837:4018"], "interface_number": 7}
        found = discover_unprofiled([parse_profile(data)], self.backend)
        self.assertIn("ninguna interfaz", found[0].reason)

    def test_report_descriptor_parser(self):
        reports = parse_report_descriptor(DEMO_VENDOR_DESCRIPTOR)
        self.assertEqual([(r.kind, r.report_id, r.size, r.usage_page) for r in reports],
                         [("Input", 9, 63, 0xFF01), ("Output", 9, 63, 0xFF01), ("Feature", 9, 63, 0xFF01)])
        self.assertEqual(reports[0].frame_len, 64)

    def test_base_profile_is_valid_and_claims_the_device(self):
        dev = discover_unprofiled([], self.backend)[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = save_base_profile(dev, self.backend, tmp)
            profile = load_profile(path)
            self.assertTrue(profile.enabled and profile.matches_ids(0x3837, 0x4018))
            self.assertEqual((profile.transport.write_mode, profile.transport.read_mode,
                              profile.transport.report_id, profile.transport.report_length),
                             ("output_report", "input_report", 9, 64))
            infos = discover([profile], self.backend)              # ahora SÍ lo reclama el perfil (IF2)
            self.assertEqual([i.path for i in infos], [b"demo:u2"])
            # El dongle (otro VID:PID) se añade al MISMO perfil
            dongle = discover_unprofiled([], HidBackend(FakeHid(
                [dict(e, vendor_id=0x5253, product_id=0x1020) for e in _unprofiled_a7()])))[0]
            self.assertEqual(save_base_profile(dongle, self.backend, tmp), path)
            both = load_profile(path)
            self.assertTrue(both.matches_ids(0x5253, 0x1020) and both.matches_ids(0x3837, 0x4018))
        self.assertEqual(make_base_profile(dev, self.backend)["device"]["kind"], "mouse")


class UdevDetectionTests(unittest.TestCase):
    def test_connected_mchose_vids_are_added_and_previous_ones_kept(self):
        detected = detect_mchose_vids(_unprofiled_a7() + [{"vendor_id": 0x046D, "product_string": "Headset"}])
        self.assertEqual(list(detected), [0x3837])
        with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as fh:
            fh.write(render_rules({0x5253: "detectado: dongle"}))
        previous = existing_vids(fh.name)
        self.assertEqual(previous, {0x5253: "detectado: dongle"})
        vids = collect_vids([], include_known=False, detected=detected, previous=previous)
        self.assertEqual(set(vids), {0x3837, 0x5253})


class UdevTests(unittest.TestCase):
    def test_rules_cover_usb_and_bluetooth(self):
        rules = render_rules({0x320F: "x"})
        self.assertIn('ATTRS{idVendor}=="320f", TAG+="uaccess"', rules)
        self.assertIn('KERNELS=="0005:320F:*"', rules)

    def test_group_mode(self):
        self.assertIn('GROUP="plugdev", MODE="0660"', render_rules({0x1234: "x"}, group="plugdev"))

    def test_collect_vids_ignores_disabled_and_zero(self):
        self.assertEqual(set(collect_vids([load_profile(MOUSE)], include_known=False)), set())
        self.assertIn(0x320F, collect_vids([], include_known=True))
        self.assertIn(0xABCD, collect_vids([], extra=[0xABCD], include_known=False))


class HotplugTests(unittest.TestCase):
    def test_polling_detects_plug_and_unplug(self):
        hid = FakeHid()
        changed = threading.Event()
        mon = HotplugMonitor(changed.set, enumerate_fn=hid.enumerate, poll_interval=0.05, force_polling=True)
        mon.start()
        try:
            hid.mode = "unplugged"
            self.assertTrue(changed.wait(2), "no se detectó la desconexión")
            changed.clear()
            hid.mode = "ok"
            self.assertTrue(changed.wait(2), "no se detectó la conexión")
        finally:
            mon.stop()


if __name__ == "__main__":
    unittest.main()
