#!/usr/bin/env python3
"""
Blind Archer Duel - helmet program (game version of helmet.py).

Start it as root, with the player number:

    sudo python3 helmet_game.py 1      # this helmet belongs to player 1 (Odysseus, bow)
    sudo python3 helmet_game.py 2      # this helmet belongs to player 2 (suitor, crossbow)

Same camera and left/right buzz as helmet.py, plus:
  - the IR light flashes this helmet's player code in short 38 kHz bursts
    (the receivers on the weapons cannot see a steady, always-on carrier)
  - a long buzz on BOTH motors when this player is hit
  - fast ticks on both motors while this player's own weapon is on target
  - the camera is reopened automatically if it drops out
  - a camera page for debugging; the address is printed when the program starts
  - AIMING BY CAMERA: the player turns and tilts their head until the opponent's tag is in the middle
    of the picture. Left/right is felt on the helmet's two motors (as before). Up/down is sent to the
    player's own weapon ("cue <player> U|D|C|N <on target 0/1>"), which plays it on its motor.
    Tag in the middle both ways = ON TARGET: fast ticks on both helmet motors, and a shot now is a hit.
  - the tag's position in metres (x right, y up, z forward): printed, shown on the camera page,
    and served at /xyz on the camera page
The motor pins and the tag number are copied from helmet.py in the same folder,
so each helmet keeps its own wiring. helmet.py itself is not changed.
"""
import json
import multiprocessing
import os
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import atan2, ceil, degrees, sqrt
from pathlib import Path
from time import monotonic, sleep

# ---------------- settings ----------------
# None = copy the value from helmet.py in this folder.
LEFT_PIN = None
LEFT_REVERSE_PIN = None
RIGHT_PIN = None
RIGHT_REVERSE_PIN = None
TARGET_TAG = None                   # AprilTag number worn by the OPPONENT
FALLBACK = {"left": 23, "left_reverse": 24, "right": 17, "right_reverse": 27, "tag": 0}

IR_CARRIER_HZ = 100
IR_BURST_US = {1: 5000, 2: 5000}
IR_GAP_US = 5000
IR_BURSTS = 4
IR_PWM_CHANNEL = None               # None = ask the Pi which pin is set to PWM (GPIO18 = 2 on a Pi 5, 0 on older Pis)
IR_PWM_CHIP = None                  # None = find it. Or a name such as "pwmchip0"

GAME_PORT = 4210                    # everything in the game talks on this UDP port
GAME_BROADCAST = ("10.42.0.255", "255.255.255.255")   # everyone on the UNOQ network / on whatever network this Pi is on
HIT_BUZZ_S = 0.7                    # both motors, when I am hit
LOCK_CUE = True                     # fast ticks on both motors while my weapon is on target

UP_DOWN_BAND = (0.40, 0.60)         # tag centre inside this part of the picture's HEIGHT = level (left/right uses 0.425-0.575 of the width)
AIM_UPDOWN = True                   # False = "on target" only needs left/right (use it if this player's weapon has no motor)
CUE_SEND_S = 0.1                    # tell this player's weapon "aim up / aim down / on target" this often
MIN_TAG_PX = 10                     # ignore "tags" smaller than this many pixels across (false sightings)

TAG_SIZE_MM = 100                   # side of the tag's BLACK square in millimetres. Measure yours: every distance scales with it.
                                    # Can also be given when starting:  sudo python3 helmet_game.py 1 62
CAMERA_FOCAL_PX = 615               # Logitech C920 at 640x480. To calibrate, hold the tag at a measured distance and set this to
                                    #   (this number) x (real distance) / (z the program shows)
XYZ_PRINT_S = 1.0                   # print the tag's position this often while it is in view (0 = never)
XYZ_SEND_S = 0                      # send "pos <player> x y z" on the game network this often (0 = never)

