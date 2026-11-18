# MCHOSE Linux Hub

A GTK4/libadwaita application to configure MCHOSE gaming mice on Linux — DPI, polling rate, lift-off distance, debounce, sleep and battery level. No Windows driver, no browser required.

## Compatibility

This app uses [alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux) as its HID backend. Any mouse that works with that CLI will work here too:

**L7** · **L7 Pro** · **L7 Ultra** · **L7 Pro+** · **M7** · **M7 Pro** · **M7 Ultra** · **A7** · **A7 Pro** · **A7 Ultra** · **A7 V2** · **K7 Ultra** · **AX5** · **A5**

Developed and tested on an **MCHOSE A7 V2 Pro** (`5253:1020`, 2.4G 8K dongle).

Not supported: the G3 family (different protocol).

## Requirements

- Linux with GTK 4 + libadwaita 1
- Python ≥ 3.9
- [alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux) installed

## Installation

```bash
# 1. Install the HID backend
git clone https://github.com/alexfrih/mchose-linux ~/mchose-linux
cd ~/mchose-linux && ./install.sh

# 2. Install this app
git clone https://github.com/damianiglesias/mchose-linux-hub
cd mchose-linux-hub
bash install.sh
```

The installer handles Python dependencies, udev rules and creates a desktop entry.

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

HID protocol reverse-engineered by [alexfrih/mchose-linux](https://github.com/alexfrih/mchose-linux). This project adds the GTK4/libadwaita GUI on top of it.

## License

MIT
