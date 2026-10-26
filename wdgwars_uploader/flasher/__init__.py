"""Desktop WardriveESP flasher: one click flashes a board, and both chips of a
Biscuit Pro / Ultra.

* :mod:`.ports`    finds ESP32 boards (COM ports, WinUSB C5s) and pairs a Biscuit's chips
* :mod:`.probe`    reads what a board runs without resetting it
* :mod:`.firmware` version.json and the checked images from rocketgod.dev
* :mod:`.engine`   ROM loader sessions through esptool
* :mod:`.service`  the job runner behind the dashboard panel
"""

from .service import FlasherService

__all__ = ["FlasherService"]