VIEW_PORT = 8080                    # camera page: http://<this Pi's address>:8080   (0 = no camera page)
VIEW_OPEN = True                    # True = any device on the same network can open it.
                                    # False = only through an SSH tunnel from the laptop:
                                    #   ssh -L 18080:localhost:8080 pi@h1   then open http://localhost:18080
VIDEO_DEV_DIR = Path(os.environ.get("VIDEO_DEV_DIR", "/dev"))

PWM_ROOT = Path(os.environ.get("PWM_ROOT", "/sys/class/pwm"))
LIRC_DEVICE = os.environ.get("IR_DEVICE", "/dev/lirc0")
PINCTRL = os.environ.get("PINCTRL_CMD", "pinctrl")
HERE = Path(__file__).resolve().parent


def log(*a):
    print(*a, flush=True)


# ---------------- copy this helmet's wiring from helmet.py ----------------
def read_helmet_py():
    cfg = dict(FALLBACK)
    cfg["dict"] = "DICT_APRILTAG_16h5"
    src_file = HERE / "helmet.py"
    found = []
    if src_file.exists():
        text = src_file.read_text(errors="ignore")
        for key in ("left", "left_reverse", "right", "right_reverse"):
            m = re.search(r"^%s\s*=\s*OutputDevice\(\s*(\d+)" % key, text, re.M)
            if m:
                cfg[key] = int(m.group(1))
                found.append(key)
        m = re.search(r"int\(tag\)\s*==\s*(\d+)", text)
        if m:
            cfg["tag"] = int(m.group(1))
            found.append("tag")
        m = re.search(r"cv2\.aruco\.(DICT_\w+)", text)
        if m:
            cfg["dict"] = m.group(1)
    for key, override in (("left", LEFT_PIN), ("left_reverse", LEFT_REVERSE_PIN), ("right", RIGHT_PIN),
                          ("right_reverse", RIGHT_REVERSE_PIN), ("tag", TARGET_TAG)):
        if override is not None:
            cfg[key] = override
    source = "helmet.py" if len(found) == 5 else ("built-in defaults" if not found else "helmet.py + defaults")
    return cfg, source


