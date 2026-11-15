import cv2
from time import monotonic, sleep
from math import ceil
from pathlib import Path
from gpiozero import OutputDevice

# ============================================================
# IR HARDWARE PWM CONFIGURATION
# Requires a working Linux PWM sysfs interface and root access.
# GPIO 18: hardware PWM, approximately 35 kHz, 50% duty cycle.
# ============================================================

PWM_ROOT = Path("/sys/class/pwm")
PWM_FREQUENCY = 35000
PWM_PERIOD_NS = round(1_000_000_000 / PWM_FREQUENCY)
PWM_DUTY_NS = PWM_PERIOD_NS // 2

pwm_dir = None
pwm_exported_by_script = False


def start_ir_carrier():
    global pwm_dir, pwm_exported_by_script

    chips = sorted(PWM_ROOT.glob("pwmchip*"))

    if not chips:
        raise RuntimeError(
            "No Linux PWM controller found at /sys/class/pwm. "
            "The PWM overlay alone may not expose a sysfs controller."
        )

    # GPIO 18 is normally PWM channel 0 on the standard Pi 4 mapping.
    # Verify the overlay's pin mapping before using this channel.
    chip = next(
        (c for c in chips if (c / "npwm").exists()
         and int((c / "npwm").read_text().strip()) >= 1),
        None
    )

    if chip is None:
        raise RuntimeError("No usable PWM channel found.")

    pwm_dir = chip / "pwm0"

    if not pwm_dir.exists():
        (chip / "export").write_text("0")
        pwm_exported_by_script = True

        # Wait briefly for the kernel to create the channel directory.
        import time
        for _ in range(50):
            if pwm_dir.exists():
                break
            time.sleep(0.02)

    if not pwm_dir.exists():
        raise RuntimeError(f"PWM channel was not created: {pwm_dir}")

    enable = pwm_dir / "enable"
    period = pwm_dir / "period"
    duty = pwm_dir / "duty_cycle"

    # Disable before changing timing.
    if enable.read_text().strip() == "1":
        enable.write_text("0")

    period.write_text(str(PWM_PERIOD_NS))
    duty.write_text(str(PWM_DUTY_NS))
    enable.write_text("1")

    print(
        f"IR PWM enabled: {PWM_FREQUENCY} Hz, "
        f"50% duty cycle, channel {pwm_dir}",
        flush=True
    )


def stop_ir_carrier():
    global pwm_dir, pwm_exported_by_script

    if pwm_dir is not None and pwm_dir.exists():
        try:
            (pwm_dir / "enable").write_text("0")
        except OSError as exc:
            print(f"Could not disable IR PWM: {exc}", flush=True)

        if pwm_exported_by_script:
            try:
                (pwm_dir.parent / "unexport").write_text("0")
            except OSError:
                pass

    print("IR PWM stopped.", flush=True)


# ============================================================
# MOTOR GPIO CONFIGURATION — BCM numbering
# ============================================================

left = OutputDevice(23, initial_value=False)
left_reverse = OutputDevice(24, initial_value=False)
right = OutputDevice(17, initial_value=False)
right_reverse = OutputDevice(27, initial_value=False)

active_zone = None
remaining = 0
next_pulse = 0.0


def pulse_for(label, x):
    global active_zone, remaining, next_pulse

    if label == "LEFT":
        motor = left
        distance = (0.425 - x) / 0.425
    elif label == "RIGHT":
        motor = right
        distance = (x - 0.575) / 0.425
    else:
        active_zone = None
        return label

    count = max(1, min(5, ceil(distance * 5)))
    zone = (label, count)
    now = monotonic()

    if zone != active_zone:
        active_zone = zone
        remaining = count
        next_pulse = now

    if now >= next_pulse:
        try:
            motor.on()
            sleep(0.06 if count <= 3 else 0.12)
        finally:
            motor.off()

        if count <= 3:
            remaining -= 1
            if remaining == 0:
                remaining = count
                pause = 0.9
            else:
                pause = 0.10
        else:
            pause = 0.30 if count == 4 else 0.12

        next_pulse = monotonic() + pause

    return f"{label} {count}"


# ============================================================
# MAIN PROGRAM
# ============================================================

camera = None

try:
    # Start the continuous IR carrier.
    start_ir_carrier()

    camera = cv2.VideoCapture(0, cv2.CAP_V4L2)
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not camera.isOpened():
        raise RuntimeError("Could not open the webcam.")

    dictionary = cv2.aruco.getPredefinedDictionary(
        cv2.aruco.DICT_APRILTAG_16h5
    )
    detector = cv2.aruco.ArucoDetector(dictionary)

    print(
        "Camera running; continuous IR carrier active. "
        "Press Ctrl+C to quit.",
        flush=True
    )

    last_status = None

    while True:
        success, frame = camera.read()

        if not success:
            print("Camera read failed.", flush=True)
            sleep(0.1)
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, ids, _ = detector.detectMarkers(gray)

        x = 0.5
        label = "No tag 0 visible"

        if ids is not None:
            matches = [
                points.reshape(4, 2)
                for points, tag in zip(corners, ids.flatten())
                if int(tag) == 0
            ]

            if matches:
                points = max(matches, key=cv2.contourArea)
                x = points[:, 0].mean() / frame.shape[1]

                if x < 0.425:
                    label = "LEFT"
                elif x > 0.575:
                    label = "RIGHT"
                else:
                    label = "CENTER"

        status = pulse_for(label, x)

        if status != last_status:
            print(f"{status} | x={x:.3f}", flush=True)
            last_status = status

except KeyboardInterrupt:
    print("\nStopping camera...", flush=True)

finally:
    if camera is not None:
        camera.release()

    # Disable the IR carrier.
    stop_ir_carrier()

    # Ensure all motor outputs are off.
    for motor in (left, left_reverse, right, right_reverse):
        motor.off()
        motor.close()

    print("Program stopped; motor outputs turned off.", flush=True)