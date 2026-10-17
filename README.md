# WDGWars Mesh + ADS-B Uploader

Windows tray app that feeds live aircraft and LoRa mesh nodes to [WDGWars](https://wdgwars.pl), with a local dashboard and map.

## 📡 Description

It runs in the tray, watches whatever radios are plugged in, and uploads on a schedule. Everything it hears is kept in its own store, so nodes and aircraft that the radio has already forgotten still make it into the next upload.

- **ADS-B** from an [RTL-SDR](https://www.rtl-sdr.com/) decoded on the PC by [dump1090](https://github.com/gvanem/Dump1090), or from a [PortaPack running Mayhem](https://github.com/portapack-mayhem/mayhem-firmware), which decodes on-device and hands the log back over its USB console.
- **Mesh** from [Meshtastic](https://meshtastic.org/) and [MeshCore](https://meshcore.co.uk/) radios at the same time, over serial or BLE, read from the live packet stream rather than from the node list.
- **Wardrift** (optional): aircraft can also be sent to [Wardrift](https://wardrift.net) with a character API key.
- **Dashboard** on loopback: live feed, counters, a map, WDGWars stats and leaderboard, and the prerequisite checks.

## 🛠️ Notes

- The RTL-SDR has to be on the WinUSB driver before any decoder can open it — that is what [Zadig](https://zadig.akeo.ie/) is for.
- A radio's firmware caps how many nodes it will remember (80 on a Meshtastic nRF52 build, around 350 on MeshCore) and evicts the rest. The capture store is unbounded and survives restarts, so the count keeps climbing past that.
- The bundled dump1090 build takes the receiver position and tuner gain from its own `dump1090.cfg`, not the command line. Left alone it ships `homepos = -1,-1`, which puts every single-frame position off the coast of West Africa.
- The map is [OpenFreeMap](https://openfreemap.org/) — no key, no tile account.
- Aircraft details come from [adsbdb](https://www.adsbdb.com/) and photos from [Planespotters](https://www.planespotters.net/).
- The API keys live in `%APPDATA%\WDGWarsUploader\config.json`; they are never returned by the dashboard API or written to the log.

## 📦 Requirements

Windows, Python 3.11+, and at least one radio. An API key from your [WDGWars profile](https://wdgwars.pl/profile) is what the uploads are credited to.

## 🤝 Community

Join **The Pirates' Plunder** on Discord:

➡️ **[https://discord.gg/thepirates](https://discord.gg/thepirates)**

## 📄 License

[LICENSE](LICENSE)

<img width="1500" height="1000" alt="rocketgod_logo_transparent" src="https://github.com/user-attachments/assets/b14191a5-b509-44c4-be17-ad51e3226ec8" />