# ---------------- IR beacon ----------------
def pwm_pins():
    """Ask the Pi which header pins are switched to hardware PWM. Returns {gpio: channel}, or None if it cannot tell."""
    try:
        out = subprocess.run([PINCTRL, "get", "12,13,18,19"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    pins = {}
    for line in out.splitlines():
        m = re.search(r"GPIO(\d+)\s*=\s*PWM(\d)(?:_(?:CHAN)?(\d))?", line)
        if m:                        # "PWM0_CHAN2" (Pi 5), "PWM0_1" (Pi 4), "PWM1" (older)
            pins[int(m.group(1))] = int(m.group(3) if m.group(3) is not None else m.group(2))
    return pins


def find_pwm():
    """Pick the PWM controller and channel that drive the IR pin (GPIO18 unless the Pi says otherwise)."""
    model = ""
    try:
        model = Path("/proc/device-tree/model").read_text(errors="ignore").strip("\x00\n ")
    except OSError:
        pass
    model = os.environ.get("PI_MODEL", model)
    pi5 = "Pi 5" in model or "Module 5" in model
    pins = pwm_pins()
    if IR_PWM_CHANNEL is not None:
        channel, where = IR_PWM_CHANNEL, "channel set by hand"
    elif pins:
        gpio = 18 if 18 in pins else sorted(pins)[0]
        channel, where = pins[gpio], "GPIO%d" % gpio
    else:
        channel, where = (2 if pi5 else 0), "GPIO18"
        if pins is not None:
            log("IR WARNING: no header pin is switched to hardware PWM, so the IR light will stay dark.")
            log("   Fix: add the line  dtoverlay=pwm  to /boot/firmware/config.txt, reboot, wire DAT to GPIO18.")
    chips = [PWM_ROOT / IR_PWM_CHIP] if IR_PWM_CHIP else sorted(PWM_ROOT.glob("pwmchip*"))

    def npwm(c):
        try:
            return int((c / "npwm").read_text().strip())
        except (OSError, ValueError):
            return 0

    def rank(c):                     # on a Pi 5 the header PWM is the controller at ...98000; ...9c000 is the fan
        real = os.path.realpath(c)
        return (0 if "98000" in real else 2 if "9c000" in real else 1, c.name)

    usable = sorted((c for c in chips if npwm(c) > channel), key=rank)
    if not usable:
        raise RuntimeError("no PWM controller with channel %d under %s (is  dtoverlay=pwm  in /boot/firmware/config.txt?)"
                           % (channel, PWM_ROOT))
    return usable[0], channel, where, model


def start_pwm():
    chip, channel, where, model = find_pwm()
    pwm_dir = chip / ("pwm%d" % channel)
    exported = False
    if not pwm_dir.exists():
        (chip / "export").write_text(str(channel))
        exported = True
        for _ in range(50):
            if pwm_dir.exists():
                break
            time.sleep(0.02)
    if not pwm_dir.exists():
        raise RuntimeError("PWM channel was not created: %s" % pwm_dir)
    period_ns = round(1_000_000_000 / IR_CARRIER_HZ)
    try:
        if (pwm_dir / "enable").read_text().strip() == "1":
            (pwm_dir / "enable").write_text("0")
    except OSError:
        pass
    try:
        (pwm_dir / "duty_cycle").write_text("0")
    except OSError:
        pass
    (pwm_dir / "period").write_text(str(period_ns))
    (pwm_dir / "duty_cycle").write_text("0")          # dark until the beacon process flashes it
    (pwm_dir / "enable").write_text("1")
    log("IR: %s = %s/%s, %d Hz carrier (%s)" % (where, chip.name, pwm_dir.name, IR_CARRIER_HZ, model or "unknown board"))
    return pwm_dir, period_ns // 2, exported


def stop_pwm(pwm_dir, exported):
    if pwm_dir is None:
        return
    for name, value in (("duty_cycle", "0"), ("enable", "0")):
        try:
            (pwm_dir / name).write_text(value)
        except OSError:
            pass
    if exported:
        try:
            (pwm_dir.parent / "unexport").write_text(pwm_dir.name[3:])
        except OSError:
            pass


def beacon_pwm(duty_path, duty_ns, burst_us, gap_us, bursts, packets=None):
    """Own process, so the camera loop can never stretch a burst. Flashes the carrier on and off."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)       # Ctrl+C: the main program switches this off
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(20))
    except (OSError, AttributeError):
        pass
    fd = os.open(duty_path, os.O_WRONLY)
    on, off = str(duty_ns).encode(), b"0"
    burst_ns, gap_ns = burst_us * 1000, gap_us * 1000
    clock = time.perf_counter_ns
    sent = 0
    try:
        while packets is None or sent < packets:
            for _ in range(bursts):
                os.pwrite(fd, on, 0)
                t = clock() + burst_ns
                while clock() < t:
                    pass
                os.pwrite(fd, off, 0)
                t = clock() + gap_ns
                while clock() < t:
                    pass
            sent += 1
            time.sleep(0.035 + random.random() * 0.02)
    finally:
        try:
            os.pwrite(fd, off, 0)
        finally:
            os.close(fd)


def beacon_lirc(device, burst_us, gap_us, bursts):
    """Used instead of PWM when the kernel IR sender (/dev/lirc0) is present: it does the timing itself."""
    seq = [burst_us, gap_us] * (bursts - 1) + [burst_us]
    data = struct.pack("%dI" % len(seq), *seq)
    while True:
        try:
            fd = os.open(device, os.O_WRONLY)
        except OSError as e:
            log("IR: cannot open", device, "-", e)
            time.sleep(5)
            continue
        try:
            while True:
                os.write(fd, data)
                time.sleep(0.035 + random.random() * 0.02)
        except OSError as e:
            log("IR error:", e)
            time.sleep(1)
        finally:
            os.close(fd)


# ---------------- game messages ----------------
class Game:
    def __init__(self, player, left, right, motor_lock):
        self.player, self.left, self.right, self.lock = player, left, right, motor_lock
        self.aim_until = 0.0
        self.last_shot = {}
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.out.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def send(self, text):
        for address in GAME_BROADCAST:
            try:
                self.out.sendto(text.encode(), (address, GAME_PORT))
            except OSError:
                pass

    def both(self, seconds):
        with self.lock:
            try:
                self.left.on()
                self.right.on()
                sleep(seconds)
            finally:
                self.left.off()
                self.right.off()

    def listener(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", GAME_PORT))
        sock.settimeout(0.5)
        hello = ("hello %d helmet" % self.player).encode()
        next_hello = 0.0
        while True:
            now = monotonic()
            if now >= next_hello:
                next_hello = now + 2.0
                for address in GAME_BROADCAST:
                    try:
                        sock.sendto(hello, (address, GAME_PORT))
                    except OSError:
                        pass
            try:
                data, _ = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                sleep(1)
                continue
            p = data.decode("ascii", "ignore").split()
            try:
                if len(p) >= 4 and p[0] == "shot":
                    shooter, hit, n = int(p[1]), int(p[2]), int(p[3])
                    last_n, last_t = self.last_shot.get(shooter, (None, 0.0))
                    if n == last_n and now - last_t < 1.0:
                        continue                     # same shot, sent more than once
                    self.last_shot[shooter] = (n, now)
                    if hit and shooter != self.player:
                        log("I WAS HIT by player", shooter)
                        threading.Thread(target=self.both, args=(HIT_BUZZ_S,), daemon=True).start()
                elif len(p) >= 3 and p[0] == "aim" and int(p[1]) == self.player:
                    self.aim_until = now + 0.4 if int(p[2]) else 0.0
            except ValueError:
                pass

    def lock_cue(self):
        while True:
            if LOCK_CUE and monotonic() < self.aim_until:
                self.both(0.04)
                sleep(0.12)
            else:
                sleep(0.03)


# ---------------- camera page (debugging) ----------------
class Viewer:
    """Shows what the camera sees. Pictures are only made while a browser is looking."""
    PAGE = (b"<!doctype html><title>Helmet camera</title>"
            b"<body style='margin:0;background:#111;color:#ddd;font-family:sans-serif;text-align:center'>"
            b"<img src='/stream' style='max-width:100%;height:auto'>"
            b"<p>Tag inside the yellow box = ON TARGET. Left/right of it buzzes the helmet; above/below goes to the weapon.</p>")

    def __init__(self):
        self.cond = threading.Condition()
        self.jpeg = None
        self.count = 0
        self.wanted_until = 0.0
        self.info = {"visible": False}               # latest tag position, for /xyz

    def wanted(self):
        return monotonic() < self.wanted_until

    def publish(self, jpeg):
        with self.cond:
            self.jpeg = jpeg
            self.count += 1
            self.cond.notify_all()

    def next_frame(self, last):
        self.wanted_until = monotonic() + 2.0
        with self.cond:
            self.cond.wait_for(lambda: self.count != last, timeout=1.0)
            return self.jpeg, self.count

    def serve(self):
        viewer = self

        class Page(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/xyz"):
                    body = json.dumps(viewer.info).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if not self.path.startswith("/stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(viewer.PAGE)))
                    self.end_headers()
                    self.wfile.write(viewer.PAGE)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                last = -1
                try:
                    while True:
                        jpeg, n = viewer.next_frame(last)
                        if jpeg is None or n == last:
                            continue
                        last = n
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg))
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except OSError:
                    pass                             # the browser tab was closed

        try:
            server = ThreadingHTTPServer(("" if VIEW_OPEN else "127.0.0.1", VIEW_PORT), Page)
        except OSError as e:
            log("Camera page OFF:", e)
            return
        server.daemon_threads = True
        if VIEW_OPEN:
            log("Camera page: http://%s:%d" % (my_address(), VIEW_PORT))
        else:
            log("Camera page: tunnel only. On the laptop: ssh -L 18080:localhost:%d pi@%s  then http://localhost:18080"
                % (VIEW_PORT, socket.gethostname()))
        server.serve_forever()


def my_address():
    """This Pi's address on the network it is using."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.254.254.254", 9))     # nothing is sent; this only picks the outgoing interface
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return socket.gethostname()


# ---------------- where the tag is, in metres ----------------
def make_locator(cv2, np, tag_mm):
    """Returns locate(corners, frame_width, frame_height) -> (x, y, z) in metres from the camera:
    x = to the right, y = up, z = straight ahead. None if it cannot be worked out."""
    half = tag_mm / 2000.0
    square = np.array([[-half, half, 0], [half, half, 0], [half, -half, 0], [-half, -half, 0]], dtype=np.float32)

    def locate(corners, width, height):
        focal = CAMERA_FOCAL_PX * width / 640.0
        lens = np.array([[focal, 0, width / 2.0], [0, focal, height / 2.0], [0, 0, 1]], dtype=np.float64)
        try:
            ok, _, t = cv2.solvePnP(square, corners.astype(np.float32), lens, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        except cv2.error:
            return None
        if not ok:
            return None
        x, y, z = (float(v) for v in t.ravel())
        return (x, -y, z) if z > 0 else None

    return locate


# ---------------- camera ----------------
def open_camera(cv2):
    """A USB camera that drops out and comes back gets a new number (video1, video2...), so try each one that exists.
    Numbers 10 and up are the Pi's own video hardware, not the webcam."""
    indexes = sorted(int(m.group(1)) for m in (re.fullmatch(r"video(\d)", p.name) for p in VIDEO_DEV_DIR.glob("video?")) if m)
    for index in indexes:
        cam = cv2.VideoCapture(index, cv2.CAP_V4L2)
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if cam.isOpened():
            ok, _ = cam.read()
            if ok:
                return cam, index
        cam.release()
    return None, None


# ---------------- main ----------------
def main():
    usage = ("Usage: sudo python3 helmet_game.py 1   (player 1 = bow)   or   2   (player 2 = crossbow)\n"
             "       add the tag's size in mm to override TAG_SIZE_MM:  sudo python3 helmet_game.py 1 62")
    if len(sys.argv) > 1 and sys.argv[1] in ("1", "2"):
        player = int(sys.argv[1])
    elif len(sys.argv) > 1:
        raise SystemExit(usage)
    else:
        player = 2 if socket.gethostname().rstrip().endswith("2") else 1
    tag_mm = float(TAG_SIZE_MM)
    if len(sys.argv) > 2:
        try:
            tag_mm = float(sys.argv[2])
        except ValueError:
            raise SystemExit(usage)
        if not 5 <= tag_mm <= 2000:
            raise SystemExit(usage)
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))      # so "systemctl stop" still switches everything off

    cfg, source = read_helmet_py()
    log("Helmet for player %d. Motors: left GPIO%d, right GPIO%d. Tracking tag %d. (wiring from %s)"
        % (player, cfg["left"], cfg["right"], cfg["tag"], source))

    pwm_dir, exported, beacon = None, False, None
    camera = None
    motors = []
    try:
        # --- IR beacon first, before any threads exist ---
        burst = IR_BURST_US[player]
        if os.path.exists(LIRC_DEVICE):
            threading.Thread(target=beacon_lirc, args=(LIRC_DEVICE, burst, IR_GAP_US, IR_BURSTS), daemon=True).start()
            log("IR: kernel sender %s, player code %d (%d us bursts)" % (LIRC_DEVICE, player, burst))
        else:
            try:
                pwm_dir, duty_ns, exported = start_pwm()
                ctx = multiprocessing.get_context("fork")
                beacon = ctx.Process(target=beacon_pwm, daemon=True,
                                     args=(str(pwm_dir / "duty_cycle"), duty_ns, burst, IR_GAP_US, IR_BURSTS))
                beacon.start()
                log("IR: flashing player code %d (%d us bursts)" % (player, burst))
            except (RuntimeError, OSError) as e:
                log("IR beacon OFF:", e)
                log("   That only matters if a weapon aims by IR. Camera aiming does not need it."
                    + ("  (Start with sudo to switch it on.)" if isinstance(e, PermissionError) else ""))

        import cv2
        import numpy as np
        from gpiozero import OutputDevice

        left = OutputDevice(cfg["left"], initial_value=False)
        left_reverse = OutputDevice(cfg["left_reverse"], initial_value=False)
        right = OutputDevice(cfg["right"], initial_value=False)
        right_reverse = OutputDevice(cfg["right_reverse"], initial_value=False)
        motors = [left, left_reverse, right, right_reverse]
        motor_lock = threading.Lock()

        game = Game(player, left, right, motor_lock)
        threading.Thread(target=game.listener, daemon=True).start()
        threading.Thread(target=game.lock_cue, daemon=True).start()
        viewer = None
        if VIEW_PORT:
            viewer = Viewer()
            threading.Thread(target=viewer.serve, daemon=True).start()

        state = {"zone": None, "remaining": 0, "next": 0.0}

        def pulse_for(label, x):                     # same rhythm as helmet.py
            if label == "LEFT":
                motor, distance = left, (0.425 - x) / 0.425
            elif label == "RIGHT":
                motor, distance = right, (x - 0.575) / 0.425
            else:
                state["zone"] = None
                return label
            count = max(1, min(5, ceil(distance * 5)))
            zone = (label, count)
            now = monotonic()
            if zone != state["zone"]:
                state["zone"], state["remaining"], state["next"] = zone, count, now
            if now >= state["next"]:
                with motor_lock:
                    try:
                        motor.on()
                        sleep(0.06 if count <= 3 else 0.12)
                    finally:
                        motor.off()
                if count <= 3:
                    state["remaining"] -= 1
                    if state["remaining"] == 0:
                        state["remaining"] = count
                        pause = 0.9
                    else:
                        pause = 0.10
                else:
                    pause = 0.30 if count == 4 else 0.12
                state["next"] = monotonic() + pause
            return "%s %d" % (label, count)

        detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, cfg["dict"])))
        locate = make_locator(cv2, np, tag_mm)
        log("Tag position: assuming the tag's black square is %g mm wide (x right, y up, z forward, in metres)." % tag_mm)
        next_xyz_print = 0.0
        next_xyz_send = 0.0
        no_tag = "No tag %d visible" % cfg["tag"]
        last_status = None
        last_cue = None
        next_cue = 0.0
        tilt_words = {"U": "aim UP", "D": "aim DOWN", "C": "level"}
        failures = 0
        warned = False

        while True:
            if camera is None:
                camera, cam_index = open_camera(cv2)
                if camera is None:
                    if not warned:
                        log("Camera not found. Check the USB cable. Retrying every 2 s...")
                        warned = True
                    sleep(2)
                    continue
                log("Camera running (/dev/video%d)." % cam_index)
                warned = False
                failures = 0

            success, frame = camera.read()
            if not success:
                failures += 1
                if failures >= 15:                   # about 1.5 s of nothing: the camera dropped out
                    log("Camera stopped answering. Reopening it...")
                    camera.release()
                    camera = None
                else:
                    sleep(0.1)
                continue
            failures = 0

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            corners, ids, _ = detector.detectMarkers(gray)
            height, width = frame.shape[:2]
            x, y, label, tilt = 0.5, 0.5, no_tag, "N"      # tilt: U = aim up, D = aim down, C = level, N = no tag
            where = None                             # (x, y, z) of the tag in metres
            if ids is not None:
                matches = [pts.reshape(4, 2) for pts, tag in zip(corners, ids.flatten()) if int(tag) == cfg["tag"]]
                matches = [pts for pts in matches if cv2.contourArea(pts) >= MIN_TAG_PX * MIN_TAG_PX]
                if matches:
                    pts = max(matches, key=cv2.contourArea)
                    x = pts[:, 0].mean() / width
                    y = pts[:, 1].mean() / height    # 0 = top of the picture, 1 = bottom
                    label = "LEFT" if x < 0.425 else "RIGHT" if x > 0.575 else "CENTER"
                    tilt = "U" if y < UP_DOWN_BAND[0] else "D" if y > UP_DOWN_BAND[1] else "C"
                    where = locate(pts, width, height)
            locked = label == "CENTER" and (tilt == "C" or not AIM_UPDOWN)

            now = monotonic()
            cue = "cue %d %s %d" % (player, tilt, 1 if locked else 0)
            if cue != last_cue or now >= next_cue:   # to this player's weapon: its motor plays up/down, its shots hit when locked
                game.send(cue)
                last_cue, next_cue = cue, now + CUE_SEND_S
            if locked:
                game.aim_until = now + 0.4           # fast ticks on both helmet motors: shoot now

            xyz_text = ""
            info = {"visible": tilt != "N", "player": player, "tag": cfg["tag"], "left_right": label if tilt != "N" else None,
                    "up_down": tilt_words.get(tilt), "on_target": locked, "time": round(time.time(), 2)}
            if where is not None:
                tx, ty, tz = where
                dist = sqrt(tx * tx + ty * ty + tz * tz)
                angle = degrees(atan2(tx, tz))       # + = to the right of straight ahead
                xyz_text = "x %+.2f  y %+.2f  z %.2f m" % (tx, ty, tz)
                if XYZ_PRINT_S and now >= next_xyz_print:
                    next_xyz_print = now + XYZ_PRINT_S
                    log("   tag %d at %s   (%.2f m away, %.0f deg %s)"
                        % (cfg["tag"], xyz_text, dist, abs(angle), "right" if angle >= 0 else "left"))
                if XYZ_SEND_S and now >= next_xyz_send:
                    next_xyz_send = now + XYZ_SEND_S
                    game.send("pos %d %.3f %.3f %.3f" % (player, tx, ty, tz))
                info.update(x=round(tx, 3), y=round(ty, 3), z=round(tz, 3), distance=round(dist, 3),
                            angle_deg=round(angle, 1), tag_size_mm=tag_mm)
            if viewer is not None:
                viewer.info = info

            status = pulse_for(label, x)
            if locked:
                status = "ON TARGET"
            elif tilt != "N":
                status = "%s, %s" % (status, tilt_words[tilt])
            if status != last_status:
                log("%s | x=%.3f y=%.3f" % (status, x, y))
                last_status = status

            if viewer is not None and viewer.wanted():       # only while a browser has the page open
                if ids is not None:
                    cv2.aruco.drawDetectedMarkers(frame, corners, ids)
                box = ((int(width * 0.425), int(height * UP_DOWN_BAND[0])), (int(width * 0.575), int(height * UP_DOWN_BAND[1])))
                colour = (0, 0, 255) if locked else (0, 255, 255)
                cv2.rectangle(frame, box[0], box[1], colour, 2 if locked else 1)
                cv2.putText(frame, "P%d  %s" % (player, status), (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, colour if locked else (0, 255, 0), 2)
                if xyz_text:
                    cv2.putText(frame, xyz_text, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                ok, image = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    viewer.publish(image.tobytes())

    except KeyboardInterrupt:
        log("\nStopping...")
    finally:
        if beacon is not None:
            beacon.terminate()
            beacon.join(timeout=1)
        stop_pwm(pwm_dir, exported)
        if camera is not None:
            camera.release()
        for m in motors:
            try:
                m.off()
                m.close()
            except Exception:
                pass
        log("Helmet stopped; IR and motors off.")


if __name__ == "__main__":
    main()