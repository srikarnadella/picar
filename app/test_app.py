"""Run: .venv/bin/python -m unittest app.test_app   (no window needed)"""
import os
import tempfile
import time
import unittest

from app import picar_app as A


class T(unittest.TestCase):
    def test_shell_runs_commands_and_has_the_car_shortcut(self):
        A.setup_files()
        out = []
        t = A.Terminal(out.append)
        t.start(100, 30)
        t.write("echo hello-$((1+1)); type car | head -1\r")
        end = time.time() + 8
        while time.time() < end and b"hello-2" not in b"".join(out):
            time.sleep(0.1)
        time.sleep(0.5)
        text = b"".join(out).decode(errors="replace")
        self.assertIn("hello-2", text)
        self.assertIn("car is a shell function", text)
        t.write("exit\r")

    def test_unreachable_car_gives_a_plain_message(self):
        cfg = os.path.join(tempfile.mkdtemp(), "ssh_config")
        with open(cfg, "w") as f:
            f.write("Host picar\n  HostName 192.0.2.1\n  User pi\n  ConnectTimeout 2\n")   # a test address that never answers
        token, err = A.fetch_keys(cfg, timeout=6)
        self.assertIsNone(token)
        self.assertTrue(err)

    def test_dangerous_buttons_ask_first(self):
        asks = {label for _, label, _, q in A.COMMANDS if q}
        self.assertTrue({"Stop software", "Self-test", "Reboot Pi", "Shut down Pi"} <= asks)
        self.assertTrue(all(cmd.strip() for _, _, cmd, _ in A.COMMANDS))

    def test_knows_when_something_is_running_and_can_stop_it(self):
        A.setup_files()
        t = A.Terminal(lambda data: None)
        t.start(100, 30)
        time.sleep(1.5)
        self.assertIsNone(t.busy())                     # idle prompt
        t.write("sleep 30\r")
        time.sleep(0.8)
        self.assertEqual(t.busy(), "sleep")             # a program is in front: a button would type into it
        t.stop_running()
        self.assertIsNone(t.busy())                     # Ctrl-C freed the prompt
        t.write("exit\r")


if __name__ == "__main__":
    unittest.main()
