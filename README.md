# MCHOSE Linux Hub

Gestor de periféricos MCHOSE para Linux en **espacio de usuario** (sin módulos de kernel): interfaz GTK4/libadwaita,
motor HID dirigido por **perfiles JSON** y un instalador que configura permisos udev para no usar `root`.

> **Estado, con honestidad.** Esto es el *framework* completo más dos **plantillas**. Los bytes de
> `profiles/*_template.json` están inventados para ilustrar el formato: **no hay protocolos reales de MCHOSE
> verificados** (no tengo acceso a hardware ni encontré documentación pública). Para que un modelo funcione hay
> que capturar sus payloads (§5) y escribir su perfil. Los VID `0x320F` y `0x1915` de las reglas udev son
> candidatos sin verificar; comprueba el tuyo con `mchose-hub list --all` o `lsusb`.
>
> Probado: 31 tests automáticos (sin hardware), instalador completo en Ubuntu 24.04, GUI en modo demo sobre Xvfb
> (controles, reposo, desconexión, reconexión). **No probado:** hardware real, otras distribuciones, Wayland nativo.

## 1. Arquitectura

```
 ┌─────────────── GUI · gui.py (GTK4 + libadwaita) ───────────────┐
 │ genera los controles leyendo profile.features                  │
 │ Worker (hilo HID)  ⇄  GLib.idle_add (hilo de GTK)              │
 └───────────────────────────┬────────────────────────────────────┘
                             │ device.execute("set_dpi", dpi=1600)
 ┌───────────────────────────▼──────── Backend ──────────────────┐
 │ device.py    MchoseDevice: abrir/escribir/leer, reintentos,    │
 │              detección de reposo, reapertura tras fallos       │
 │ protocol.py  comando + valores ⇄ frame de bytes (puro, sin E/S)│
 │ hotplug.py   eventos pyudev (o sondeo) → «algo cambió»         │
 └───────────────────────────┬────────────────────────────────────┘
                             │ lee
 ┌───────────────────────────▼──────── Datos ────────────────────┐
 │ profiles/*.json   VID/PID · transporte · comandos · controles  │
 └────────────────────────────────────────────────────────────────┘
   udevgen.py: perfiles → /etc/udev/rules.d/70-mchose-hub.rules
```

- **Lenguaje: Python.** PyGObject (GTK4/libadwaita) y `hidapi` están empaquetados en las distros principales, así
  que `install.sh` no compila nada; con Rust habría un binario único, pero GTK4-rs y el empaquetado multi-distro
  encarecen justo la parte «fácil de instalar». El motor no depende de GTK, así que se podría portar.
- **Nada específico de MCHOSE en el código:** todo byte vive en un perfil. Añadir un modelo = añadir un `.json`.
- **Flujo:** hot-plug → `discover()` empareja interfaces HID con perfiles → la GUI crea una página por dispositivo →
  cada control llama a `execute()` en un hilo trabajador → `protocol.build_frame()` → `hidapi`.
- **Interfaz correcta:** un teclado expone varias interfaces HID. El perfil fija `usage_page`/`interface_number`;
  sin ellos sólo se usan interfaces «vendor defined» (≥ `0xFF00`). Así nunca se escribe en la interfaz que teclea.
- **Robustez:** `DeviceAsleepError` (escribe bien pero no responde: reposo/fuera de alcance), `DeviceDisconnectedError`
  y `DeviceAccessError` (permisos). Tras un fallo de E/S se reabre el nodo (la ruta `/dev/hidrawN` puede cambiar).

## 2. Estructura de un perfil (`profiles/mchose_mouse_template.json`)

| Sección | Contenido |
|---|---|
| `device` | `id`, `name`, `kind` (`mouse`/`keyboard`), `enabled`, `verified`, `author`, `notes` |
| `match` | `ids` (lista `"VID:PID"` en hex, como `lsusb`: el cable y el dongle suelen tener **VID distintos**) o `vendor_id` + `product_ids`; `usage_page`, `usage`, `interface_number` |
| `transport` | `write_mode` (`feature_report`/`output_report`), `read_mode` (`feature_report`/`input_report`/`none`), `report_id`, `report_length` (total, con Report ID), tiempos, `retries`, `checksum` (`sum8`/`sub8`/`xor8`) |
| `commands` | por comando: `request` (hex y `{param}`), `params`, `response` (`expect` + `fields`), `then` (comandos de confirmación) |
| `features` | controles de la GUI: `choice`, `slider`, `color`, `switch`, `battery`, `mapping` (se infieren del parámetro) |

Parámetros: `type` (`u8`, `u16le/be`, `u32le/be`, `rgb`, `bool`), `min/max/step`, `default`, `linear`
(`crudo = valor·mul/div + add`) y `choices` con su byte crudo (`wire`). Ejemplo real de la plantilla: `set_dpi` con
`"request": "07 {slot} {dpi}"` y `linear {1, 50, −1}` envía 1600 DPI como `08 07 00 1f 00 …` + checksum.
Los offsets de checksum/respuesta cuentan desde el byte 0 (el Report ID). Los perfiles YAML también valen si hay PyYAML.

