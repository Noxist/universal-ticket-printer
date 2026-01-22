import unittest
from unittest import mock

from PIL import Image

import universal_ticket_printer as utp


class TestRobustness(unittest.TestCase):
    def test_write_log_safe_retries(self):
        mock_file = mock.mock_open()
        side_effects = [
            PermissionError("locked"),
            PermissionError("locked"),
            mock_file.return_value,
        ]

        def fake_open(*args, **kwargs):
            effect = side_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect

        with mock.patch("builtins.open", side_effect=fake_open), \
            mock.patch.object(utp.time, "sleep") as sleep_mock:
            utp._write_log_safe("hello\n")

        self.assertEqual(mock_file().write.call_count, 1)
        self.assertGreaterEqual(sleep_mock.call_count, 2)

    def test_parse_missing_dependencies_fd(self):
        log_text = "! LaTeX Error: File `calligra.fd' not found"
        missing_pkg, missing_tikz = utp._parse_missing_dependencies(log_text)
        self.assertEqual(missing_pkg, "calligra")
        self.assertIsNone(missing_tikz)

    def test_status_updates_for_render_and_mqtt(self):
        messages = []

        def callback(msg):
            messages.append(msg)

        with mock.patch.object(utp, "_check_pdflatex", return_value=True), \
            mock.patch.object(utp.importlib.util, "find_spec", return_value=object()), \
            mock.patch.object(utp, "render_with_pdflatex", return_value=Image.new("L", (10, 10))):
            utp.render_latex_image("x", status_callback=callback)

        def fake_send_mqtt(_img, cut=True, status_callback=None):
            if status_callback:
                status_callback("Status: Connecting to Cloud...")
            return True

        with mock.patch.object(utp, "send_lan_image", return_value=False), \
            mock.patch.object(utp, "send_mqtt_image", side_effect=fake_send_mqtt):
            utp.print_master(Image.new("L", (1, 1)), status_callback=callback)

        self.assertTrue(any(msg == "Status: Rendering LaTeX..." for msg in messages))
        self.assertTrue(any(msg == "Status: Connecting to Cloud..." for msg in messages))


if __name__ == "__main__":
    unittest.main()
