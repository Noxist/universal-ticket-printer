import os
import sys
import types
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import universal_ticket_printer as utp


class DummyStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 0


class AutoInstallMockTest(unittest.TestCase):
    def test_missing_package_triggers_install_with_hidden_window(self):
        fake_pdf2image = types.SimpleNamespace(
            convert_from_path=lambda *args, **kwargs: [Image.new("L", (10, 10), 255)]
        )
        state = {"pdflatex_calls": 0}
        popen_calls = []

        def popen_side_effect(cmd, cwd=None, stdout=None, stderr=None, text=None, creationflags=None, startupinfo=None):
            popen_calls.append(
                {
                    "cmd": cmd,
                    "cwd": cwd,
                    "creationflags": creationflags,
                    "startupinfo": startupinfo,
                }
            )

            class FakeProc:
                def __init__(self):
                    self.returncode = None

                def communicate(self, timeout=None):
                    if cmd[0] == "pdflatex":
                        state["pdflatex_calls"] += 1
                        if state["pdflatex_calls"] == 1:
                            with open(os.path.join(cwd, "ticket.log"), "w", encoding="utf-8") as handle:
                                handle.write("! LaTeX Error: File `tcolorbox.sty' not found")
                            self.returncode = 1
                            return ("", "")
                        self.returncode = 0
                        return ("", "")
                    if cmd[0] == "mpm":
                        self.returncode = 0
                        return ("", "")
                    if cmd[0] == "initexmf":
                        self.returncode = 0
                        return ("", "")
                    self.returncode = 0
                    return ("", "")

                def kill(self):
                    return None

            return FakeProc()

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "installed_libraries.txt")
            with patch.dict(sys.modules, {"pdf2image": fake_pdf2image}):
                with patch.object(utp, "INSTALLED_LIBS_FILE", manifest_path):
                    with patch.object(utp, "time") as mock_time:
                        mock_time.sleep.return_value = None
                        with patch.object(utp.os, "name", "nt"):
                            with patch.object(utp.subprocess, "STARTUPINFO", DummyStartupInfo):
                                with patch.object(utp.subprocess, "CREATE_NO_WINDOW", 134217728):
                                    with patch.object(utp.subprocess, "STARTF_USESHOWWINDOW", 1):
                                        with patch.object(utp.subprocess, "SW_HIDE", 0):
                                            with patch.object(utp.subprocess, "Popen", side_effect=popen_side_effect):
                                                image = utp.render_with_pdflatex("x")

        self.assertIsNotNone(image)
        self.assertEqual(state["pdflatex_calls"], 2)
        mpm_calls = [call for call in popen_calls if call["cmd"][0] == "mpm"]
        self.assertEqual(len(mpm_calls), 1)
        mpm_call = mpm_calls[0]
        self.assertEqual(mpm_call["cmd"], ["mpm", "--admin", "--install", "tcolorbox"])
        self.assertIsNotNone(mpm_call["startupinfo"])
        self.assertEqual(mpm_call["creationflags"], utp.subprocess.CREATE_NO_WINDOW)
        self.assertEqual(mpm_call["startupinfo"].wShowWindow, utp.subprocess.SW_HIDE)


if __name__ == "__main__":
    unittest.main()