## 3. Instalación y uso

```bash
unzip mchose-linux-hub.zip && cd mchose-linux-hub
bash install.sh            # pide sudo; --no-deps, --group NOMBRE, --uninstall
mchose-hub --demo          # prueba la interfaz sin hardware
mchose-hub                 # GUI (o «MCHOSE Linux Hub» en el menú)
```

`install.sh`: detecta apt/dnf/pacman/zypper, instala GTK 4 + libadwaita + hidapi, verifica los imports (si falta
`hid`, crea un venv en `/opt/mchose-linux-hub/venv`), instala la app, genera y recarga las reglas udev y crea el
`.desktop`. Es idempotente. Las reglas usan `TAG+="uaccess"`: acceso sólo para el usuario con sesión local activa
(sin `MODE=0666`). Sin systemd-logind: `--group plugdev`. Si el dispositivo ya estaba conectado y no tienes acceso,
reconéctalo.

CLI útil: `mchose-hub list --all` · `mchose-hub inspect` · `mchose-hub validate` · `mchose-hub send ID COMANDO k=v` ·
`mchose-hub udev [--install]`.

## 4. Añadir un dispositivo

La GUI **siempre muestra los MCHOSE conectados**, tengan perfil o no (el botón «Todos los HID» muestra además el
resto). Un dispositivo sin perfil aparece con sus interfaces, el estado de permisos y los reports que declara su
descriptor.

1. Abre la GUI (o `mchose-hub inspect`) y localiza tu dispositivo. Si faltan permisos, pulsa **Dar permisos**
   (usa `pkexec`) o ejecuta `sudo mchose-hub udev --install`: las reglas incluyen automáticamente los VID de los
   MCHOSE conectados y **acumulan** los anteriores (cable y dongle).
2. Pulsa **Crear perfil base**: guarda `~/.config/mchose-hub/profiles/<modelo>.json` con el VID:PID, la interfaz del
   fabricante y un `transport` deducido del descriptor. Conecta también el dongle y repite: se añade al mismo perfil.
3. Captura los comandos (§5) y añade `commands` y `features` (mira las plantillas). `mchose-hub validate`.
4. Reinicia la app y prueba con `mchose-hub send ID COMANDO k=v` antes de usar la GUI.

La GUI escribe ajustes pero no lee su estado actual (sólo lo que definas con comandos `get_*`, como la batería).
Macros y reasignación de teclas completas quedan fuera de la GUI base: el motor ya envía cualquier comando
(`remap_key` en la plantilla de teclado) y se pueden añadir widgets en `gui.py`.

## 5. Guía de sniffing (Wireshark/USBPcap en una VM de Windows)

Crea una VM Windows 10/11 (virt-manager/QEMU-KVM o VirtualBox) y pásale el dispositivo por USB (*Add Hardware → USB Host Device*, o un filtro USB por VID:PID); si es inalámbrico, pasa el **dongle 2.4 GHz** y captura con el periférico enlazado a él, y repite con el cable si usa otro PID. Instala en la VM el software oficial de MCHOSE y Wireshark con USBPcap, y captura sobre el *USBPcap root hub* donde cuelga el dispositivo. Cambia **un solo ajuste cada vez** (p. ej. DPI 800 → 1600), anota la hora y para la captura; filtra el tráfico del host al dispositivo con datos (`usb.src == "host" && usb.capdata`; los *Set Report* son transferencias de control con `bRequest 0x09` y el nombre del campo de datos varía según la versión de Wireshark). Compara capturas que difieren en un solo ajuste: los bytes que cambian son el parámetro (y el checksum; prueba si el último byte es suma, resta o xor de los anteriores: `sum8`, `sub8`, `xor8`), y fíjate en el Report ID, la longitud y si el programa lee una respuesta. Vuelca esos valores en `transport` y `commands` del JSON. Captura sólo ajustes de usuario: **no reproduzcas actualizaciones de firmware ni comandos que no entiendas** (un modo bootloader/DFU puede dejar el dispositivo inservible), y prueba primero con valores seguros.

## 6. Límites y seguridad

- Las reglas `uaccess` por VID dan acceso a **todas** las interfaces HID de ese VID al usuario de la sesión, incluida la que teclea. Es el compromiso habitual de estas herramientas; quita de `KNOWN_VIDS` (`udevgen.py`) los VID que no uses.
- La reingeniería para interoperar con tu propio hardware es habitual; revisa la legislación de tu país y no distribuyas software o firmware propietario.
- `mchose-hub udev` incluye los VID candidatos por defecto; `--no-known` los omite.

## 7. Tests

```bash
python3 -m unittest discover -s tests -v
```
