"""Picar for Mac: the car's dashboard and a terminal with one-click car commands, in one window.

Run:  .venv/bin/python app/picar_app.py        (or open ~/Applications/Picar.app, made by app/build_app.sh)
Car address: PICAR_HOST env var, default raspberrypi.local. Uses the SSH key ~/.ssh/picar_ed25519.

The dashboard is the car's own page (so every car feature appears here automatically); the app fetches the
car's key over SSH, so there is nothing to type. The terminal is your normal shell, plus a `car` command:
`car` logs into the car, `car 'some command'` runs one there. The buttons type their command into the
terminal, so you always see exactly what runs.
"""
import base64
import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
KEY = os.path.expanduser("~/.ssh/picar_ed25519")
STATE = os.path.expanduser("~/.picar-app")          # generated ssh config and shell setup (not your own files)
HOST = os.environ.get("PICAR_HOST", "raspberrypi.local")
PORT = json.load(open(os.path.join(ROOT, "car", "config.json")))["port"]

# one button for what used to be four (status, Wi-Fi, disk and memory, controller); no single quotes inside
STATUS = ('printf "software:  "; systemctl is-active picar; printf "up:        "; uptime -p; '
          'printf "cpu:       "; vcgencmd measure_temp; printf "power:     "; vcgencmd get_throttled; '
          'printf "address:   "; hostname -I; printf "wi-fi:     "; iwgetid -r || echo unknown; '
          'printf "disk:      "; df -h / | tail -1; printf "memory:    "; free -m | sed -n 2p; '
          'printf "ps3:       "; [ -e /dev/input/js0 ] && echo connected || echo not connected')
# group, label, command typed into the terminal, confirmation question (None = runs straight away)
COMMANDS = [
    ("Car", "Connect to car", "car", None),
    ("Car", "Live logs", "car 'journalctl -u picar -f -n 40 -o cat'", None),
    ("Car", "Car status", "car '%s'" % STATUS, None),
    ("Car", "Restart software", "car 'sudo systemctl restart picar && systemctl is-active picar'", None),
    ("Test and deploy", "Self-test", "car 'sudo systemctl stop picar; cd ~/picar && ~/env/bin/python -m car.selftest; sudo systemctl start picar'",
     "Are the wheels off the ground? The steering and wheels will move."),
    ("Test and deploy", "Run tests", "cd '%s' && .venv/bin/python -m unittest car/test_*.py app/test_*.py" % ROOT, None),
    ("Test and deploy", "Deploy from Mac", "cd '%s' && deploy/sync.sh" % ROOT, None),
    ("Power", "Stop software", "car 'sudo systemctl stop picar && echo stopped: motors neutral'", "Stop the car software? The motors go to neutral and the dashboard disconnects."),
    ("Power", "Reboot Pi", "car 'sudo reboot'", "Reboot the Pi? The car is offline for about a minute."),
    ("Power", "Shut down Pi", "car 'sudo shutdown -h now'", "Shut the Pi down? You will need to unplug and replug its power to start it again."),
]


def setup_files():
    """An ssh config just for the car and a shell setup that adds `car`; your own ~/.ssh/config and ~/.zshrc are untouched."""
    os.makedirs(STATE, exist_ok=True)
    cfg = os.path.join(STATE, "ssh_config")
    with open(cfg, "w") as f:
        f.write("Host picar\n  HostName %s\n  User pi\n  IdentityFile %s\n  IdentitiesOnly yes\n"
                "  ConnectTimeout 6\n  ServerAliveInterval 5\n  StrictHostKeyChecking accept-new\n" % (HOST, KEY))
    with open(os.path.join(STATE, ".zshrc"), "w") as f:
        f.write('[ -f "$HOME/.zshrc" ] && ZDOTDIR="$HOME" source "$HOME/.zshrc"\n'
                'car() { if [ $# -eq 0 ]; then ssh -F "%s" picar; else ssh -t -F "%s" picar "$@"; fi; }\n'
                'echo "Type  car  to log into the car, or use the buttons above."\n' % (cfg, cfg))
    return cfg


