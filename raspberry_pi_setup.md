# Raspberry Pi OS Setup Guide

This document covers OS installation, first-boot updates, and all system/
Python dependencies required to run this project on a Raspberry Pi Zero W.
For wiring, pin mapping, and software architecture, see [readme.md](readme.md).

---

## 1. Requirements

- Raspberry Pi Zero W
- microSD card (8GB+, Class 10/A1 recommended)
- A PC with an SD card reader
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/) installed on that PC
- PS4 DualShock 4 controller

> **Architecture note:** the Pi Zero W uses an ARM11 (armv6) CPU. It only
> supports **32-bit** Raspberry Pi OS — do not flash a 64-bit image, it will
> not boot on this board.

---

## 2. Flash Raspberry Pi OS

1. Open Raspberry Pi Imager on your PC.
2. **Choose Device** → Raspberry Pi Zero W.
3. **Choose OS** → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (32-bit)**.
   The Lite image is recommended since this project runs headless (no
   desktop needed); it also boots faster and leaves more RAM free on the
   Zero W's 512MB.
4. **Choose Storage** → select your microSD card.
5. Click the gear icon (⚙ *Edit Settings*) before writing, and configure:
   - **Hostname** (e.g. `rc-tank.local`)
   - **Enable SSH** → use password or your public key
   - **Set username and password**
   - **Configure Wi‑Fi** (SSID/password + correct country code)
   - **Set locale/timezone/keyboard layout**
6. Write the image and boot the Pi from it.

---

## 3. First Boot and System Update

Connect over SSH (replace with your hostname/IP):

```bash
ssh <username>@rc-tank.local
```

Update the OS and firmware before installing anything else:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Reconnect after reboot and confirm the kernel/firmware are current:

```bash
uname -a
vcgencmd version
```

---

## 4. Enable and Configure Bluetooth (PS4 Controller)

Bluetooth is enabled by default on Raspberry Pi OS. Confirm the service is
running:

```bash
sudo systemctl status bluetooth
```

Pair the DS4 controller (put it into pairing mode by holding **PS + Share**
until the light bar flashes rapidly):

```bash
sudo bluetoothctl
power on
agent on
default-agent
scan on
# wait for a line like: [NEW] Device XX:XX:XX:XX:XX:XX Wireless Controller
pair   XX:XX:XX:XX:XX:XX
trust  XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
scan off
exit
```

The controller should reconnect automatically on future boots once trusted,
as long as it's powered on within Bluetooth range.

---

## 5. Install System Packages

```bash
sudo apt update
sudo apt install -y \
  git \
  python3-pip \
  python3-venv \
  python3-pygame \
  python3-rpi.gpio
```

Notes:

- Installing `python3-pygame` and `python3-rpi.gpio` via `apt` (rather than
  `pip`) is strongly recommended on the Pi Zero W: both ship as
  prebuilt `armhf` packages, whereas `pip install pygame` on this CPU may
  attempt a slow source build and require extra SDL2 development headers.
- If you specifically need a newer `pygame` version than the one in `apt`,
  install the SDL2 build dependencies first:

  ```bash
  sudo apt install -y libsdl2-dev libsdl2-image-dev libsdl2-mixer-dev \
    libsdl2-ttf-dev libportmidi-dev libjpeg-dev python3-setuptools
  ```

---

## 6. Get the Project Code

```bash
cd ~
git clone <this-repository-url> rc_tank
cd rc_tank
```

(Or copy the project files over with `scp`/`rsync` if you're not using git.)

---

## 7. Python Environment

Recent Raspberry Pi OS releases (Bookworm and newer) mark the system Python
as "externally managed" (PEP 668), which blocks plain `pip install` at the
system level. Since this project relies on `python3-rpi.gpio` and
`python3-pygame` installed via `apt` (step 5), create a virtual environment
that can still see those system packages instead of reinstalling them:

```bash
python3 -m venv --system-site-packages ~/rc_tank/.venv
source ~/rc_tank/.venv/bin/activate
```

Activate this environment (`source ~/rc_tank/.venv/bin/activate`) in every
new shell before running the scripts, or reference the venv's Python
directly (`~/rc_tank/.venv/bin/python3 tank_ps4_control.py`).

If you prefer not to use a venv and just need the two dependencies without
`apt`, the system-level equivalent is:

```bash
pip install --break-system-packages pygame RPi.GPIO
```

---

## 8. GPIO and Joystick Permissions

The default `pi`/first-run user created by Raspberry Pi Imager is already a
member of the `gpio` and `input` groups needed to access GPIO and joystick
devices without `sudo`. Verify with:

```bash
groups
```

You should see `gpio` (and ideally `input`, `dialout`) listed. If a custom
user was created without these groups:

```bash
sudo usermod -aG gpio,input,dialout <username>
# then log out and back in for group membership to take effect
```

---

## 9. Verify the Setup

```bash
cd ~/rc_tank
source .venv/bin/activate   # if using a venv
python3 ps4_controller_test.py
```

Move the sticks and press buttons — the live telemetry line should update.
Press `Ctrl+C` to stop, then run the full drive script:

```bash
python3 tank_ps4_control.py
```

See [readme.md](readme.md#8-testing-and-validation-procedure) for the bench
validation checklist before driving on tracks.

---

## 10. Optional: Run Automatically on Boot (systemd)

Create a service unit:

```bash
sudo tee /etc/systemd/system/rc-tank.service > /dev/null <<'EOF'
[Unit]
Description=RC Tank PS4 Drive Control
After=bluetooth.target network.target

[Service]
Type=simple
User=<username>
WorkingDirectory=/home/<username>/rc_tank
ExecStart=/home/<username>/rc_tank/.venv/bin/python3 tank_ps4_control.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rc-tank.service
sudo journalctl -u rc-tank.service -f   # view live logs
```

---

## 11. Optional: Power Saving / Disable Unused Peripherals

See the **"Optional: Peripherals to Disable (Power Saving)"** section in
[readme.md](readme.md#10-optional-peripherals-to-disable-power-saving) for
the `/boot/firmware/config.txt` overlay options (disabling HDMI, onboard
audio, unused buses, etc.). Keep Bluetooth enabled since it's required for
the PS4 controller link.

---

## 12. Troubleshooting

| Symptom | Likely Cause | Action |
|---------|--------------|--------|
| `error: externally-managed-environment` on `pip install` | PEP 668 protection on Bookworm+ | Use a venv (`--system-site-packages`) or `pip install --break-system-packages` |
| `pip install pygame` hangs / takes very long | Building SDL2 from source on armv6 | Use `sudo apt install python3-pygame` instead (step 5) |
| Controller won't pair | Not in pairing mode, or stale pairing entry | Hold PS+Share until light bar flashes fast; `bluetoothctl remove <MAC>` then re-pair |
| `Permission denied` accessing GPIO/joystick | User not in `gpio`/`input` group | `sudo usermod -aG gpio,input <username>`, then re-login |
| Wi‑Fi/SSH unreachable after flashing | Wi‑Fi country code not set, or wrong credentials in Imager | Re-flash with Imager's advanced settings, double-check SSID/password/country |
| Service fails to start under systemd but works interactively | Wrong `WorkingDirectory`/`ExecStart` path, or venv not referenced | Confirm paths in the unit file match your actual install location |
