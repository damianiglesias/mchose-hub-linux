# MCHOSE Linux Hub

A GTK4/libadwaita application to configure MCHOSE gaming mice on Linux — DPI, polling rate, lift-off distance, debounce, sleep, RGB and battery level. No Windows driver, no browser required.

> Developed and tested on an **MCHOSE A7 V2 Pro** (`5253:1020`, 2.4G 8K dongle). Other models in the L7 / M7 / A7 / K7 family should work — open an issue with the output of `mchose-hub list --all` if yours does not appear.

## Requirements

- Linux with GTK 4 + libadwaita 1
- Python ≥ 3.9
- [`mchose-linux`](https://github.com/alexfrih/mchose-linux) installed (provides the HID backend)

## Installation

```bash
# 1. Install the HID backend (once)
git clone https://github.com/alexfrih/mchose-linux ~/mchose-linux
cd ~/mchose-linux && ./install.sh

# 2. Install this application
git clone https://github.com/YOUR_USERNAME/mchose-linux-hub
cd mchose-linux-hub
bash install.sh
```

The installer handles Python dependencies, udev rules (hardware access without root) and creates a desktop entry in your application launcher.

## Usage

Open **MCHOSE Linux Hub** from the application menu, or run:

```bash
mchose-hub
```

If the dongle is unresponsive on startup, unplug it and plug it back in. The app retries automatically.

## Features

- **DPI**: 6 profiles from 100 to 26 000 DPI, active profile selector
- **Polling rate**: up to 8000 Hz (wireless and wired independently)
- **Sensor**: LOD (1/2 mm), ripple control, angle snap, motion sync
- **Debounce**: 0–20 ms
- **Sleep**: configurable inactivity timeout
- **Battery**: live percentage
- **Hot-plug**: automatic detection on connect/disconnect

## Credits

The HID protocol was reverse-engineered by [alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux), which also provides the CLI backend. This project adds the GTK4/libadwaita graphical interface on top of it.

## License

MIT