def fetch_keys(cfg, timeout=8):
    """The car's driving key, over SSH (the same key login the terminal uses)."""
    try:
        out = subprocess.run(["ssh", "-n", "-F", cfg, "-o", "BatchMode=yes", "picar", "cat ~/picar/car/.token"],
                             capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "The car did not answer within %d s. Is it on, and on the same Wi-Fi as this Mac?" % timeout
    if out.returncode != 0:
        return None, (out.stderr.strip().splitlines() or ["ssh failed"])[-1]
    return out.stdout.strip(), None


class Terminal:
    """Your shell in a pseudo-terminal; output goes to on_output as bytes."""
    def __init__(self, on_output):
        self.on_output, self.fd, self.pid = on_output, None, None

    def start(self, cols=100, rows=30):
        if self.fd is not None:
            return
        self.size = (cols, rows)
        env = dict(os.environ, ZDOTDIR=STATE, TERM="xterm-256color", COLORTERM="truecolor")
        pid, fd = pty.fork()
        if pid == 0:  # child: become the shell straight away
            os.chdir(ROOT)
            os.execve("/bin/zsh", ["/bin/zsh", "-i"], env)
        self.pid, self.fd = pid, fd
        self.size = (cols, rows)
        self.resize(cols, rows)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                if select.select([self.fd], [], [], 1.0)[0]:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    self.on_output(data)
            except OSError:
                break
        fd, self.fd = self.fd, None
        try:
            os.close(fd)
            os.waitpid(self.pid, 0)      # reap the old shell: no zombie or open pty per restart
        except OSError:
            pass
        self.on_output(b"\r\n[shell ended - starting a new one]\r\n")
        self.start(*self.size)          # a closed shell never leaves the panel dead

    def busy(self):
        """The program in front, if it is not the shell itself (e.g. 'ssh' showing live logs), else None."""
        try:
            pg = os.tcgetpgrp(self.fd)
        except (OSError, TypeError):
            return None
        if pg == self.pid:
            return None
        name = subprocess.run(["ps", "-o", "comm=", "-p", str(pg)], capture_output=True, text=True).stdout.strip()
        return os.path.basename(name) or "a program"

    def stop_running(self):
        """Ctrl-C; if an interactive ssh session is still in front, its ~. escape closes it."""
        self.write("\x03")
        for _ in range(10):
            if not self.busy():
                return
            time.sleep(0.06)
        if self.busy() == "ssh":
            self.write("\r~.")
            for _ in range(15):
                if not self.busy():
                    return
                time.sleep(0.06)

    def write(self, text):
        if self.fd is not None:
            os.write(self.fd, text.encode())

    def resize(self, cols, rows):
        self.size = (cols, rows)
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))


class Api:
    """Called from the window's JavaScript (pywebview js_api)."""
    def __init__(self):
        self.window = None
        self.cfg = setup_files()
        self.term = Terminal(self._to_screen)

    def _to_screen(self, data):
        if self.window:
            self.window.evaluate_js("termWrite('%s')" % base64.b64encode(data).decode())

    def info(self):
        return {"host": HOST, "commands": [{"group": g, "label": l, "cmd": c, "confirm": q} for g, l, c, q in COMMANDS]}

    def connect(self):
        token, err = fetch_keys(self.cfg)
        if err:
            return {"ok": False, "error": err}
        return {"ok": True, "url": "http://%s:%d/?t=%s&view=laptop&join=watch" % (HOST, PORT, token)}   # watch if someone is driving

    def term_start(self, cols, rows):
        self.term.start(cols, rows)

    def term_input(self, text):
        self.term.write(text)

    def term_resize(self, cols, rows):
        self.term.resize(cols, rows)

    def busy(self):
        return self.term.busy()

    def run(self, cmd, stop_first=False):
        if stop_first:
            self.term.stop_running()
        self.term.write(cmd + "\r")

    def open_in_browser(self, url):
        subprocess.Popen(["open", url])


def single_instance():
    """A second launch would open a second window driving the same car: refuse it."""
    os.makedirs(STATE, exist_ok=True)
    lock = open(os.path.join(STATE, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        subprocess.run(["osascript", "-e", 'display notification "Picar is already open." with title "Picar"'])
        sys.exit(0)
    return lock


def main():
    import webview
    lock = single_instance()  # noqa: F841 (held for the life of the app)
    api = Api()
    api.window = webview.create_window("Picar", os.path.join(HERE, "ui", "index.html"), js_api=api,
                                       width=1440, height=900, min_size=(1000, 640), background_color="#131217")

    def dock_icon():
        try:
            import AppKit
            img = AppKit.NSImage.alloc().initWithContentsOfFile_(os.path.join(HERE, "icon.png"))
            if img:
                AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        except Exception:
            pass
    webview.start(dock_icon, http_server=True)


if __name__ == "__main__":
    sys.exit(main())
