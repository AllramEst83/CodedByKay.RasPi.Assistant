# Pi setup, connection and reset

## Hardware seen in photos

Raspberry Pi 5 (board silk-screen), official red/white case with fan and heatsink, Kingston Canvas Select Plus 128 GB microSD (A1/U1), official Raspberry Pi 27 W USB-C supply (model P4123, EU, 5.1 V/5 A, confirmed by photo).

## Ports (Pi 5, case end view)

| Port | Use here |
|---|---|
| USB-C (short edge, left of the two micro-HDMI) | Power only, official 27 W PSU |
| RJ45 Ethernet (long edge) | Optional wired LAN; Wi-Fi works too |
| 2 × USB 3 (blue) | USB mic / speaker or headset |
| 2 × USB 2 (black) | Spare |
| 2 × micro-HDMI | Optional monitor for first-boot debugging; not needed headless |

The Pi is not connected to the PC by a port. Both join the same router; the PC reaches the Pi by `<hostname>.local` or its IP from the router's client list.

## Step-by-step: reflash and connect (Windows PC)

**You need:** the Pi with its power supply, the microSD card, a way to read it on the PC (SD-card slot with the microSD adapter, or a USB card reader), and your Wi-Fi name and password (or a network cable to the router).

### A. Flash the card
1. Download and install **Raspberry Pi Imager** from raspberrypi.com/software.
2. Take the microSD card out of the Pi (power off first) and put it in the PC.
3. Open Imager and choose:
   - **Device:** Raspberry Pi 5
   - **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**
   - **Storage:** the 128 GB card. Check the size and drive letter; the next step erases it.
4. Fill in the customisation screens:
   - Hostname: `gemini-pi`
   - Localisation: time zone `Europe/Stockholm`, keyboard `se`
   - Username and password: pick your own and note them (the old `pi` user is not preset)
   - Wi-Fi: your SSID and password, country `SE` (skip if you use the cable)
   - Services: **enable SSH**, password authentication
5. Write and confirm the erase. Wait for "verified", then eject the card.

### B. Boot the Pi
1. Insert the card in the Pi. The slot is on the underside of the board at the end opposite the USB/Ethernet ports; contacts face the board.
2. Optional: plug a network cable from the Pi to your router.
3. Connect the USB-C power supply. Wait 2–3 minutes for the first boot; the green LED flickers while it works.

### C. Connect with PuTTY
1. Find the Pi: in PowerShell run `ping gemini-pi.local`. If it fails, open your router's admin page and read the IP of `gemini-pi` in the client list.
2. Open PuTTY: Host Name `gemini-pi.local` (or the IP), Port `22`, Connection type SSH. Type a name under Saved Sessions and click Save so you can reuse it.
3. Click Open. On the first connection accept the host-key prompt.
4. Log in with your username and password. Nothing shows while you type the password; that is normal.
5. Verify: `hostname -I`, `uname -m` (expect `aarch64`), `cat /etc/os-release`.
6. Update: `sudo apt update && sudo apt full-upgrade -y`, then `sudo reboot` and reconnect.

Windows alternative to PuTTY: `ssh <username>@gemini-pi.local` in PowerShell.

### D. If it goes wrong
- Host not found: wait another minute, then try the IP from the router, or use the network cable instead of Wi-Fi.
- PuTTY warns that the host key changed ("potential security breach"): expected if this IP or name was the old Docker server; accept the new key. In Windows `ssh`, run `ssh-keygen -R gemini-pi.local` first.
- Wrong Wi-Fi details: reflash with corrected settings, or use the cable.

Next: plan.md sections 3.2–6 (audio packages, Python venv, `.env`, `assistant.py`) once the USB mic and speaker have arrived.

## Audio check

`arecord -l` and `aplay -l` list devices; test with `arecord -d 3 -f S16_LE -r 16000 test.wav && aplay test.wav`; tune with `alsamixer`.
