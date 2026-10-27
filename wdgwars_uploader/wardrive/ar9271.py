"""Atheros AR9271 monitor mode over libusb (ath9k_htc firmware; Alfa AWUS036NHA,
TP-Link TL-WN722N v1 and other AR9271 adapters).

A line-for-line port of the Wardrive Go Android driver (usb/Ar9271Loader.kt,
usb/Ar9271Init.kt and usb/MonitorCapture.kt, plus the AR9271 engage flow in
MainActivity.kt). Same register writes, same order, same sequence numbers, same
firmware handling:

  * probe-first: a live GET_DESCRIPTOR tells the ROM bootloader (bcdDevice
    0x0108) from running firmware (0xFFFF). A WMI packet sent to a bare
    bootloader poisons it, so the WMI reuse probe only runs when the chip is not
    known to be a bootloader.
  * the firmware (htc_9271.fw) is uploaded only to a true bootloader, once, then
    the chip boots and its one-shot HTC READY is read off interrupt IN 0x83.
    Endpoint halts are never cleared (that flushes HTC READY).
  * running firmware is adopted (reuse): no chip reset, no PLL, no INI rewrite,
    just the RX re-arm cycle and hopping, with the stall safety net.
  * the full ath9k_hw bring-up otherwise: wake, AR9271 first-boot radio reset,
    warm reset, PLL, INI tables, ANI, EEPROM 4K board values, channel 6,
    interrupt masks, AGC + self-measured noise floor calibration, AR_CFG byte
    swap, FLUSH/SET_MODE/ATH_INIT/START_RECV, VAP/NODE, two re-arm passes.
  * RX: eight 16 KB transfers kept in flight on bulk IN 0x82 (the hif_usb
    model), resubmitted before parsing; hopping 2.4 GHz channels 1-13 with a
    500 ms dwell.

The adapter has to be on the WinUSB driver for libusb to open it. A wedged
AR9271 can only be recovered by a physical replug (a USB reset cannot drop the
firmware), so a failed upload or a missing HTC READY raises NeedsReplug, a
usb1.USBErrorTimeout, which the monitor manager reports as "needs replug".
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import usb1

log = logging.getLogger("wdgwars.ar9271")

VID_PIDS = [(0x0CF3, 0x9271), (0x0CF3, 0x1006)]
FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"
FIRMWARE = "htc_9271.fw"

# ===== Ar9271Loader.kt =====
FW_REQ_TYPE = 0x40            # vendor | host->device | device recipient
FW_DOWNLOAD = 0x30
FW_DOWNLOAD_COMP = 0x31
FW_ADDR = 0x501000            # AR9271_FIRMWARE (download target)
FW_TEXT_ADDR = 0x903000       # AR9271_FIRMWARE_TEXT (execute)
FW_BLOCK = 4096
FW_BOOT_WAIT_S = 3.5          # MainActivity engage: delay(3500) after the upload

EP_RX = 0x82                  # bulk IN, 802.11 RX stream
EP_REG_IN = 0x83              # interrupt IN, HTC control / WMI replies (HTC READY arrives here)
EP_REG_OUT = 0x04             # interrupt OUT, WMI commands

BCD_BOOTLOADER = 0x0108
BCD_FIRMWARE = 0xFFFF

# ===== MonitorCapture.kt =====
# 0xffff plus CONTROL_WRAPPER (0x80000) and 4ADDRESS (0x100000, WDS / mesh / repeater).
RX_FILTER_MONITOR = 0x0018FFFF
HOP_CHANNELS = (1, 6, 11, 3, 9, 2, 7, 13, 4, 10, 5, 8, 12)   # all of 2.4 GHz, 1/6/11 first
HOP_DWELL_S = 0.5
REUSE_STALL_S = 4.0           # reuse sessions: a hop followed by no RX frame for this long parks hopping
HOLD_S = 3.0                  # hold a failed write through a hiccup for this long
HTC_RX_FRAME_HEADER_SIZE = 40
RX_URBS = 8                   # MAX_RX_URB_NUM in ath9k_hif_usb; the only value proven on Android
RX_URB_SIZE = 16384
RSSI_NOISE_REF = 95           # rs_rssi -> dBm (ath9k noise reference), as the Android feeder does

# Kept off on Android: live 0x2000 SEEPROM reads at these points dropped this adapter.
ANT_EEPROM_READ = False
ANT_EEPROM_EARLY = False

# ar9271Modes_9271_ANI_reg, modesIndex=4 (2 GHz HT20)
ANI_REGS = (
    (0x9850, 0x6D4000E2), (0x985C, 0x3137605E), (0x9858, 0x7EC84D2E), (0x986C, 0x06903881),
    (0x9868, 0x5AC640D0), (0x9924, 0xD00A800D), (0x99C0, 0x05EEA6D4),
)

# Data services connected after the WMI service: BEACON, CAB, UAPSD, MGMT, DATA_BE, BK, VI, VO
DATA_SERVICES = (0x0101, 0x0102, 0x0103, 0x0104, 0x0107, 0x0108, 0x0106, 0x0105)


# ===== Ar9271Init.kt: AR9271 2.4 GHz HT20 init tables, verbatim from ath9k ar9002_initvals.h =====
# Ar9271Init.common: 325 registers
INI_COMMON = (
    (0x000c, 0x00000000), (0x0030, 0x00020045), (0x0034, 0x00000005), (0x0040, 0x00000000),
    (0x0044, 0x00000008), (0x0048, 0x00000008), (0x004c, 0x00000010), (0x0050, 0x00000000),
    (0x0054, 0x0000001f), (0x0800, 0x00000000), (0x0804, 0x00000000), (0x0808, 0x00000000),
    (0x080c, 0x00000000), (0x0810, 0x00000000), (0x0814, 0x00000000), (0x0818, 0x00000000),
    (0x081c, 0x00000000), (0x0820, 0x00000000), (0x0824, 0x00000000), (0x1040, 0x002ffc0f),
    (0x1044, 0x002ffc0f), (0x1048, 0x002ffc0f), (0x104c, 0x002ffc0f), (0x1050, 0x002ffc0f),
    (0x1054, 0x002ffc0f), (0x1058, 0x002ffc0f), (0x105c, 0x002ffc0f), (0x1060, 0x002ffc0f),
    (0x1064, 0x002ffc0f), (0x1230, 0x00000000), (0x1270, 0x00000000), (0x1038, 0x00000000),
    (0x1078, 0x00000000), (0x10b8, 0x00000000), (0x10f8, 0x00000000), (0x1138, 0x00000000),
    (0x1178, 0x00000000), (0x11b8, 0x00000000), (0x11f8, 0x00000000), (0x1238, 0x00000000),
    (0x1278, 0x00000000), (0x12b8, 0x00000000), (0x12f8, 0x00000000), (0x1338, 0x00000000),
    (0x1378, 0x00000000), (0x13b8, 0x00000000), (0x13f8, 0x00000000), (0x1438, 0x00000000),
    (0x1478, 0x00000000), (0x14b8, 0x00000000), (0x14f8, 0x00000000), (0x1538, 0x00000000),
    (0x1578, 0x00000000), (0x15b8, 0x00000000), (0x15f8, 0x00000000), (0x1638, 0x00000000),
    (0x1678, 0x00000000), (0x16b8, 0x00000000), (0x16f8, 0x00000000), (0x1738, 0x00000000),
    (0x1778, 0x00000000), (0x17b8, 0x00000000), (0x17f8, 0x00000000), (0x103c, 0x00000000),
    (0x107c, 0x00000000), (0x10bc, 0x00000000), (0x10fc, 0x00000000), (0x113c, 0x00000000),
    (0x117c, 0x00000000), (0x11bc, 0x00000000), (0x11fc, 0x00000000), (0x123c, 0x00000000),
    (0x127c, 0x00000000), (0x12bc, 0x00000000), (0x12fc, 0x00000000), (0x133c, 0x00000000),
    (0x137c, 0x00000000), (0x13bc, 0x00000000), (0x13fc, 0x00000000), (0x143c, 0x00000000),
    (0x147c, 0x00000000), (0x4030, 0x00000002), (0x403c, 0x00000002), (0x4024, 0x0000001f),
    (0x4060, 0x00000000), (0x4064, 0x00000000), (0x8004, 0x00000000), (0x8008, 0x00000000),
    (0x800c, 0x00000000), (0x8018, 0x00000700), (0x8020, 0x00000000), (0x8038, 0x00000000),
    (0x803c, 0x00000000), (0x8048, 0x00000000), (0x8054, 0x00000000), (0x8058, 0x00000000),
    (0x805c, 0x000fc78f), (0x8060, 0x0000000f), (0x8064, 0x00000000), (0x8070, 0x00000000),
    (0x80b0, 0x00000000), (0x80b4, 0x00000000), (0x80b8, 0x00000000), (0x80bc, 0x00000000),
    (0x80c0, 0x2a80001a), (0x80c4, 0x05dc01e0), (0x80c8, 0x1f402710), (0x80cc, 0x01f40000),
    (0x80d0, 0x00001e00), (0x80d4, 0x00000000), (0x80d8, 0x00400000), (0x80e0, 0xffffffff),
    (0x80e4, 0x0000ffff), (0x80e8, 0x003f3f3f), (0x80ec, 0x00000000), (0x80f0, 0x00000000),
    (0x80f4, 0x00000000), (0x80f8, 0x00000000), (0x80fc, 0x00020000), (0x8100, 0x00020000),
    (0x8104, 0x00000001), (0x8108, 0x00000052), (0x810c, 0x00000000), (0x8110, 0x00000168),
    (0x8118, 0x000100aa), (0x811c, 0x00003210), (0x8120, 0x08f04810), (0x8124, 0x00000000),
    (0x8128, 0x00000000), (0x812c, 0x00000000), (0x8130, 0x00000000), (0x8134, 0x00000000),
    (0x8138, 0x00000000), (0x813c, 0x00000000), (0x8144, 0xffffffff), (0x8168, 0x00000000),
    (0x816c, 0x00000000), (0x8170, 0x32143320), (0x8174, 0xfaa4fa50), (0x8178, 0x00000100),
    (0x817c, 0x00000000), (0x81c0, 0x00000000), (0x81d0, 0x0000320a), (0x81ec, 0x00000000),
    (0x81f0, 0x00000000), (0x81f4, 0x00000000), (0x81f8, 0x00000000), (0x81fc, 0x00000000),
    (0x8200, 0x00000000), (0x8204, 0x00000000), (0x8208, 0x00000000), (0x820c, 0x00000000),
    (0x8210, 0x00000000), (0x8214, 0x00000000), (0x8218, 0x00000000), (0x821c, 0x00000000),
    (0x8220, 0x00000000), (0x8224, 0x00000000), (0x8228, 0x00000000), (0x822c, 0x00000000),
    (0x8230, 0x00000000), (0x8234, 0x00000000), (0x8238, 0x00000000), (0x823c, 0x00000000),
    (0x8240, 0x00100000), (0x8244, 0x0010f400), (0x8248, 0x00000100), (0x824c, 0x0001e800),
    (0x8250, 0x00000000), (0x8254, 0x00000000), (0x8258, 0x00000000), (0x825c, 0x400000ff),
    (0x8260, 0x00080922), (0x8264, 0x88a00010), (0x8270, 0x00000000), (0x8274, 0x40000000),
    (0x8278, 0x003e4180), (0x827c, 0x00000000), (0x8284, 0x0000002c), (0x8288, 0x0000002c),
    (0x828c, 0x00000000), (0x8294, 0x00000000), (0x8298, 0x00000000), (0x829c, 0x00000000),
    (0x8300, 0x00000040), (0x8314, 0x00000000), (0x8328, 0x00000000), (0x832c, 0x00000001),
    (0x8330, 0x00000302), (0x8334, 0x00000e00), (0x8338, 0x00ff0000), (0x833c, 0x00000000),
    (0x8340, 0x00010380), (0x8344, 0x00581043), (0x7010, 0x00000030), (0x7034, 0x00000002),
    (0x7038, 0x000004c2), (0x7800, 0x00140000), (0x7804, 0x0e4548d8), (0x7808, 0x54214514),
    (0x780c, 0x02025820), (0x7810, 0x71c0d388), (0x7814, 0x924934a8), (0x781c, 0x00000000),
    (0x7828, 0x66964300), (0x782c, 0x8db6d961), (0x7830, 0x8db6d96c), (0x7834, 0x6140008b),
    (0x783c, 0x72ee0a72), (0x7840, 0xbbfffffc), (0x7844, 0x000c0db6), (0x7848, 0x6db6246f),
    (0x784c, 0x6d9b66db), (0x7850, 0x6d8c6dba), (0x7854, 0x00040000), (0x7858, 0xdb003012),
    (0x785c, 0x04924914), (0x7860, 0x21084210), (0x7864, 0xf7d7ffde), (0x7868, 0xc2034080),
    (0x7870, 0x10142c00), (0x9808, 0x00000000), (0x980c, 0xafe68e30), (0x9810, 0xfd14e000),
    (0x9814, 0x9c0a9f6b), (0x981c, 0x00000000), (0x982c, 0x0000a000), (0x9830, 0x00000000),
    (0x983c, 0x00200400), (0x984c, 0x0040233c), (0x9854, 0x00000044), (0x9900, 0x00000000),
    (0x9904, 0x00000000), (0x9908, 0x00000000), (0x990c, 0x00000000), (0x991c, 0x10000fff),
    (0x9920, 0x04900000), (0x9928, 0x00000001), (0x992c, 0x00000004), (0x9934, 0x1e1f2022),
    (0x9938, 0x0a0b0c0d), (0x993c, 0x00000000), (0x9940, 0x14750604), (0x9948, 0x9280c00a),
    (0x994c, 0x00020028), (0x9954, 0x5f3ca3de), (0x9958, 0x0108ecff), (0x9968, 0x000003ce),
    (0x9970, 0x192bb514), (0x9974, 0x00000000), (0x9978, 0x00000001), (0x997c, 0x00000000),
    (0x9980, 0x00000000), (0x9984, 0x00000000), (0x9988, 0x00000000), (0x998c, 0x00000000),
    (0x9990, 0x00000000), (0x9994, 0x00000000), (0x9998, 0x00000000), (0x999c, 0x00000000),
    (0x99a0, 0x00000000), (0x99a4, 0x00000001), (0x99a8, 0x201fff00), (0x99ac, 0x2def0400),
    (0x99b0, 0x03051000), (0x99b4, 0x00000820), (0x99dc, 0x00000000), (0x99e0, 0x00000000),
    (0x99e4, 0xaaaaaaaa), (0x99e8, 0x3c466478), (0x99ec, 0x0cc80caa), (0x99f0, 0x00000000),
    (0xa208, 0x803e68c8), (0xa210, 0x4080a333), (0xa214, 0x00206c10), (0xa218, 0x009c4060),
    (0xa220, 0x01834061), (0xa224, 0x00000400), (0xa228, 0x000003b5), (0xa22c, 0x00000000),
    (0xa234, 0x20202020), (0xa238, 0x20202020), (0xa244, 0x00000000), (0xa248, 0xfffffffc),
    (0xa24c, 0x00000000), (0xa254, 0x00000000), (0xa258, 0x0ccb5380), (0xa25c, 0x15151501),
    (0xa260, 0xdfa90f01), (0xa268, 0x00000000), (0xa26c, 0x0ebae9e6), (0xa388, 0x0c000000),
    (0xa38c, 0x20202020), (0xa390, 0x20202020), (0xa39c, 0x00000001), (0xa3a0, 0x00000000),
    (0xa3a4, 0x00000000), (0xa3a8, 0x00000000), (0xa3ac, 0x00000000), (0xa3b0, 0x00000000),
    (0xa3b4, 0x00000000), (0xa3b8, 0x00000000), (0xa3bc, 0x00000000), (0xa3c0, 0x00000000),
    (0xa3c4, 0x00000000), (0xa3cc, 0x20202020), (0xa3d0, 0x20202020), (0xa3d4, 0x20202020),
    (0xa3e4, 0x00000000), (0xa3e8, 0x18c43433), (0xa3ec, 0x00f70081), (0xa3f0, 0x01036a2f),
    (0xa3f4, 0x00000000), (0xd270, 0x0d820820), (0xd35c, 0x07ffffef), (0xd360, 0x0fffffe7),
    (0xd364, 0x17ffffe5), (0xd368, 0x1fffffe4), (0xd36c, 0x37ffffe3), (0xd370, 0x3fffffe3),
    (0xd374, 0x57ffffe3), (0xd378, 0x5fffffe2), (0xd37c, 0x7fffffe2), (0xd380, 0x7f3c7bba),
    (0xd384, 0xf3307ff0),
)

# Ar9271Init.modes2g: 303 registers
INI_MODES_2G = (
    (0x1030, 0x00000160), (0x1070, 0x0000018c), (0x10b0, 0x00003e38), (0x10f0, 0x00000000),
    (0x8014, 0x08400b00), (0x801c, 0x12e0002b), (0x8318, 0x00003440), (0x9804, 0x00000300),
    (0x9820, 0x02020200), (0x9824, 0x01000e0e), (0x9828, 0x3a020001), (0x9834, 0x00000e0e),
    (0x9838, 0x00000007), (0x9840, 0x206a012e), (0x9844, 0x03721620), (0x9848, 0x00001053),
    (0xa848, 0x00001053), (0x9850, 0x6d4000e2), (0x9858, 0x7ec84d2e), (0x985c, 0x3137605e),
    (0x9860, 0x00058d18), (0x9864, 0x0001ce00), (0x9868, 0x5ac640d0), (0x986c, 0x06903881),
    (0x9910, 0x30002310), (0x9914, 0x00000898), (0x9918, 0x0000000b), (0x9924, 0xd00a800d),
    (0x9944, 0xffbc1020), (0x9960, 0x00000000), (0x9964, 0x00000000), (0x99b8, 0x0000421c),
    (0x99bc, 0x00000c00), (0x99c0, 0x05eea6d4), (0x99c4, 0x06336f77), (0x99c8, 0x6af6532f),
    (0x99cc, 0x08f186c8), (0x99d0, 0x00046384), (0x99d4, 0x00000000), (0x99d8, 0x00000000),
    (0x9a00, 0x00058084), (0x9a04, 0x00058088), (0x9a08, 0x0005808c), (0x9a0c, 0x00058100),
    (0x9a10, 0x00058104), (0x9a14, 0x00058108), (0x9a18, 0x0005810c), (0x9a1c, 0x00058110),
    (0x9a20, 0x00058114), (0x9a24, 0x00058180), (0x9a28, 0x00058184), (0x9a2c, 0x00058188),
    (0x9a30, 0x0005818c), (0x9a34, 0x00058190), (0x9a38, 0x00058194), (0x9a3c, 0x000581a0),
    (0x9a40, 0x0005820c), (0x9a44, 0x000581a8), (0x9a48, 0x00058284), (0x9a4c, 0x00058288),
    (0x9a50, 0x00058224), (0x9a54, 0x00058290), (0x9a58, 0x00058300), (0x9a5c, 0x00058304),
    (0x9a60, 0x00058308), (0x9a64, 0x0005830c), (0x9a68, 0x00058380), (0x9a6c, 0x00058384),
    (0x9a70, 0x00068700), (0x9a74, 0x00068704), (0x9a78, 0x00068708), (0x9a7c, 0x0006870c),
    (0x9a80, 0x00068780), (0x9a84, 0x00068784), (0x9a88, 0x00078b00), (0x9a8c, 0x00078b04),
    (0x9a90, 0x00078b08), (0x9a94, 0x00078b0c), (0x9a98, 0x00078b80), (0x9a9c, 0x00078b84),
    (0x9aa0, 0x00078b88), (0x9aa4, 0x00078b8c), (0x9aa8, 0x00078b90), (0x9aac, 0x000caf80),
    (0x9ab0, 0x000caf84), (0x9ab4, 0x000caf88), (0x9ab8, 0x000caf8c), (0x9abc, 0x000caf90),
    (0x9ac0, 0x000db30c), (0x9ac4, 0x000db310), (0x9ac8, 0x000db384), (0x9acc, 0x000db388),
    (0x9ad0, 0x000db324), (0x9ad4, 0x000eb704), (0x9ad8, 0x000eb6a4), (0x9adc, 0x000eb6a8),
    (0x9ae0, 0x000eb710), (0x9ae4, 0x000eb714), (0x9ae8, 0x000eb720), (0x9aec, 0x000eb724),
    (0x9af0, 0x000eb728), (0x9af4, 0x000eb72c), (0x9af8, 0x000eb7a0), (0x9afc, 0x000eb7a4),
    (0x9b00, 0x000eb7a8), (0x9b04, 0x000eb7b0), (0x9b08, 0x000eb7b4), (0x9b0c, 0x000eb7b8),
    (0x9b10, 0x000eb7a5), (0x9b14, 0x000eb7a9), (0x9b18, 0x000eb7ad), (0x9b1c, 0x000eb7b1),
    (0x9b20, 0x000eb7b5), (0x9b24, 0x000eb7b9), (0x9b28, 0x000eb7c5), (0x9b2c, 0x000eb7c9),
    (0x9b30, 0x000eb7d1), (0x9b34, 0x000eb7d5), (0x9b38, 0x000eb7d9), (0x9b3c, 0x000eb7c6),
    (0x9b40, 0x000eb7ca), (0x9b44, 0x000eb7ce), (0x9b48, 0x000eb7d2), (0x9b4c, 0x000eb7d6),
    (0x9b50, 0x000eb7c3), (0x9b54, 0x000eb7c7), (0x9b58, 0x000eb7cb), (0x9b5c, 0x000eb7cf),
    (0x9b60, 0x000eb7d7), (0x9b64, 0x000eb7db), (0x9b68, 0x000eb7db), (0x9b6c, 0x000eb7db),
    (0x9b70, 0x000eb7db), (0x9b74, 0x000eb7db), (0x9b78, 0x000eb7db), (0x9b7c, 0x000eb7db),
    (0x9b80, 0x000eb7db), (0x9b84, 0x000eb7db), (0x9b88, 0x000eb7db), (0x9b8c, 0x000eb7db),
    (0x9b90, 0x000eb7db), (0x9b94, 0x000eb7db), (0x9b98, 0x000eb7db), (0x9b9c, 0x000eb7db),
    (0x9ba0, 0x000eb7db), (0x9ba4, 0x000eb7db), (0x9ba8, 0x000eb7db), (0x9bac, 0x000eb7db),
    (0x9bb0, 0x000eb7db), (0x9bb4, 0x000eb7db), (0x9bb8, 0x000eb7db), (0x9bbc, 0x000eb7db),
    (0x9bc0, 0x000eb7db), (0x9bc4, 0x000eb7db), (0x9bc8, 0x000eb7db), (0x9bcc, 0x000eb7db),
    (0x9bd0, 0x000eb7db), (0x9bd4, 0x000eb7db), (0x9bd8, 0x000eb7db), (0x9bdc, 0x000eb7db),
    (0x9be0, 0x000eb7db), (0x9be4, 0x000eb7db), (0x9be8, 0x000eb7db), (0x9bec, 0x000eb7db),
    (0x9bf0, 0x000eb7db), (0x9bf4, 0x000eb7db), (0x9bf8, 0x000eb7db), (0x9bfc, 0x000eb7db),
    (0xaa00, 0x00058084), (0xaa04, 0x00058088), (0xaa08, 0x0005808c), (0xaa0c, 0x00058100),
    (0xaa10, 0x00058104), (0xaa14, 0x00058108), (0xaa18, 0x0005810c), (0xaa1c, 0x00058110),
    (0xaa20, 0x00058114), (0xaa24, 0x00058180), (0xaa28, 0x00058184), (0xaa2c, 0x00058188),
    (0xaa30, 0x0005818c), (0xaa34, 0x00058190), (0xaa38, 0x00058194), (0xaa3c, 0x000581a0),
    (0xaa40, 0x0005820c), (0xaa44, 0x000581a8), (0xaa48, 0x00058284), (0xaa4c, 0x00058288),
    (0xaa50, 0x00058224), (0xaa54, 0x00058290), (0xaa58, 0x00058300), (0xaa5c, 0x00058304),
    (0xaa60, 0x00058308), (0xaa64, 0x0005830c), (0xaa68, 0x00058380), (0xaa6c, 0x00058384),
    (0xaa70, 0x00068700), (0xaa74, 0x00068704), (0xaa78, 0x00068708), (0xaa7c, 0x0006870c),
    (0xaa80, 0x00068780), (0xaa84, 0x00068784), (0xaa88, 0x00078b00), (0xaa8c, 0x00078b04),
    (0xaa90, 0x00078b08), (0xaa94, 0x00078b0c), (0xaa98, 0x00078b80), (0xaa9c, 0x00078b84),
    (0xaaa0, 0x00078b88), (0xaaa4, 0x00078b8c), (0xaaa8, 0x00078b90), (0xaaac, 0x000caf80),
    (0xaab0, 0x000caf84), (0xaab4, 0x000caf88), (0xaab8, 0x000caf8c), (0xaabc, 0x000caf90),
    (0xaac0, 0x000db30c), (0xaac4, 0x000db310), (0xaac8, 0x000db384), (0xaacc, 0x000db388),
    (0xaad0, 0x000db324), (0xaad4, 0x000eb704), (0xaad8, 0x000eb6a4), (0xaadc, 0x000eb6a8),
    (0xaae0, 0x000eb710), (0xaae4, 0x000eb714), (0xaae8, 0x000eb720), (0xaaec, 0x000eb724),
    (0xaaf0, 0x000eb728), (0xaaf4, 0x000eb72c), (0xaaf8, 0x000eb7a0), (0xaafc, 0x000eb7a4),
    (0xab00, 0x000eb7a8), (0xab04, 0x000eb7b0), (0xab08, 0x000eb7b4), (0xab0c, 0x000eb7b8),
    (0xab10, 0x000eb7a5), (0xab14, 0x000eb7a9), (0xab18, 0x000eb7ad), (0xab1c, 0x000eb7b1),
    (0xab20, 0x000eb7b5), (0xab24, 0x000eb7b9), (0xab28, 0x000eb7c5), (0xab2c, 0x000eb7c9),
    (0xab30, 0x000eb7d1), (0xab34, 0x000eb7d5), (0xab38, 0x000eb7d9), (0xab3c, 0x000eb7c6),
    (0xab40, 0x000eb7ca), (0xab44, 0x000eb7ce), (0xab48, 0x000eb7d2), (0xab4c, 0x000eb7d6),
    (0xab50, 0x000eb7c3), (0xab54, 0x000eb7c7), (0xab58, 0x000eb7cb), (0xab5c, 0x000eb7cf),
    (0xab60, 0x000eb7d7), (0xab64, 0x000eb7db), (0xab68, 0x000eb7db), (0xab6c, 0x000eb7db),
    (0xab70, 0x000eb7db), (0xab74, 0x000eb7db), (0xab78, 0x000eb7db), (0xab7c, 0x000eb7db),
    (0xab80, 0x000eb7db), (0xab84, 0x000eb7db), (0xab88, 0x000eb7db), (0xab8c, 0x000eb7db),
    (0xab90, 0x000eb7db), (0xab94, 0x000eb7db), (0xab98, 0x000eb7db), (0xab9c, 0x000eb7db),
    (0xaba0, 0x000eb7db), (0xaba4, 0x000eb7db), (0xaba8, 0x000eb7db), (0xabac, 0x000eb7db),
    (0xabb0, 0x000eb7db), (0xabb4, 0x000eb7db), (0xabb8, 0x000eb7db), (0xabbc, 0x000eb7db),
    (0xabc0, 0x000eb7db), (0xabc4, 0x000eb7db), (0xabc8, 0x000eb7db), (0xabcc, 0x000eb7db),
    (0xabd0, 0x000eb7db), (0xabd4, 0x000eb7db), (0xabd8, 0x000eb7db), (0xabdc, 0x000eb7db),
    (0xabe0, 0x000eb7db), (0xabe4, 0x000eb7db), (0xabe8, 0x000eb7db), (0xabec, 0x000eb7db),
    (0xabf0, 0x000eb7db), (0xabf4, 0x000eb7db), (0xabf8, 0x000eb7db), (0xabfc, 0x000eb7db),
    (0xa204, 0x00000004), (0xa20c, 0x0001f000), (0xb20c, 0x0001f000), (0xa21c, 0x1883800a),
    (0xa230, 0x00000108), (0xa250, 0x0004a000), (0xa358, 0x7999aa0e),
)

# Ar9271Init.txGain2g: 33 registers
INI_TXGAIN_2G = (
    (0xa300, 0x00000000), (0xa304, 0x00009200), (0xa308, 0x00010208), (0xa30c, 0x00019608),
    (0xa310, 0x0001e610), (0xa314, 0x00024650), (0xa318, 0x0002d6d0), (0xa31c, 0x000316d2),
    (0xa320, 0x00039758), (0xa324, 0x0003b759), (0xa328, 0x0003d75a), (0xa32c, 0x0004175c),
    (0xa330, 0x0004575e), (0xa334, 0x0004979f), (0xa338, 0x0004d7df), (0xa33c, 0x0003a95e),
    (0xa340, 0x0003e9df), (0xa344, 0x0003e9df), (0xa348, 0x0003e9df), (0xa34c, 0x0003e9df),
    (0xa350, 0x0003e9df), (0xa354, 0x0003e9df), (0x7838, 0x00000029), (0x7824, 0x00d8abff),
    (0x786c, 0x48609eb4), (0x7820, 0x00000c04), (0xa274, 0x0a21c652), (0xa278, 0x3bdef7bd),
    (0xa27c, 0x050e83bd), (0xa394, 0x3bdef7bd), (0xa398, 0x000003bd), (0xa3dc, 0x3bdef7bd),
    (0xa3e0, 0x000003bd),
)

# Ar9271Init.txGainNormal2g: 33 registers
INI_TXGAIN_NORMAL_2G = (
    (0xa300, 0x00000000), (0xa304, 0x00009200), (0xa308, 0x00010208), (0xa30c, 0x00019608),
    (0xa310, 0x0001e610), (0xa314, 0x00024650), (0xa318, 0x0002d6d0), (0xa31c, 0x000316d2),
    (0xa320, 0x00039758), (0xa324, 0x0003b759), (0xa328, 0x0003d75a), (0xa32c, 0x0004175c),
    (0xa330, 0x0004575e), (0xa334, 0x0004979f), (0xa338, 0x0004d7df), (0xa33c, 0x0003a95e),
    (0xa340, 0x0003e9df), (0xa344, 0x0003e9df), (0xa348, 0x0003e9df), (0xa34c, 0x0003e9df),
    (0xa350, 0x0003e9df), (0xa354, 0x0003e9df), (0x7838, 0x00000029), (0x7824, 0x00d8abff),
    (0x786c, 0x48609eb4), (0x7820, 0x00000c04), (0xa274, 0x0a21c652), (0xa278, 0x3bdef7bd),
    (0xa27c, 0x050e83bd), (0xa394, 0x3bdef7bd), (0xa398, 0x000003bd), (0xa3dc, 0x3bdef7bd),
    (0xa3e0, 0x000003bd),
)


class NeedsReplug(usb1.USBErrorTimeout):
    """The AR9271 is wedged or not booting its firmware. Only a physical
    unplug/replug (a real VBUS drop) recovers it."""

    def __init__(self, msg: str) -> None:
        super().__init__()
        self.msg = msg

    def __str__(self) -> str:
        return self.msg


def freq_of(ch: int) -> int:
    """2.4 GHz channel -> MHz (MonitorCapture.chFreq)."""
    return 2407 + ch * 5


def _hex(b: bytes | None, n: int = 48) -> str:
    if b is None:
        return "(none)"
    s = bytes(b[:n]).hex(" ")
    return s + (" .." if len(b) > n else "")


def _be16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _be32(b: bytes, i: int) -> int:
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


def _u32(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "big")


def build_wmi_cmd(epid: int, cmd_id: int, seq: int, payload: bytes = b"") -> bytes:
    """htc_frame_hdr(8, endpoint=epid) + wmi_cmd_hdr(command_id, seq_no) + payload."""
    m = bytearray(8 + 4 + len(payload))
    m[0] = epid & 0xFF                       # HTC endpoint_id = WMI endpoint
    plen = 4 + len(payload)                  # wmi_cmd_hdr + payload
    m[2], m[3] = (plen >> 8) & 0xFF, plen & 0xFF
    m[8], m[9] = (cmd_id >> 8) & 0xFF, cmd_id & 0xFF
    m[10], m[11] = (seq >> 8) & 0xFF, seq & 0xFF
    m[12:] = payload
    return bytes(m)


def build_connect_svc(service_id: int, dl: int, ul: int) -> bytes:
    m = bytearray(18)
    m[3] = 10
    m[8], m[9] = 0, 2
    m[10], m[11] = (service_id >> 8) & 0xFF, service_id & 0xFF
    m[14], m[15] = dl & 0xFF, ul & 0xFF
    return bytes(m)


def build_config_pipe(credits: int) -> bytes:
    m = bytearray(12)
    m[3], m[8], m[9], m[10], m[11] = 4, 0, 5, 1, credits & 0xFF
    return bytes(m)


def build_setup_complete() -> bytes:
    m = bytearray(10)
    m[3], m[8], m[9] = 2, 0, 4
    return bytes(m)


def parse_htc_ready(b: bytes) -> str | None:
    if len(b) < 16:
        return None
    msg_id = _be16(b, 8)
    if msg_id == 1:
        return f" · HTC READY ok cr={_be16(b, 10)} sz={_be16(b, 12)} eps={b[14]}"
    # DIRTY RE-ENUM READY (offset+4): a USB data-only disconnect reboots the firmware but the
    # first HTC READY on 0x83 arrives shifted 4 bytes (id=1 at offset 12, credits at 14).
    if _be16(b, 12) == 1:
        return f" · HTC READY ok (offset+4 dirty re-enum) cr={_be16(b, 14)} sz=320 eps=22"
    return f" · 0x83 id={msg_id} (unexpected)"


def _htc_ok(x: str | None) -> bool:
    return x is not None and "HTC READY" in x


def parse_conn_resp(b: bytes) -> str:
    if len(b) < 14:
        return f"short({len(b)})"
    msg_id = _be16(b, 8)
    if msg_id != 3:
        return f"id={msg_id}"
    status, epid = b[12], b[13]
    return f"ep={epid} ok" if status == 0 else f"st={status}"


def msg_id_of(b: bytes | None) -> int | None:
    return None if b is None or len(b) < 10 else _be16(b, 8)


def _hdr_len(f: bytes) -> int:
    """ieee80211_hdrlen(): management 24, CTS/ACK 10, other control 16, data 24/30 (+2 QoS, +4 HT control)."""
    ftype, sub = (f[0] >> 2) & 3, (f[0] >> 4) & 0xF
    if ftype == 1:
        return 10 if sub in (12, 13) else 16
    if ftype != 2:
        return 24
    n = 30 if (f[1] & 3) == 3 else 24
    if sub & 0x8:          # QoS data
        n += 2
        if f[1] & 0x80:    # +HTC (order bit)
            n += 4
    return n


def _to_80211(f: bytes, complete: bool) -> bytes:
    """Hand on_frame a standard 802.11 frame, as ath9k's own RX path does before mac80211:
    drop the 0-3 bytes of padding the AR9271 inserts after a header whose length is not a
    multiple of 4 (QoS data), and the trailing 4-byte FCS (rs_datalen includes it; only
    FCS-good frames get here). A frame cut short by the end of a transfer keeps its tail."""
    hl = _hdr_len(f)
    pad = hl & 3
    if pad and len(f) >= hl + pad + 4:
        f = f[:hl] + f[hl + pad:]
    if complete and len(f) >= 24 + 4:
        f = f[:-4]
    return f


@dataclass
class Eep4k:
    """AR9271 4K EEPROM (eep_4k_ops); USB reads start at word 64, modal header at word 90."""
    version: int = 0
    mac: str = ""
    rx_mask: int = 0
    tx_mask: int = 0
    ant_ctrl_chain0: int = 0
    ant_ctrl_common: int = 0
    switch_settling: int = 0
    tx_rx_atten: int = 0
    rx_tx_margin: int = 0
    adc_desired_size: int = 0
    tx_end_to_rx_on: int = 0
    thresh62: int = 0
    xpa_bias_lvl: int = 0
    bsw_atten: int = 0
    bsw_margin: int = 0
    xatten2_db: int = 0
    xatten2_margin: int = 0
    modal_version: int = 0
    antdiv_ctl1: int = 0
    antdiv_ctl2: int = 0
    tx_gain_type: int = 0       # base_eep_header_4k offset 31


class Ar9271:
    """One AR9271 adapter. ``on_frame(frame, rssi_dbm, channel)`` gets every FCS-good
    802.11 frame (header first, the 40-byte ath_htc_rx_status already removed)."""

    def __init__(self, ctx, device, on_frame: Callable[[bytes, int, int], None],
                 on_status: Callable[[str], None] = lambda s: None) -> None:
        self.ctx, self.device = ctx, device
        self.on_frame, self.on_status = on_frame, on_status
        self.h = None
        self.eps: list[tuple[int, int, int]] = []     # (address, transfer type, max packet size), descriptor order
        self.stop_evt = threading.Event()             # the Android abort flag: every helper bails when set
        self.channel = 6                              # MonitorCapture.curChannel
        self.frames = 0                               # FCS-good frames handed to on_frame
        self.bytes = 0
        # [0]=frames [1]=fcs-good [2]=crc [3]=phyerr [4]=maxDatalen(good only) [5]=FCS-error beacons skipped
        self.stats = [0] * 6
        self.live_bcd: int | None = None
        self.reuse = False
        self.needs_firmware = False
        self.ready: bytes | None = None
        self.htc: str | None = None
        self.fw_version = ""
        self.eep: Eep4k | None = None
        self.mac = ""
        self.serial = ""
        self.hiccups = 0
        self.gone = False
        self._port = (device.getBusNumber(), tuple(device.getPortNumberList() or ()))
        self._rx_transfers: list = []
        self._rx_thread: threading.Thread | None = None
        self._rx_stop = threading.Event()
        self._rx_lock = threading.Lock()
        self.last_rx = 0.0

    # -- USB primitives ------------------------------------------------------------
    # Android needs UsbRequest for the interrupt pipes 0x83/0x04 (bulkTransfer cannot reach them
    # on some phones). libusb does interrupt transfers natively, so ctrl_in / ctrl_out are plain
    # interrupt transfers with the same timeouts, retries and hold-through-a-hiccup rules.
    def _canceled(self) -> bool:
        return self.stop_evt.is_set() or self.gone

    def _mark_gone(self, what: str) -> None:
        if not self.gone:
            log.info("AR9271: adapter gone (%s)", what)
        self.gone = True

    def hold_on(self, what: str) -> bool:
        """True = the adapter is still attached, so the failure was a hiccup worth retrying."""
        if self._canceled():
            return False
        self.hiccups += 1
        if self.hiccups <= 20 or self.hiccups % 50 == 0:
            log.debug("USB hiccup (%s): adapter still attached, holding the session (#%d)", what, self.hiccups)
        time.sleep(0.06)
        return True

    def ctrl_in(self, ep: int, timeout_ms: int, tag: str | None = None) -> bytes | None:
        """One interrupt IN read (HTC READY / WMI replies on 0x83)."""
        if self._canceled():
            return None
        try:
            b = self.h.interruptRead(ep, 512, timeout=timeout_ms)
        except usb1.USBErrorTimeout:
            if tag:
                log.debug("%s: TIMEOUT (no data)", tag)
            return None
        except usb1.USBErrorNoDevice:
            self._mark_gone(f"{tag or 'ctrl_in'}: no device")
            return None
        except usb1.USBError as e:
            if not self.hold_on("0x83 read"):
                if tag:
                    log.debug("%s: %s, device gone, aborting probe", tag, e)
                self.gone = True
            return None
        if not b:
            if tag:
                log.debug("%s: completed but 0 bytes", tag)
            return None
        b = bytes(b)
        if tag:
            log.debug("%s: OK %d bytes = %s", tag, len(b), _hex(b, 32))
        return b

    def _ctrl_out_once(self, ep: int, data: bytes, timeout_ms: int) -> int:
        if self._canceled():
            return -1
        try:
            return self.h.interruptWrite(ep, data, timeout=timeout_ms)
        except usb1.USBErrorNoDevice:
            self._mark_gone("0x04 write: no device")
            return -1
        except usb1.USBError:
            return -1

    def ctrl_out(self, ep: int, data: bytes, timeout_ms: int) -> int:
        """One interrupt OUT write (WMI commands on 0x04). Retried while the adapter is still
        attached; every command sent is idempotent, so a duplicate is harmless."""
        until = time.monotonic() + HOLD_S
        while True:
            r = self._ctrl_out_once(ep, data, timeout_ms)
            if r >= 0 or self._canceled() or time.monotonic() > until or not self.hold_on("0x04 write"):
                return r

    def read_once(self, ep: int, timeout_ms: int) -> bytes | None:
        return self.ctrl_in(ep, timeout_ms)

    # -- WMI -----------------------------------------------------------------------------
    def wmi_reg_read(self, reg: int, seq: int) -> int | None:
        """WMI_REG_READ (0x14): payload = register offset as big-endian u32; value at byte 12."""
        if self._canceled():
            return None
        cmd = build_wmi_cmd(1, 0x14, seq, _u32(reg))
        sent = self.ctrl_out(EP_REG_OUT, cmd, 1000)
        resp = self.read_once(EP_REG_IN, 1200)

        def seq_of(r: bytes) -> int:
            return _be16(r, 10) if len(r) >= 12 else -1

        # Hold through a hiccup: a late reply for an earlier command (wrong seq) is drained, and a
        # missing reply re-issues the read (idempotent) while the adapter is still attached.
        tries = 0
        while tries < 3 and (resp is None or len(resp) < 16 or seq_of(resp) != (seq & 0xFFFF)):
            tries += 1
            if resp is not None and len(resp) >= 16:
                resp = self.read_once(EP_REG_IN, 600)
                continue
            if not self.hold_on(f"REG_READ 0x{reg:04x}"):
                break
            sent = self.ctrl_out(EP_REG_OUT, cmd, 1000)
            resp = self.read_once(EP_REG_IN, 1200)
        log.debug("REG_READ 0x%04x sent=%d resp=%s", reg, sent, _hex(resp))
        if resp is None or len(resp) < 16:
            return None
        return _be32(resp, 12)

    def wmi_reg_write(self, reg: int, value: int, seq: int) -> None:
        """WMI_REG_WRITE (0x15): payload = register_write { __be32 reg; __be32 val }."""
        if self._canceled():
            return
        cmd = build_wmi_cmd(1, 0x15, seq, _u32(reg) + _u32(value))
        sent = self.ctrl_out(EP_REG_OUT, cmd, 1000)
        resp = self.read_once(EP_REG_IN, 800)
        log.debug("REG_WRITE 0x%04x=0x%08x sent=%d ack=%s", reg, value & 0xFFFFFFFF, sent, "y" if resp else "n")

    def wmi_reg_rmw(self, reg: int, set_bits: int, clr_bits: int, seq: int) -> None:
        """WMI_REG_RMW (32): the firmware applies set/clr against the real register value."""
        cmd = build_wmi_cmd(1, 32, seq, _u32(reg) + _u32(set_bits) + _u32(clr_bits))
        self.ctrl_out(EP_REG_OUT, cmd, 1000)
        self.read_once(EP_REG_IN, 600)

    def reg_set(self, reg: int, bits: int, seq: int) -> int:
        v = self.wmi_reg_read(reg, seq)
        self.wmi_reg_write(reg, (v if v is not None else 0) | bits, seq + 1)
        return seq + 2

    def wait_clear(self, reg: int, mask: int, tries: int, seq: int) -> bool:
        for i in range(tries):
            v = self.wmi_reg_read(reg, seq + i)
            if v is None:
                return False
            if v & mask == 0:
                return True
            time.sleep(0.003)
        return False

    def wmi_cmd_log(self, cmd: int, name: str, seq: int, payload: bytes = b"") -> bytes | None:
        c = build_wmi_cmd(1, cmd, seq, payload)
        self.ctrl_out(EP_REG_OUT, c, 1000)
        r = self.read_once(EP_REG_IN, 800)
        log.debug("  %s -> %s", name, "ack" if r is not None else "no reply")
        return r

    def write_array(self, arr, label: str, seq0: int) -> int:
        """Bulk-write a (reg, val) table via multi-entry WMI_REG_WRITE, 16 registers per command."""
        seq = seq0
        n = len(arr)
        i = acks = batches = 0
        while i < n:
            if self._canceled():
                log.info("%s aborted (adapter gone)", label)
                break
            batch = min(16, n - i)
            payload = b"".join(_u32(r) + _u32(v) for r, v in arr[i:i + batch])
            cmd = build_wmi_cmd(1, 0x15, seq, payload)
            seq += 1
            self.ctrl_out(EP_REG_OUT, cmd, 1000)
            if self.read_once(EP_REG_IN, 400) is not None:
                acks += 1
            batches += 1
            i += batch
        log.debug("%s: %d regs / %d batches / %d acks", label, n, batches, acks)
        return seq

    # -- open / close ----------------------------------------------------------------------
    def _live_bcd(self) -> int | None:
        """LIVE bcdDevice: the ROM bootloader answers 0x0108, the booted ath9k_htc firmware
        0xFFFF. A standard GET_DESCRIPTOR is harmless in both states."""
        try:
            dd = bytes(self.h.controlRead(0x80, 6, 0x0100, 0, 18, timeout=1000))
            return (dd[12] | (dd[13] << 8)) if len(dd) >= 14 else None
        except Exception:
            return None

    def open(self) -> None:
        self.h = self.device.open()
        try:
            self.serial = self.device.getSerialNumber() or ""
        except Exception:
            self.serial = ""
        self.live_bcd = self._live_bcd()
        state = ("unknown state" if self.live_bcd is None else
                 "firmware RUNNING" if self.live_bcd == BCD_FIRMWARE else "ROM bootloader (no firmware)")
        log.info("AR9271: live bcdDevice=%s = %s",
                 "(read failed)" if self.live_bcd is None else f"{self.live_bcd:04x}", state)
        self.h.claimInterface(0)
        # NB: endpoint halts are deliberately NOT cleared: CLEAR_FEATURE(ENDPOINT_HALT) flushes the
        # AR9271's one-shot HTC READY off interrupt pipe 0x83.
        self.eps = []
        for setting in self.device[0][0]:
            for ep in setting:
                self.eps.append((ep.getAddress(), ep.getAttributes() & 3, ep.getMaxPacketSize()))
            break
        addrs = {a for a, _, _ in self.eps}
        if not {EP_RX, EP_REG_IN, EP_REG_OUT} <= addrs:
            raise OSError("AR9271: missing endpoints")
        log.debug("AR9271 endpoints: %s", " ".join(
            f"{a:02x}/{'b' if t == 2 else 'i' if t == 3 else '?'}{'in' if a & 0x80 else 'out'}(mps={m})"
            for a, t, m in self.eps))

    def _release(self) -> None:
        """MonitorCapture.probe() finally: let in-flight I/O settle, release, close."""
        if self.h:
            time.sleep(0.15)
            # Closing cancels and waits for any transfer still in flight before the
            # interface is released; releasing first with RX outstanding corrupts memory.
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    def close(self) -> None:
        self._stop_rx()
        self._release()

    def is_alive(self) -> bool:
        """Liveness check for the monitor manager (the AR9271 counterpart of the MT7612U
        MAC_CSR0 read): the same harmless live GET_DESCRIPTOR used to tell bootloader from
        firmware, on EP0, so it never interleaves with the WMI traffic on 0x83/0x04.
        False once the adapter is unplugged."""
        if self.gone or self.h is None:
            return False
        try:
            dd = bytes(self.h.controlRead(0x80, 6, 0x0100, 0, 18, timeout=1000))
        except usb1.USBErrorNoDevice:
            self.gone = True
            return False
        except Exception:
            return False
        return len(dd) >= 12 and (dd[8] | (dd[9] << 8)) == 0x0CF3

    # -- firmware (Ar9271Loader.kt + MainActivity engage + MonitorCapture.probe handshake) --
    def load_firmware(self) -> None:
        """PROBE-FIRST / REUSE. Never upload blindly: probe first. Running firmware is adopted
        (no upload); only a true bootloader gets htc_9271.fw, exactly once, then the booted chip
        is re-probed for its HTC READY."""
        self._handshake(after_upload=False)
        if _htc_ok(self.htc) or self.reuse:
            return
        if not self.needs_firmware:
            raise OSError("AR9271: no HTC READY")
        if self._canceled():
            raise OSError("AR9271: stopped")
        log.info("AR9271 bootloader: uploading %s ONCE (only a true bootloader; never over running fw)", FIRMWARE)
        self._release()
        self._upload()
        time.sleep(FW_BOOT_WAIT_S)
        self._reacquire()
        self.open()
        self._handshake(after_upload=True)
        if not (_htc_ok(self.htc) or self.reuse):
            raise NeedsReplug("the AR9271 never produced a valid HTC READY after the firmware upload: "
                              "unplug it and plug it back in")

    def _upload(self) -> None:
        """Ar9271Loader.upload(): the ath9k_htc download handshake over EP0 vendor requests."""
        fw = (FIRMWARE_DIR / FIRMWARE).read_bytes()
        self.on_status(f"uploading firmware · {len(fw) // 1024} KB")
        h = self.device.open()
        try:
            try:
                h.claimInterface(0)
            except Exception:
                pass
            t0 = time.monotonic()
            addr, off, blocks = FW_ADDR, 0, 0
            while off < len(fw):
                n = min(FW_BLOCK, len(fw) - off)
                try:
                    h.controlWrite(FW_REQ_TYPE, FW_DOWNLOAD, (addr >> 8) & 0xFFFF, 0, fw[off:off + n], timeout=2000)
                except usb1.USBError as e:
                    log.info("AR9271 FW BLOCK FAILED at off=%d len=%d addr=0x%06x: %s", off, n, addr, e)
                    raise NeedsReplug(f"AR9271 firmware upload stalled at {off // 1024} KB ({e}): "
                                      "unplug it and plug it back in") from e
                blocks += 1
                off += n
                addr += n
            log.info("AR9271 fw uploaded: %d B in %d blocks in %d ms", off, blocks, (time.monotonic() - t0) * 1000)
            # Trigger execution at FW_TEXT_ADDR. The adapter resets immediately, so an error here is
            # normal; success is also fine. Only a later HTC READY on 0x83 proves the boot.
            try:
                h.controlWrite(FW_REQ_TYPE, FW_DOWNLOAD_COMP, (FW_TEXT_ADDR >> 8) & 0xFFFF, 0, b"", timeout=2000)
                comp = "ok"
            except usb1.USBError as e:
                comp = str(e)
            log.info("AR9271 FW_DOWNLOAD_COMP -> exec 0x%06x: %s", FW_TEXT_ADDR, comp)
            self.on_status("firmware loaded · adapter booting")
        finally:
            try:
                h.close()
            except Exception:
                pass

    def _reacquire(self) -> None:
        """The AR9271 keeps 0cf3:9271 after boot; re-resolve it (same port first)."""
        same = other = None
        for d in self.ctx.getDeviceIterator(skip_on_error=True):
            if (d.getVendorID(), d.getProductID()) not in VID_PIDS:
                continue
            if (d.getBusNumber(), tuple(d.getPortNumberList() or ())) == self._port:
                same = d
                break
            other = other or d
        dev = same or other
        if dev is not None:
            log.info("AR9271 post-boot handle: bus %d port %s addr %d (was addr %d)", dev.getBusNumber(),
                     dev.getPortNumberList(), dev.getDeviceAddress(), self.device.getDeviceAddress())
            self.device = dev
            self._port = (dev.getBusNumber(), tuple(dev.getPortNumberList() or ()))

    def _handshake(self, after_upload: bool) -> None:
        """The front of MonitorCapture.probe(): read every IN endpoint, find HTC READY, decide
        bootloader / reuse / fresh firmware."""
        self.needs_firmware = False
        self.reuse = False
        self.hiccups = 0
        self.ready = None
        self.htc = None
        ready = None
        for addr, xfer, mps in self.eps:
            if not addr & 0x80:
                continue
            if self._canceled():
                break
            total, first = 0, None
            until = time.monotonic() + 0.9
            if xfer == 3:
                # Interrupt IN (0x83 = HTC control/WMI, where HTC READY arrives). Never clear its halt
                # first: that flushes the queued READY. Just read it.
                dbg = 0
                while first is None and not self._canceled() and time.monotonic() < until:
                    cp = self.ctrl_in(addr, 300, f"0x{addr:02x} init#{dbg}")
                    dbg += 1
                    if cp is None:
                        continue
                    first = cp
                    total += len(cp)
            else:
                size = max(mps, 512)
                while not self._canceled() and time.monotonic() < until:
                    try:
                        b = self.h.bulkRead(addr, size, timeout=300)
                    except usb1.USBErrorTimeout:
                        continue
                    except usb1.USBErrorNoDevice:
                        self._mark_gone("probe bulk read")
                        break
                    except usb1.USBError:
                        continue
                    if b:
                        first = first or bytes(b)
                        total += len(b)
            log.info("AR9271 ep%02x read %dB first=%s", addr, total, _hex(first, 24))
            if addr == EP_REG_IN and first is not None:
                ready = first
        htc = parse_htc_ready(ready) if ready is not None else None
        log.info("AR9271 HTC ready parse:%s", htc or " (none)")

        reuse = False
        if _htc_ok(htc):
            pass
        elif not after_upload and self.live_bcd is not None and self.live_bcd != BCD_FIRMWARE:
            # The live descriptor says ROM BOOTLOADER: do NOT send the WMI version probe. A WMI packet
            # written to EP 0x04 of a bare bootloader POISONS it (the next firmware never emits READY).
            log.info("AR9271: bootloader confirmed by live descriptor, skipping the WMI reuse probe")
        elif not after_upload:
            # No fresh HTC READY. Ask the firmware for its version over WMI: this only answers when
            # the firmware is up with its HTC services still mapped from a prior session (reuse).
            # Only probe-first: a WMI packet before a freshly booted firmware's READY wedges it.
            ver = build_wmi_cmd(1, 3, 2)          # WMI_GET_FW_VERSION
            self.ctrl_out(EP_REG_OUT, ver, 800)
            vr = self.read_once(EP_REG_IN, 900)
            maj = _be16(vr, 12) if vr is not None and len(vr) >= 16 else -1
            if 1 <= maj <= 3:
                reuse = True
                log.info("AR9271 REUSE: firmware already running (WMI fw major=%d), reconfiguring the live "
                         "chip, NO reset/upload", maj)
                self.on_status("reusing running firmware")
            else:
                log.info("AR9271: no running firmware via WMI (reply=%s), treating as bootloader/booting", _hex(vr))
        # Still no READY and not reuse. Just uploaded: the chip is booting, retry for READY. Not just
        # uploaded: a bootloader never sends READY on its own, so flag needs_firmware.
        if not _htc_ok(htc) and not reuse and after_upload:
            for attempt in range(1, 11):
                time.sleep(0.25)
                found = False
                for k in range(6):
                    cand = self.ctrl_in(EP_REG_IN, 300, f"0x83 retry{attempt}" if k == 0 else None)
                    if cand is None:
                        continue
                    if len(cand) >= 16:
                        p = parse_htc_ready(cand)
                        if _htc_ok(p):
                            ready, htc = cand, p
                            log.info("AR9271 HTC READY recovered on retry %d: %s", attempt, p)
                            found = True
                            break
                if found:
                    break
                log.info("AR9271 HTC handshake retry %d/10: still no valid READY", attempt)
        self.ready, self.htc, self.reuse = ready, htc, reuse
        if not _htc_ok(htc) and not reuse:
            if not after_upload:
                log.info("AR9271: no firmware present (bootloader), engage will upload %s", FIRMWARE)
                self.on_status("uploading firmware")
                self.needs_firmware = True
            else:
                log.info("AR9271 ABORT: firmware never produced a valid HTC READY after upload")

    # -- bring-up (MonitorCapture.probe, connect services through the RX re-arm cycle) -------
    def bring_up(self) -> None:
        reuse = self.reuse
        self.on_status("connecting HTC services")
        conn_msg = build_connect_svc(0x0100, dl=3, ul=4)
        sent = self.ctrl_out(EP_REG_OUT, conn_msg, 1000)
        resp = self.read_once(EP_REG_IN, 1200)
        # The reply is often just LATE: keep reading, re-send every other try (idempotent).
        tries = 0
        while resp is None and tries < 4 and self.hold_on("WMI connect reply"):
            tries += 1
            if tries % 2 == 0:
                self.ctrl_out(EP_REG_OUT, conn_msg, 1000)
                log.debug("WMI connect re-sent (holding through a hiccup)")
            resp = self.read_once(EP_REG_IN, 1200)
        log.info("AR9271 WMI connect sent=%d resp=%s", sent, _hex(resp))
        conn_str = "send failed" if sent < 0 else "no reply" if resp is None else parse_conn_resp(resp)
        exhausted = reuse and conn_str == "st=3"
        if exhausted:
            # HTC_SERVICE_NO_RESOURCES: the firmware hands out new HTC endpoints on every connect
            # (22 in all, 9 per session), so the third reuse without a power cycle runs out. The
            # endpoints the first session connected are still live and every WMI command goes to
            # endpoint 1, so carry on with those instead of asking for a replug.
            log.info("AR9271 out of HTC endpoints; reusing the ones already connected")
        elif resp is None or "ep=" not in conn_str:
            raise OSError(f"AR9271: WMI connect failed ({conn_str})")

        # Connect the data/mgmt HTC services so the firmware has endpoints to deliver RX frames on.
        for svc in ([] if exhausted else DATA_SERVICES):
            self.ctrl_out(EP_REG_OUT, build_connect_svc(svc, dl=2, ul=1), 1000)
            r = self.read_once(EP_REG_IN, 1000)
            log.debug("connect 0x%04x -> %s", svc, parse_conn_resp(r) if r is not None else "noreply")
        rdy = self.ready

        def rbe16(o: int) -> int:
            return _be16(rdy, o) if rdy is not None and len(rdy) >= o + 2 else -1

        if reuse or rdy is None or len(rdy) < 16:
            credits = 33
        elif rbe16(8) == 1:
            credits = rbe16(10)          # normal READY: id@8, credits@10
        elif rbe16(12) == 1:
            credits = rbe16(14)          # offset+4 dirty-re-enum READY: id@12, credits@14
        else:
            credits = 33
        self.ctrl_out(EP_REG_OUT, build_config_pipe(credits), 1000)
        cfg = self.read_once(EP_REG_IN, 1000)
        log.debug("config-pipe resp=%s", _hex(cfg))
        self.ctrl_out(EP_REG_OUT, build_setup_complete(), 1000)
        setup = self.read_once(EP_REG_IN, 1000)
        log.debug("setup-complete resp=%s", _hex(setup))

        # First real WMI command: GET_FW_VERSION (id 3) over WMI endpoint 1.
        wmi_cmd = build_wmi_cmd(1, 3, 1)
        self.ctrl_out(EP_REG_OUT, wmi_cmd, 1000)
        wr = self.read_once(EP_REG_IN, 1200)
        if wr is not None and len(wr) >= 16:
            self.fw_version = f"{_be16(wr, 12)}.{_be16(wr, 14)}"
            log.info("AR9271 WMI fw version = %s", self.fw_version)
        else:
            log.info("AR9271 WMI fw-version: %s", "noreply" if wr is None else f"{len(wr)}B")

        # ===== REUSE = ADOPT THE LIVE FIRMWARE, SKIP THE HW RE-INIT =====
        # The RX re-arm cycle and hopping still run on reuse (field log 2026-10-20).
        if not reuse:
            self._cold_init()
        # host_rx_init: rxena + RX/mcast filter (kept on reuse too: an idempotent re-assert).
        self.wmi_reg_write(0x0008, 0x00000004, 213)          # AR_CR = RXE
        self.wmi_reg_write(0x803C, RX_FILTER_MONITOR, 214)   # AR_RX_FILTER (full monitor)
        self.wmi_reg_write(0x8040, 0xFFFFFFFF, 215)          # AR_MCAST_FIL0
        self.wmi_reg_write(0x8044, 0xFFFFFFFF, 216)          # AR_MCAST_FIL1
        diag = self.wmi_reg_read(0x8048, 217) or 0
        self.wmi_reg_write(0x8048, diag & ~(0x20 | 0x02000000), 218)
        if not reuse:
            # Target-side context ath9k creates right after START_RECV: without a VAP + node the
            # firmware has no interface to attribute RX frames to.
            mac_str = self.eep.mac if self.eep else ""
            mac = bytes(int(x, 16) for x in mac_str.split(":")) if ":" in mac_str else bytes(6)
            self.wmi_cmd_log(24, "TARGET_IC_UPDATE", 790, bytes([0, 0, 0xFF, 0xFF, 0xFF, 0, 1, 0]))
            self.wmi_cmd_log(19, "VAP_CREATE", 791, bytes([0, 1]) + mac + bytes([0, 9, 0, 0]))
            self.wmi_cmd_log(16, "NODE_CREATE", 792, mac + bytes(6) + bytes([0, 0, 1, 0, 0, 0, 0, 0xFF]))
            self.wmi_cmd_log(15, "SET_MODE", 793, bytes([0, 1]))
            self.wmi_cmd_log(5, "ENABLE_INTR", 794)
            # MAX SENSITIVITY: lowest firstep/spur immunity levels, weak-signal OFDM detection on.
            self.ani_set(ofdm_weak=True, firstep_lvl=0, spur_lvl=0)
        # ===== ath9k_htc_set_channel() re-arm cycle =====
        # DISABLE_INTR -> DRAIN_TXQ_ALL -> STOP_RECV -> host_rx_init -> START_RECV -> SET_MODE -> ENABLE_INTR
        log.info("AR9271 RX re-arm cycle%s", " on adopted firmware" if reuse else "")
        for p in (1, 2):
            if self._canceled():
                break
            self.wmi_cmd_log(4, "DISABLE_INTR", 810 + p * 10)
            self.wmi_cmd_log(11, "DRAIN_TXQ_ALL", 811 + p * 10)
            self.wmi_cmd_log(13, "STOP_RECV", 812 + p * 10)
            time.sleep(0.02)
            self.wmi_reg_write(0x0008, 0x00000004, 813 + p * 10)
            self.wmi_reg_write(0x803C, RX_FILTER_MONITOR, 814 + p * 10)
            self.wmi_reg_write(0x8040, 0xFFFFFFFF, 815 + p * 10)
            self.wmi_reg_write(0x8044, 0xFFFFFFFF, 816 + p * 10)
            dg = self.wmi_reg_read(0x8048, 817 + p * 10) or 0
            self.wmi_reg_write(0x8048, dg & ~(0x20 | 0x02000000), 818 + p * 10)
            self.wmi_cmd_log(12, "START_RECV", 819 + p * 10)
            self.wmi_cmd_log(15, "SET_MODE", 820 + p * 10, bytes([0, 1]))
            self.wmi_cmd_log(5, "ENABLE_INTR", 821 + p * 10)
            st = self.wmi_reg_read(0x9864, 822 + p * 10) or 0
            log.debug("  re-arm pass %d done (CCA=0x%08x)", p, st)
            time.sleep(0.15)
        if self._canceled():
            raise OSError("AR9271: adapter unplugged during bring-up" if self.gone else "AR9271: stopped")
        self.on_status("monitor mode live" + (" (reused running firmware)" if reuse else ""))

    def _cold_init(self) -> None:
        """Everything MonitorCapture runs inside ``if (!reuse)`` before host_rx_init."""
        # ===== QUIESCE THE FIRMWARE BEFORE TOUCHING HARDWARE =====
        self.wmi_cmd_log(13, "STOP_RECV", 380)               # WMI_STOP_RECV
        self.wmi_cmd_log(4, "DISABLE_INTR", 381)             # WMI_DISABLE_INTR
        time.sleep(0.12)
        # --- REGISTER I/O TICKLE ---
        srev = self.wmi_reg_read(0x4020, 2)                      # AR_SREV
        if srev is not None:
            sid = srev & 0xFF
            mac_ver = ((srev >> 18) & 0x3FFF) if sid == 0xFF else ((srev >> 4) & 0xF)
            mac_rev = ((srev >> 8) & 0xF) if sid == 0xFF else (srev & 0x7)
            log.info("AR9271 AR_SREV=0x%08x -> macVer=0x%x rev=%d (%s)", srev, mac_ver, mac_rev,
                     "AR9271" if mac_ver == 0x140 else f"ver0x{mac_ver:x}")
        self.wmi_reg_read(0x7044, 3)                             # AR_RTC_STATUS
        self.wmi_reg_read(0x8000, 4)                             # AR_STA_ID0
        self.wmi_reg_read(0x8004, 5)                             # AR_STA_ID1

        # ===== CHIP-RESET BLOCKS: FIRST BOOT ONLY =====
        # --- WAKE SEQUENCE (ath9k_hw_set_reset_power_on for AR9271) ---
        self.on_status("waking radio")
        self.wmi_reg_write(0x704C, 0x00000003, 6)                # FORCE_WAKE_EN | ON_INT
        self.wmi_reg_write(0x4000, 0x00000001, 7)                # AR_RC = AHB
        self.wmi_reg_write(0x7040, 0x00000000, 8)                # RTC_RESET = 0
        time.sleep(0.003)
        self.wmi_reg_write(0x4000, 0x00000000, 9)                # AR_RC = 0
        self.wmi_reg_write(0x7040, 0x00000001, 10)               # RTC_RESET = 1
        woke = False
        for i in range(12):
            st = self.wmi_reg_read(0x7044, 11 + i)
            if st is not None and st & 0xF == 0x2:
                log.debug("RTC STATUS=ON after %d polls", i + 1)
                woke = True
                break
            time.sleep(0.005)
        if not woke:
            log.info("AR9271: RTC did not report ON")
        mac0 = self.wmi_reg_read(0x8000, 40)
        log.debug("post-wake 0x8000=0x%08x macAwake=%s", -1 if mac0 is None else mac0,
                  mac0 is not None and mac0 != 0xDEADBEEF)
        # Firmware's EEPROM-derived antenna switch, read before the reset/INI wipes it.
        a0 = self.wmi_reg_read(0x9960, 240)
        ac = self.wmi_reg_read(0x9964, 241)
        log.debug("BOOT board values: 0x9960=%s 0x9964=%s", a0, ac)
        # ANT_EEPROM_EARLY is off on Android (0x2000 reads here dropped the adapter).

        # ===== AR9271 FIRST-RESET RADIO BLOCK (REQUIRED; without it NF reads -13 dBm, RX deaf) =====
        self.wmi_reg_write(0x50044, 0x00000020, 48)              # AR9271_RADIO_RF_RST
        time.sleep(0.05)
        # --- WARM RESET (ath9k_hw_set_reset, ATH9K_RESET_WARM) ---
        self.on_status("warm reset")
        self.wmi_reg_write(0x704C, 0x00000003, 50)               # force wake
        sync = self.wmi_reg_read(0x4028, 51) or 0                # AR_INTR_SYNC_CAUSE
        if sync & 0x3000:
            self.wmi_reg_write(0x402C, 0x00000000, 52)           # AR_INTR_SYNC_ENABLE = 0
            self.wmi_reg_write(0x4000, 0x00000101, 53)           # AR_RC = HOSTIF|AHB
        else:
            self.wmi_reg_write(0x4000, 0x00000001, 53)           # AR_RC = AHB
        self.wmi_reg_write(0x7000, 0x00000001, 54)               # AR_RTC_RC = MAC_WARM
        time.sleep(0.002)
        self.wmi_reg_write(0x7000, 0x00000000, 55)               # AR_RTC_RC = 0
        rc_clear = False
        for i in range(10):
            rc = self.wmi_reg_read(0x7000, 56 + i)
            if rc is not None and rc & 0x3 == 0:
                rc_clear = True
                break
            time.sleep(0.003)
        self.wmi_reg_write(0x4000, 0x00000000, 70)               # AR_RC = 0
        log.debug("warm reset rcClear=%s", rc_clear)
        self.wmi_reg_read(0x9960, 242)
        self.wmi_reg_read(0x9964, 243)
        # Write/read-back proof on the now-powered MAC (AR_STA_ID0).
        self.wmi_reg_write(0x8000, 0x1A2B3C4D, 71)
        rb = self.wmi_reg_read(0x8000, 72)
        log.info("AR9271 MAC write/readback ok=%s", rb == 0x1A2B3C4D)
        # --- PLL / CLOCK INIT (ath9k_hw_init_pll, AR9271 2.4 GHz) ---
        self.on_status("PLL")
        self.wmi_reg_write(0x7014, 0x0000142C, 73)               # AR_RTC_PLL_CONTROL: refdiv5 | div0x2c
        time.sleep(0.001)
        self.wmi_reg_write(0x50040, 0x00000304, 74)              # AR9271 core clock -> 117 MHz
        self.wmi_reg_write(0x7048, 0x00000002, 75)               # AR_RTC_SLEEP_CLK = FORCE_DERIVED_CLK
        time.sleep(0.002)
        self.wmi_reg_write(0x50044, 0x00004000, 76)              # AR9271_GATE_MAC_CTL
        time.sleep(0.05)

        # --- PHY INIT TABLES (ar9002 initvals) ---
        self.on_status("writing PHY init (~660 regs)")
        s = 100
        s = self.write_array(INI_MODES_2G, "modes(2G_HT20)", s)
        s = self.write_array(INI_COMMON, "common", s)
        s = self.write_array(INI_TXGAIN_2G, "txGain(2G)", s)
        chk = self.wmi_reg_read(0x1030, s)
        s += 1
        log.info("AR9271 PHY checkpoint 0x1030=%s (want 0x160) ok=%s", None if chk is None else hex(chk), chk == 0x160)
        # --- ar9002_hw_load_ani_reg: detection thresholds ---
        for i, (reg, val) in enumerate(ANI_REGS):
            self.wmi_reg_write(reg, val, 280 + i)
        cck_cur = self.wmi_reg_read(0xA208, 288) or 0
        self.wmi_reg_write(0xA208, (0x803E68C8 & 0x3F) | (cck_cur & ~0x3F), 289)
        # --- CHAIN MASKS (1x1) ---
        self.wmi_reg_write(0x99A4, 0x00000001, 250)              # AR_PHY_RX_CHAINMASK
        self.wmi_reg_write(0xA39C, 0x00000001, 251)              # AR_PHY_CAL_CHAINMASK
        # --- set_rfmode: AR_PHY_MODE = 2.4 GHz dynamic (OFDM+CCK) ---
        self.wmi_reg_write(0xA200, 0x00000004, 252)
        self.wmi_reg_write(0x9804, 0x000003C0, 256)              # HT_EN|SHORT_GI_40|SINGLE_HT_LTF1|WALSH
        # --- reset_opmode essentials ---
        self.wmi_reg_write(0x8058, 0x00000000, 253)              # AR_DEF_ANTENNA
        self.wmi_reg_write(0x0080, 0xFFFFFFFF, 254)              # AR_ISR = ~0 (clear)
        self.wmi_reg_write(0x8018, 0x00000007, 255)              # AR_RSSI_THR
        # --- EEPROM HEALTH PROBE (safe: a plain MAC register, no 0x2000 access) ---
        eep_stat = self.wmi_reg_read(0x407C, 256)
        if eep_stat is None:
            log.info("AR9271: 0x407c read FAILED (no response)")
        else:
            log.debug("0x407c=0x%08x (busy/prot/absent bits 0x%x)", eep_stat, eep_stat & 0x000F0000)
        # ===== REAL set_board_values from this board's 4K EEPROM =====
        self.eep = self.read_eeprom4k()
        if self.eep is not None:
            self.mac = self.eep.mac
            self.apply_board_values4k(self.eep)
        else:
            log.info("AR9271: EEPROM unreadable, leaving INI defaults")
        log.debug("antenna: 0x9960=%s 0x9964=%s 0x99ac=%s", self.wmi_reg_read(0x9960, 780),
                  self.wmi_reg_read(0x9964, 781), self.wmi_reg_read(0x99AC, 782))
        # ANT_EEPROM_READ is off on Android (safe mode, no 0x2000 reads).

        # --- CHANNEL SET + PHY ACTIVATE (channel 6 = 2437 MHz) ---
        self.on_status("tuning ch6")
        freq = 2437
        chan_sel = (freq * 0x10000) // 15                        # CHANSEL_2G
        synth_old = self.wmi_reg_read(0x9874, 200) or 0
        synth = (synth_old & 0xC0000000) | 0x30000000 | chan_sel
        self.wmi_reg_write(0x9874, synth, 201)                   # AR_PHY_SYNTH_CONTROL
        self.set_delta_slope(2437)
        # --- init_queues: DQCUMASK(i) = 1<<i ---
        for i in range(10):
            self.wmi_reg_write(0x1000 + (i << 2), 1 << i, 260 + i)
        # --- STA_ID1 |= PRESERVE_SEQNUM ---
        sid1 = self.wmi_reg_read(0x8004, 271) or 0
        self.wmi_reg_write(0x8004, sid1 | 0x20000000, 272)
        # --- set_dma: AHB prefetch, TX/RX DMA 128B, RX FIFO threshold ---
        ahb = self.wmi_reg_read(0x4024, 273) or 0
        self.wmi_reg_write(0x4024, ahb | 0x4, 274)               # AR_AHB_MODE
        txc = self.wmi_reg_read(0x0030, 275) or 0
        self.wmi_reg_write(0x0030, (txc & ~0x7) | 5, 276)        # AR_TXCFG DMASZ 128B
        rxc = self.wmi_reg_read(0x0034, 277) or 0
        self.wmi_reg_write(0x0034, (rxc & ~0x7) | 5, 278)        # AR_RXCFG DMASZ 128B
        self.wmi_reg_write(0x8114, 0x00000200, 279)              # AR_RXFIFO_CFG
        # ===== ath9k_hw_init_interrupt_masks() =====
        self.wmi_reg_write(0x00A4, 0x010F0000, 730)              # AR_IMR_S0
        self.wmi_reg_write(0x00A8, 0x010F0000, 731)              # AR_IMR_S1
        self.wmi_reg_write(0x00AC, 0x00800000, 732)              # AR_IMR_S2
        self.wmi_reg_write(0x00A0, 0x81800964, 733)              # AR_IMR
        self.wmi_reg_write(0x4028, 0xFFFFFFFF, 734)              # INTR_SYNC_CAUSE_CLR
        self.wmi_reg_write(0x402C, 0x00023F60, 735)              # INTR_SYNC_ENABLE
        self.wmi_reg_write(0x4034, 0x00000000, 736)              # INTR_SYNC_MASK
        self.wmi_reg_read(0x00A0, 737)
        self.wmi_reg_write(0x981C, 0x00000001, 202)              # AR_PHY_ACTIVE = EN
        time.sleep(0.005)

        # ===== CALIBRATION: AGC offset cal + SELF-MEASURED noise floor (never forced) =====
        self.on_status("calibrating")
        c = 300
        c = self.reg_set(0x9860, 0x1, c)                         # AGC |= CAL (offset cal)
        cal1 = self.wait_clear(0x9860, 0x1, 40, c)
        c += 40
        # ath9k_hw_start_nfcal(update=true): SET ENABLE_NF, CLEAR NO_UPDATE_NF, SET NF (start)
        agc = self.wmi_reg_read(0x9860, 340) or 0
        self.wmi_reg_write(0x9860, ((agc | 0x8000) & ~0x20000) | 0x2, 341)
        nf = self.wait_clear(0x9860, 0x00000002, 60, c)
        c += 60
        time.sleep(0.3)
        cca = self.wmi_reg_read(0x9864, 349) or 0
        nf_post = (cca & 0x1FF00000) >> 20
        if nf_post & 0x100:
            nf_post -= 0x200
        log.info("AR9271 calibration: AGC=%s NF=%s minCCApwr=%d dBm (AR9271 range -127..-116)", cal1, nf, nf_post)

        # --- FULL RX ENABLE (exact ath9k_htc_start order) ---
        staid1 = self.wmi_reg_read(0x8004, 210) or 0
        self.wmi_reg_write(0x8004, (staid1 & ~(0x10000 | 0x20000)) | 0x10000000, 211)
        # AR_CFG = SWRB|SWTB: RX/TX buffer byte swap for AR9271 over USB (else every word is reversed).
        self.wmi_reg_write(0x0014, 0x0000000A, 209)
        self.wmi_cmd_log(14, "FLUSH_RECV", 207)                  # WMI_FLUSH_RECV
        self.wmi_cmd_log(15, "SET_MODE", 219, bytes([0, 1]))     # HTC_MODE_11NG, before ATH_INIT
        self.wmi_cmd_log(6, "ATH_INIT", 208)                     # registers ISR + enables RX interrupt
        self.wmi_cmd_log(12, "START_RECV", 212)                  # WMI_START_RECV

    # -- EEPROM / board values ----------------------------------------------------------------
    def eep4k(self, word: int, seq: int) -> int:
        """16-bit EEPROM word through the firmware's SEEPROM window (reg 0x2000 + word*4)."""
        v = self.wmi_reg_read(0x2000 + (word << 2), seq)
        return (v if v is not None else -1) & 0xFFFF

    def read_eeprom4k(self) -> Eep4k | None:
        w = []
        for i in range(48):
            v = self.eep4k(64 + i, 600 + i)
            if v < 0:
                log.info("AR9271 EEPROM read failed at word %d", 64 + i)
                return None
            w.append(v)
        b = bytearray(96)
        for i, x in enumerate(w):
            b[i * 2] = x & 0xFF
            b[i * 2 + 1] = (x >> 8) & 0xFF

        def u8(o: int) -> int:
            return b[o]

        def s8(o: int) -> int:
            return b[o] - 256 if b[o] > 127 else b[o]

        def le16(o: int) -> int:
            return b[o] | (b[o + 1] << 8)

        def le32(o: int) -> int:
            return le16(o) | (le16(o + 2) << 16)

        e = Eep4k()
        e.version = le16(4)
        if e.version >> 12 != 0xE:
            log.info("AR9271 EEPROM version 0x%04x unexpected (want major 0xE)", e.version)
            return None
        e.mac = ":".join(f"{u8(i):02x}" for i in range(12, 18))
        e.rx_mask, e.tx_mask = u8(18), u8(19)
        m = 32 + 20                       # base header + custData -> modal header
        e.ant_ctrl_chain0, e.ant_ctrl_common = le32(m + 0), le32(m + 4)
        e.switch_settling, e.tx_rx_atten, e.rx_tx_margin = u8(m + 9), u8(m + 10), u8(m + 11)
        e.adc_desired_size, e.tx_end_to_rx_on, e.thresh62 = s8(m + 12), u8(m + 16), u8(m + 18)
        e.xpa_bias_lvl, e.bsw_atten, e.bsw_margin = u8(m + 27), u8(m + 31), u8(m + 32)
        e.xatten2_db, e.xatten2_margin, e.modal_version = u8(m + 34), u8(m + 35), u8(m + 37)
        e.antdiv_ctl1 = (u8(m + 39) >> 4) & 0xF
        e.antdiv_ctl2 = (u8(m + 41) >> 4) & 0xF
        e.tx_gain_type = u8(31)
        log.info("AR9271 EEPROM 4K: ver=0x%04x mac=%s rx/tx=%d/%d modalVer=%d txGainType=%d",
                 e.version, e.mac, e.rx_mask, e.tx_mask, e.modal_version, e.tx_gain_type)
        return e

    def apply_board_values4k(self, e: Eep4k) -> None:
        """ath9k_hw_4k_set_board_values(): per-board calibration, including antenna diversity."""
        def rmw(reg: int, mask: int, shift: int, v: int, seq: int) -> None:
            self.wmi_reg_rmw(reg, (v << shift) & mask, mask, seq)

        self.wmi_reg_write(0x9964, e.ant_ctrl_common, 700)
        self.wmi_reg_write(0x9960, e.ant_ctrl_chain0, 701)
        # EEPROM GAIN BLOCK deliberately skipped (it zeroes 0xa20c's XATTEN1_MARGIN on this board);
        # the INI values stay, which matches a working ath9k capture.
        self.wmi_reg_read(0xA20C, 702)
        self.wmi_reg_read(0x9848, 704)
        if e.modal_version >= 3:
            a1, a2 = e.antdiv_ctl1, e.antdiv_ctl2
            v = (((a1 << 24) & 0x01000000) | ((a2 << 25) & 0x06000000) |
                 (((a2 >> 2) << 27) & 0x18000000) | (((a1 >> 1) << 29) & 0x20000000) |
                 (((a1 >> 2) << 30) & 0x40000000))
            self.wmi_reg_rmw(0x99AC, v, 0x7F000000, 715)
            self.wmi_reg_rmw(0xA208, ((a1 >> 3) << 13) & 0x2000, 0x2000, 717)
            self.wmi_reg_read(0x99AC, 718)
        rmw(0x9844, 0x00003F80, 7, e.switch_settling, 720)
        rmw(0x9850, 0x000000FF, 0, e.adc_desired_size & 0xFF, 722)
        rmw(0x9828, 0x00FF0000, 16, e.tx_end_to_rx_on, 724)
        rmw(0x9864, 0x000FF000, 12, e.thresh62, 726)
        rmw(0x99B8, 0x000000FF, 0, e.thresh62, 728)
        # ar9002_hw_init_mode_gain_regs(): table picked by EEPROM txGainType (it also programs the
        # analog RF bank). The INI pass wrote the high-power table.
        if e.tx_gain_type != 1:
            self.write_array(INI_TXGAIN_NORMAL_2G, "txGain(NORMAL, from EEPROM)", 790)

    # -- channel ---------------------------------------------------------------------------------
    def set_delta_slope(self, chan_mhz: int) -> None:
        """ar5008_hw_set_delta_slope(): OFDM symbol timing coefficients."""
        def vals(cs: int) -> tuple[int, int]:
            e = 31
            while e > 0:
                if (cs >> e) & 1 == 1:
                    break
                e -= 1
            e = 14 - (e - 24)
            man = cs + (1 << (24 - e - 1))
            return man >> (24 - e), e - 16

        cs = 0x64000000 // chan_mhz
        m1, e1 = vals(cs)
        v = self.wmi_reg_read(0x9814, 730) or 0
        v = (v & ~0xFFFE0000) | ((m1 << 17) & 0xFFFE0000)
        v = (v & ~0x0001E000) | ((e1 << 13) & 0x0001E000)
        self.wmi_reg_write(0x9814, v, 731)
        m2, e2 = vals((9 * cs) // 10)
        h = self.wmi_reg_read(0x99D0, 732) or 0
        h = (h & ~0x0007FFF0) | ((m2 << 4) & 0x0007FFF0)
        h = (h & ~0x0000000F) | (e2 & 0x0000000F)
        self.wmi_reg_write(0x99D0, h, 733)

    def ani_set(self, ofdm_weak: bool, firstep_lvl: int, spur_lvl: int) -> None:
        """ar5008_hw_ani_control(): FIRSTEP value = level*2, SPUR value = (level+1)*2."""
        def rmw(reg: int, mask: int, shift: int, v: int, seq: int) -> None:
            self.wmi_reg_rmw(reg, (v << shift) & mask, mask, seq)

        if not ofdm_weak:
            rmw(0x986C, 0x001FC000, 14, 127, 740)
            rmw(0x986C, 0x0FE00000, 21, 127, 742)
            rmw(0x986C, 0x00003F00, 8, 63, 744)
            rmw(0x9868, 0x00FE0000, 17, 127, 746)
            rmw(0x9868, 0x7F000000, 24, 127, 748)
            rmw(0x9868, 0x0000001F, 0, 31, 750)
            rmw(0x99C0, 0x001FC000, 14, 127, 752)
            rmw(0x99C0, 0x0FE00000, 21, 127, 754)
            rmw(0x99C0, 0x0000007F, 0, 127, 756)
            rmw(0x99C0, 0x00003F80, 7, 127, 758)
            v = self.wmi_reg_read(0x986C, 760) or 0
            self.wmi_reg_write(0x986C, v & ~1, 761)
        fs = firstep_lvl * 2
        rmw(0x9858, 0x0003F000, 12, fs, 762)
        rmw(0x9840, 0x00000FC0, 6, fs, 764)
        sp = (spur_lvl + 1) * 2
        rmw(0x9924, 0x000000FE, 1, sp, 766)
        rmw(0x99BC, 0x0000FE00, 9, max(sp - 1, 0), 768)

    def hop_to(self, ch: int, seq: int, stop_first: bool = True) -> None:
        """ath9k channel change (ath9k_htc_set_channel): stop RX, retune the synth, re-arm the RX
        MAC state, restart RX. stop_first=False is the reuse safety net's gentle recovery."""
        freq = freq_of(ch)
        chan_sel = (freq * 0x10000) // 15                        # CHANSEL_2G
        synth_old = self.wmi_reg_read(0x9874, seq) or 0
        synth = (synth_old & 0xC0000000) | 0x30000000 | chan_sel # keep bMode|fracMode
        if stop_first:
            self.wmi_cmd_log(4, "DISABLE_INTR", seq + 1)
            self.wmi_cmd_log(11, "DRAIN_TXQ_ALL", seq + 2)
            self.wmi_cmd_log(13, "STOP_RECV", seq + 3)
        self.wmi_reg_write(0x9874, synth, seq + 4)               # AR_PHY_SYNTH_CONTROL
        self.wmi_reg_write(0x9804, 0x000003C0, seq + 5)
        self.set_delta_slope(freq)
        self.wmi_reg_write(0x0008, 0x00000004, seq + 6)
        self.wmi_reg_write(0x803C, RX_FILTER_MONITOR, seq + 7)
        self.wmi_reg_write(0x8040, 0xFFFFFFFF, seq + 8)
        self.wmi_reg_write(0x8044, 0xFFFFFFFF, seq + 9)
        dg = self.wmi_reg_read(0x8048, seq + 10) or 0
        self.wmi_reg_write(0x8048, dg & ~(0x20 | 0x02000000), seq + 11)
        self.wmi_cmd_log(12, "START_RECV", seq + 12)
        if stop_first:
            self.wmi_cmd_log(15, "SET_MODE", seq + 13, bytes([0, 1]))
        self.wmi_cmd_log(5, "ENABLE_INTR", seq + 14)
        self.channel = ch

    def target_stats(self, el: int) -> str:
        """Firmware counters: WMI INT_STATS (28) and RX_STATS (30)."""
        ci = build_wmi_cmd(1, 28, 900 + el)
        self.ctrl_out(EP_REG_OUT, ci, 400)
        ri = self.read_once(EP_REG_IN, 400)
        cr = build_wmi_cmd(1, 30, 950 + el)
        self.ctrl_out(EP_REG_OUT, cr, 400)
        rr = self.read_once(EP_REG_IN, 400)
        if ri is not None and len(ri) >= 24 and rr is not None and len(rr) >= 24:
            return (f"ast_rx={_be32(ri, 12)} rxorn={_be32(ri, 16)} nobuf={_be32(rr, 12)} "
                    f"send={_be32(rr, 16)} done={_be32(rr, 20)}")
        return ""

    # -- RX ----------------------------------------------------------------------------------------
    def parse(self, buf: bytes, n: int | None = None) -> int:
        """HTC(8B) on ep3 -> ath_htc_rx_status(40B) -> 802.11. Resyncs on bad headers."""
        n = len(buf) if n is None else n
        stats = self.stats
        frames = 0

        def handle_htc(ps: int, plen: int) -> None:
            nonlocal frames
            if plen < HTC_RX_FRAME_HEADER_SIZE:
                return
            frames += 1
            status = buf[ps + 10]
            stats[0] += 1
            if status == 0:
                stats[1] += 1
            if status & 0x01:
                stats[2] += 1
            if status & 0x02:
                stats[3] += 1
            rssi = buf[ps + 12]
            if rssi > 127:
                rssi -= 256
            dlen = (buf[ps + 8] << 8) | buf[ps + 9]
            if status == 0 and dlen > stats[4]:
                stats[4] = dlen
            if status & 0x02:
                return                              # PHY error: unparseable
            d = ps + HTC_RX_FRAME_HEADER_SIZE
            if d + 24 > n or dlen < 24:
                return
            # ONLY FCS-GOOD FRAMES: a corrupt MAC would be a phantom device.
            if status != 0:
                stats[5] += 1
                return
            end = min(n, d + dlen)                  # bound to THIS frame, not the whole transfer
            self.frames += 1
            dbm = max(-120, min(-20, rssi - RSSI_NOISE_REF))
            self.on_frame(_to_80211(bytes(buf[d:end]), end == d + dlen), dbm, self.channel)

        off = 0
        while off + 4 <= n:
            # ath9k_hif_usb RX stream mode: [pkt_len LE16][0x4e00 LE16][HTC frame][pad to 4]
            if (buf[off + 2] | (buf[off + 3] << 8)) == 0x4E00:
                pkt_len = buf[off] | (buf[off + 1] << 8)
                if 12 <= pkt_len <= 1700 and off + 4 + pkt_len <= n:
                    hs = off + 4
                    if buf[hs] == 3:
                        hlen = (buf[hs + 2] << 8) | buf[hs + 3]
                        if buf[hs + 1] & 0x02:      # HTC_FLAGS_RECV_TRAILER
                            hlen -= buf[hs + 4]
                        if hlen > 0 and hlen + 8 <= pkt_len + 4:
                            handle_htc(hs + 8, hlen)
                    nxt = off + 4 + pkt_len
                    nxt += (4 - (nxt & 3)) & 3
                    off = nxt
                    continue
            # bare HTC frame (no stream wrapper)
            if off + 8 <= n and 1 <= buf[off] <= 9:
                plen = (buf[off + 2] << 8) | buf[off + 3]
                if 12 <= plen <= 1700 and off + 8 + plen <= n:
                    pl = plen
                    if buf[off + 1] & 0x02:
                        pl -= buf[off + 4]
                    if buf[off] == 3 and pl > 0:
                        handle_htc(off + 8, pl)
                    nxt = off + 8 + plen
                    nxt += (4 - (nxt & 3)) & 3
                    off = nxt
                    continue
            off += 1
        return frames

    def _start_rx(self) -> None:
        """ath9k_hif_usb_alloc_rx_urbs(): MAX_RX_URB_NUM (8) transfers permanently queued on 0x82,
        each resubmitted the instant it completes, BEFORE parsing. Events are handled on a
        dedicated thread so the blocking WMI hop traffic never starves the drain (on Android the
        RX reader is its own thread for the same reason)."""
        self._rx_stop.clear()

        def done(t) -> None:
            st = t.getStatus()
            data = None
            if st == usb1.TRANSFER_COMPLETED:
                n = t.getActualLength()
                if n:
                    data = bytes(t.getBuffer()[:n])  # private copy before the buffer goes back on the wire
            if st in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT) and not self._rx_stop.is_set():
                self._resubmit(t)
            elif st == usb1.TRANSFER_NO_DEVICE:
                self._mark_gone("RX: no device")
            if data:
                self.bytes += len(data)
                self.last_rx = time.monotonic()
                try:
                    self.parse(data)
                except Exception:
                    log.debug("parse error", exc_info=True)

        self._rx_done = done
        for _ in range(RX_URBS):
            t = self.h.getTransfer()
            t.setBulk(EP_RX, RX_URB_SIZE, callback=done, timeout=0)
            t.submit()
            self._rx_transfers.append(t)
        log.info("AR9271 RX: %d async transfers queued on 0x82 (hif_usb model)", len(self._rx_transfers))
        self.last_rx = time.monotonic()

        def pump() -> None:
            while not self._rx_stop.is_set():
                try:
                    self.ctx.handleEventsTimeout(0.05)
                except Exception:
                    if self.gone:
                        break
                    time.sleep(0.01)

        self._rx_thread = threading.Thread(target=pump, name="ar9271-rx", daemon=True)
        self._rx_thread.start()

    def _resubmit(self, t) -> None:
        try:
            t.submit()
            return
        except usb1.USBErrorNoDevice:
            self._mark_gone("RX resubmit: no device")
            return
        except Exception:
            pass
        # Resubmit failed: REBUILD the transfer so the shallow 8-deep pool never bleeds down.
        with self._rx_lock:
            try:
                self._rx_transfers.remove(t)
            except ValueError:
                pass
            try:
                nt = self.h.getTransfer()
                nt.setBulk(EP_RX, RX_URB_SIZE, callback=self._rx_done, timeout=0)
                nt.submit()
                self._rx_transfers.append(nt)
            except Exception:
                log.debug("RX transfer rebuild failed", exc_info=True)

    def _stop_rx(self) -> None:
        if not self._rx_transfers:
            return
        self._rx_stop.set()
        for t in list(self._rx_transfers):
            try:
                t.cancel()
            except Exception:
                pass
        if self._rx_thread:
            self._rx_thread.join(2)
        end = time.monotonic() + 3
        while time.monotonic() < end and any(t.isSubmitted() for t in self._rx_transfers):
            try:
                self.ctx.handleEventsTimeout(0.05)
            except Exception:
                time.sleep(0.05)
        self._rx_transfers = []

    def run(self, hop: tuple[int, ...] = HOP_CHANNELS, dwell: float = HOP_DWELL_S) -> None:
        """Start the RX reader, then the LIVE loop: hop every ``dwell`` seconds until stop_evt
        is set or the adapter is unplugged."""
        self._start_rx()
        log.info("AR9271 LIVE monitor%s", " (adopted firmware)" if self.reuse else "")
        reuse = self.reuse
        started = time.monotonic()
        next_tick = started + 5
        hop_idx = 0
        next_hop = started + dwell
        # ===== REUSE HOP SAFETY NET =====
        # A hop on adopted firmware that is not followed by ANY frame within REUSE_STALL_S parks
        # hopping: one gentle recovery (re-tune to the last channel that produced frames + START_RECV
        # + ENABLE_INTR, no STOP_RECV), then stay there. Cold sessions are not watched.
        hop_parked = False
        hop_pending_at = 0.0
        last_hop_at = 0.0
        last_frames = self.stats[0]
        good_ch = hop[0]
        parked_at = 0.0
        parked_frames = 0
        park_reported = False

        def do_hop(idx: int) -> None:
            nonlocal last_hop_at, hop_pending_at
            try:
                self.hop_to(hop[idx], 1000 + idx * 20)
            except Exception:
                log.debug("hop failed", exc_info=True)
            t = time.monotonic()
            last_hop_at = t
            if reuse and hop_pending_at == 0.0:
                hop_pending_at = t

        self.channel = hop[0]
        if reuse:
            log.info("AR9271 REUSE: channel-hopping on adopted firmware (stall safety net armed, %.0f s)", REUSE_STALL_S)
        do_hop(0)
        self.on_status("monitor mode live")
        try:
            while not self._canceled():
                now = time.monotonic()
                f_now = self.stats[0]
                if f_now != last_frames:
                    last_frames = f_now
                    hop_pending_at = 0.0
                    if now - last_hop_at > 0.15:
                        good_ch = self.channel
                if reuse and not hop_parked and hop_pending_at and now - hop_pending_at >= REUSE_STALL_S:
                    log.info("AR9271 REUSE: no RX frames %.1f s after a hop (ch=%d), hopping unsafe this session; "
                             "re-tune ch %d + START_RECV + ENABLE_INTR (no STOP_RECV), then park",
                             now - hop_pending_at, self.channel, good_ch)
                    try:
                        self.hop_to(good_ch, 1400, stop_first=False)
                    except Exception:
                        log.debug("recovery hop failed", exc_info=True)
                    hop_parked = True
                    next_hop = float("inf")
                    hop_idx = hop.index(good_ch) if good_ch in hop else 0
                    parked_at, parked_frames = now, self.stats[0]
                    self.on_status(f"parked on ch {self.channel} (replug to sweep)")
                if hop_parked and not park_reported and now - parked_at >= REUSE_STALL_S:
                    park_reported = True
                    got = self.stats[0] - parked_frames
                    if got > 0:
                        log.info("AR9271 REUSE: frames resumed while parked on ch %d (+%d)", self.channel, got)
                    else:
                        log.info("AR9271 REUSE: still no frames after recovery on ch %d, replug to recover", self.channel)
                if now >= next_hop:
                    ni = (hop_idx + 1) % len(hop) if len(hop) > 1 else hop_idx
                    if ni != hop_idx:
                        hop_idx = ni
                        do_hop(hop_idx)
                    next_hop = now + dwell
                if now >= next_tick:
                    el = int(now - started)
                    tgt = ""
                    try:
                        tgt = self.target_stats(el)
                    except Exception:
                        pass
                    log.debug("AR9271 live t=%ds ch=%d rx=%dB frames=%d good=%d crc=%d phyerr=%d %s", el,
                              self.channel, self.bytes, self.stats[0], self.stats[1], self.stats[2],
                              self.stats[3], tgt)
                    next_tick = now + 5
                time.sleep(0.06)
        finally:
            # Android leaves the radio running when the session ends (the loop only exits on
            # abort, after which every register helper is a no-op), so nothing is written here.
            self._stop_rx()
        if self.gone:
            raise OSError("AR9271: adapter unplugged")
