"""MediaTek MT7612U monitor mode over libusb (Alfa AWUS036ACM, Panda PAU0D and
any other MT7612U adapter).

A userspace port of the Linux mt76x2u driver, the same sequence the Android app
runs: upload the ROM patch and firmware, power the radio on, reset the MAC,
read the EEPROM, load the RF/BBP registers, tune and calibrate, set a
promiscuous RX filter, then read 802.11 frames off the RX bulk endpoint while
hopping every 2.4 and 5 GHz channel.

The adapter has to be on the WinUSB driver for libusb to open it.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("wdgwars.mt7612u")

VID_PIDS = {(0x0E8D, 0x7612), (0x0E8D, 0x7610)}
FIRMWARE_DIR = Path(__file__).resolve().parent / "firmware"

OUT_VENDOR, IN_VENDOR, OUT_CLASS = 0x40, 0xC0, 0x20
DEV_MODE, MULTI_WRITE, MULTI_READ = 0x01, 0x06, 0x07
READ_EEPROM, WRITE_FCE, WRITE_CFG, READ_CFG = 0x09, 0x42, 0x46, 0x47

ASIC_VERSION, MCU_CLOCK_CTL, MCU_COM_REG0 = 0x0000, 0x0708, 0x0730
FCE_PSE_CTRL, FCE_DMA_ADDR, FCE_DMA_LEN = 0x0800, 0x0230, 0x0234
TX_CPU_FROM_FCE_BASE_PTR, TX_CPU_FROM_FCE_MAX_COUNT = 0x09A0, 0x09A4
TX_CPU_FROM_FCE_CPU_DESC_IDX, FCE_PDMA_GLOBAL_CONF, FCE_SKIP_FS = 0x09A8, 0x09C4, 0x0A6C
USB_U3DMA_CFG = 0x9018
USB_DMA_CFG_VAL = (1 << 22) | (1 << 23) | 0x20
ILM_OFFSET, DLM_OFFSET, ROM_PATCH_OFFSET = 0x80000, 0x110000, 0x90000
FW_URB_MAX_PAYLOAD, ROM_PATCH_MAX_PAYLOAD = 0x3900, 2048
FW_HDR_LEN, PATCH_HDR_LEN, REV_E3 = 32, 30, 0x22
DMA_INFO_BASE = (2 << 27) | (1 << 30)

WLAN_FUN_CTRL, MAC_CSR0, MAC_SYS_CTRL, MAC_STATUS = 0x0080, 0x1000, 0x1004, 0x1200
CH_IDLE, CH_BUSY, RX_STAT_0 = 0x1130, 0x1134, 0x1700
WPDMA_GLO_CFG, RX_FILTR_CFG, US_CYC_CFG, TXOP_CTRL_CFG = 0x0208, 0x1400, 0x02A4, 0x1340
COEXCFG0, EXT_CCA_CFG, TX_ALC_CFG_4 = 0x0040, 0x141C, 0x13C0
PBF_TX_MAX_PCNT, PBF_RX_MAX_PCNT, TX_LINK_CFG, AUTO_RSP_CFG = 0x0408, 0x040C, 0x1350, 0x1404
MAX_LEN_CFG, WMM_AIFSN, WMM_CWMIN, WMM_CWMAX = 0x1018, 0x0214, 0x0218, 0x021C
CH_TIME_CFG, TX_PIN_CFG, TXOP_HLDR_ET = 0x110C, 0x1328, 0x1608
XIFS_TIME_CFG, BKOFF_SLOT_CFG, FCE_L2_STUFF = 0x1100, 0x1104, 0x080C
XO_CTRL5, XO_CTRL6, XO_CTRL7, ED_CCA_TIMER = 0x0114, 0x0118, 0x011C, 0x1140
ADDR_504, ADDR_50C, TX_BAND_CFG = 0x0504, 0x050C, 0x132C
BBP_CORE1, BBP_AGC0, BBP_AGC2, BBP_AGC7 = 0x2004, 0x2300, 0x2308, 0x231C
BBP_AGC11, BBP_AGC61, BBP_TXO4, BBP_RXO13 = 0x232C, 0x23F4, 0x2610, 0x2934

WLAN_EN, WLAN_CLK_EN, WLAN_RESET_RF, FRC_WL_ANT_SEL = 1, 2, 4, 1 << 5
SYS_CTRL_RESET_CSR, SYS_CTRL_RESET_BBP = 1, 2
SYS_CTRL_ENABLE_TX, SYS_CTRL_ENABLE_RX = 4, 8
WPDMA_TX_DMA_BUSY, WPDMA_RX_DMA_BUSY = 2, 8
DMA_RX_DROP_OR_PAD, DMA_RX_BULK_AGG_EN = 1 << 18, 1 << 21
DMA_RX_BULK_EN, DMA_TX_BULK_EN = 1 << 22, 1 << 23
FILTR_CRC_ERR, FILTR_PHY_ERR = 1, 2
DMA_HDR, RXWI_LEN = 4, 32
RXINFO_L2PAD, RXINFO_CRCERR = 1 << 14, 1 << 8

EE_SIZE, EE_MAC_ADDR = 512, 0x004
EE_NIC_CONF_0, EE_NIC_CONF_1, EE_NIC_CONF_2 = 0x034, 0x036, 0x042
EE_LNA_GAIN, EE_RSSI_OFFSET_2G_0, EE_RSSI_OFFSET_2G_1, EE_RSSI_OFFSET_5G_0 = 0x044, 0x046, 0x048, 0x04A
EE_XTAL_TRIM_1, EE_XTAL_TRIM_2 = 0x03A, 0x09E

CMD_FUN_SET_OP, CMD_LOAD_CR, CMD_INIT_GAIN_OP = 1, 2, 3
CMD_POWER_SAVING_OP, CMD_SWITCH_CHANNEL_OP, CMD_CALIBRATION_OP = 20, 30, 31
Q_SELECT, RADIO_ON, MT_RF_BBP_CR = 1, 0x31, 2
MCU_CAL_TEMP, MCU_CAL_RXDCOC, MCU_CAL_RC, MCU_CAL_LC = 2, 3, 4, 6
MCU_CAL_TX_LOFT, MCU_CAL_TXIQ, MCU_CAL_RXIQC_FI, MCU_CAL_TX_SHAPING = 7, 8, 12, 15

HOP = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13,
       36, 40, 44, 48, 52, 56, 60, 64,
       100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
       149, 153, 157, 161, 165)
DWELL_S = 0.125
CAL_TTL_S = 30.0

# mt76 init.c MAC init values, verbatim
MAC_INITVALS = (
    (0x0400, 0x00080C00), (0x0404, 0x1EFEBCFF), (0x0800, 0x00000001), (0x1004, 0x00000000),
    (0x1018, 0x003E3F00), (0x1030, 0xAAA99887), (0x1034, 0x000000AA), (0x1100, 0x33A40D0A),
    (0x1104, 0x00000209), (0x1118, 0x00422010), (0x1204, 0x00000000), (0x1238, 0x001700C8),
    (0x1330, 0x00101001), (0x1334, 0x00010000), (0x1338, 0x00000000), (0x1340, 0x0400583F),
    (0x1344, 0x00FFFF20), (0x1348, 0x000A2290), (0x134C, 0x47F01F0F), (0x1380, 0x002C00DC),
    (0x13E0, 0xE3F42004), (0x13E4, 0xE3F42084), (0x13E8, 0xE3F42104), (0x13EC, 0x00060FFF),
    (0x1400, 0x00015F97), (0x1408, 0x0000017F), (0x140C, 0x00004003), (0x150C, 0x00000003),
    (0x1608, 0x00000002), (0x0A44, 0x00000000), (0x0260, 0x00000000), (0x0250, 0x00000000),
    (0x120C, 0x00000000), (0x1264, 0x00000000), (0x13C0, 0x00000000), (0x13C8, 0x00000000),
    (0x1314, 0x3A3A3A3A), (0x1318, 0x3A3A3A3A), (0x131C, 0x3A3A3A3A), (0x1320, 0x3A3A3A3A),
    (0x1324, 0x3A3A3A3A), (0x13D4, 0x3A3A3A3A), (0x13D8, 0x0000003A), (0x13DC, 0x0000003A),
    (0x0024, 0x0000D000), (0x0A38, 0x0000000A), (0x0824, 0x60401C18), (0x0210, 0x94FF0000),
    (0x1478, 0x00000004), (0x1384, 0x00001818), (0x1358, 0xEDCBA980), (0x1648, 0x00830083),
    (0x1410, 0x000001FF), (0x1350, 0x00001020),
)


def _le32(b: bytes, i: int = 0) -> int:
    return int.from_bytes(b[i:i + 4], "little")


def _le16(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def _words(*w: int) -> bytes:
    return b"".join((x & 0xFFFFFFFF).to_bytes(4, "little") for x in w)


def _sext(v: int, bits: int) -> int:
    s = 1 << (bits - 1)
    return v - (1 << bits) if v & s else v


def freq_of(ch: int) -> int:
    return 2407 + ch * 5 if ch <= 14 else 5000 + ch * 5


class Mt7612u:
    """One MT7612U adapter. ``on_frame(frame, rssi, channel)`` gets every FCS-good
    802.11 frame (header first, L2 padding already removed)."""

    def __init__(self, ctx, device, on_frame: Callable[[bytes, int, int], None],
                 on_status: Callable[[str], None] = lambda s: None) -> None:
        self.ctx, self.device = ctx, device
        self.on_frame, self.on_status = on_frame, on_status
        self.h = None
        self.ep_rx = self.ep_resp = self.ep_cmd = 0
        self.stop_evt = threading.Event()
        self.seq = 0
        self.mcu_gain = 0
        self.rssi_off_2g = self.rssi_off_5g = self.lna_2g = self.lna_5g = 0
        self.channel = HOP[0]
        self.last_cal: dict[int, float] = {}
        self.frames = 0
        self.bytes = 0
        self.mcu_no_resp = 0
        self.mac = ""
        self.serial = ""

    # -- USB primitives (mt76 usb.c) -----------------------------------------
    def rd(self, addr: int) -> int:
        try:
            b = self.h.controlRead(IN_VENDOR, MULTI_READ, (addr >> 16) & 0xFFFF, addr & 0xFFFF, 4, timeout=1000)
            return _le32(b) if len(b) == 4 else 0
        except Exception:
            return 0

    def wr(self, addr: int, v: int) -> None:
        self.h.controlWrite(OUT_VENDOR, MULTI_WRITE, (addr >> 16) & 0xFFFF, addr & 0xFFFF,
                            (v & 0xFFFFFFFF).to_bytes(4, "little"), timeout=1000)

    def set_bits(self, addr: int, bits: int) -> None:
        self.wr(addr, self.rd(addr) | bits)

    def clear_bits(self, addr: int, bits: int) -> None:
        self.wr(addr, self.rd(addr) & ~bits)

    def rmw(self, addr: int, mask: int, v: int) -> None:
        self.wr(addr, (self.rd(addr) & ~mask) | v)

    def cfg_wr(self, addr: int, v: int) -> None:
        self.h.controlWrite(OUT_VENDOR, WRITE_CFG, (addr >> 16) & 0xFFFF, addr & 0xFFFF,
                            (v & 0xFFFFFFFF).to_bytes(4, "little"), timeout=1000)

    def cfg_rd(self, addr: int) -> int:
        try:
            b = self.h.controlRead(IN_VENDOR, READ_CFG, (addr >> 16) & 0xFFFF, addr & 0xFFFF, 4, timeout=1000)
            return _le32(b) if len(b) == 4 else 0
        except Exception:
            return 0

    def cfg_set(self, addr: int, bits: int) -> None:
        self.cfg_wr(addr, self.cfg_rd(addr) | bits)

    def cfg_clear(self, addr: int, bits: int) -> None:
        self.cfg_wr(addr, self.cfg_rd(addr) & ~bits)

    def vendor(self, req: int, req_type: int, value: int, index: int, data: bytes = b"") -> None:
        self.h.controlWrite(req_type, req, value, index, data, timeout=1000)

    def single_wr(self, req: int, offset: int, v: int) -> None:
        self.h.controlWrite(OUT_VENDOR, req, v & 0xFFFF, offset, b"", timeout=1000)
        self.h.controlWrite(OUT_VENDOR, req, (v >> 16) & 0xFFFF, offset + 2, b"", timeout=1000)

    def poll(self, addr: int, mask: int, want: int, seconds: float, cfg: bool = False) -> bool:
        end = time.monotonic() + seconds
        while True:
            if ((self.cfg_rd(addr) if cfg else self.rd(addr)) & mask) == want:
                return True
            if time.monotonic() > end:
                return False
            time.sleep(0.005)

    def clear_halt(self, ep: int) -> None:
        try:
            self.h.clearHalt(ep)
        except Exception:
            pass

    # -- open / close ----------------------------------------------------------
    def open(self) -> None:
        self.h = self.device.open()
        try:
            self.serial = self.device.getSerialNumber() or ""
        except Exception:
            self.serial = ""
        self.h.claimInterface(0)
        ins, outs = [], []
        for setting in self.device[0][0]:
            for ep in setting:
                addr = ep.getAddress()
                if ep.getAttributes() & 3 == 2:
                    (ins if addr & 0x80 else outs).append(addr)
        if len(ins) < 2 or not outs:
            raise OSError("MT7612U: missing bulk endpoints")
        self.ep_rx, self.ep_resp, self.ep_cmd = ins[0], ins[1], outs[0]
        for ep in (self.ep_rx, self.ep_resp, self.ep_cmd):
            self.clear_halt(ep)

    def close(self) -> None:
        # Closing the handle cancels every transfer still in flight and waits for it
        # before the interface goes away. Releasing the interface first, with RX
        # transfers outstanding (an adapter that errored mid-run), corrupts memory.
        self.stop_evt.set()
        if self.h:
            try:
                self.h.close()
            except Exception:
                pass
            self.h = None

    # -- firmware (mt76x2/usb_mcu.c) ---------------------------------------------
    def load_firmware(self) -> None:
        rev = self.rd(ASIC_VERSION) & 0xFFFF
        e3 = rev >= REV_E3
        if self.rd(MCU_COM_REG0) & 1:
            # Firmware still running from an earlier session (the adapter stayed powered):
            # re-uploading to a live MCU stalls, so hand straight off to bring-up.
            log.info("MT7612U: firmware already running, skipping the upload")
            return
        patch = (FIRMWARE_DIR / "mt7662_rom_patch.bin").read_bytes()
        fw = (FIRMWARE_DIR / "mt7662.bin").read_bytes()
        self.on_status("uploading ROM patch")
        self.cfg_wr(USB_U3DMA_CFG, USB_DMA_CFG_VAL)
        self.vendor(DEV_MODE, OUT_VENDOR, 0x1, 0)
        time.sleep(0.008)
        self._configure_fce()
        self._send_fw(patch, PATCH_HDR_LEN, len(patch) - PATCH_HDR_LEN, ROM_PATCH_MAX_PAYLOAD, ROM_PATCH_OFFSET)
        self.vendor(DEV_MODE, OUT_CLASS, 0x12, 0, bytes([0x6F, 0xFC, 0x08, 0x01, 0x20, 0x04, 0, 0, 0, 0x09, 0]))
        self.vendor(DEV_MODE, OUT_CLASS, 0x12, 0, bytes([0x6F, 0xFC, 0x05, 0x01, 0x07, 0x01, 0, 0x04]))
        time.sleep(0.02)
        self.poll(MCU_CLOCK_CTL if e3 else MCU_COM_REG0, 1 if e3 else 2, 1 if e3 else 2, 0.1)

        ilm, dlm = _le32(fw, 0), _le32(fw, 4)
        if FW_HDR_LEN + ilm + dlm != len(fw):
            raise OSError("MT7612U: bad firmware file")
        self.on_status("uploading firmware")
        self.vendor(DEV_MODE, OUT_VENDOR, 0x1, 0)
        time.sleep(0.008)
        self.cfg_wr(USB_U3DMA_CFG, USB_DMA_CFG_VAL)
        self._configure_fce()
        self._send_fw(fw, FW_HDR_LEN, ilm, FW_URB_MAX_PAYLOAD, ILM_OFFSET)
        self._send_fw(fw, FW_HDR_LEN + ilm, dlm, FW_URB_MAX_PAYLOAD, DLM_OFFSET + (0x800 if e3 else 0))
        self.vendor(DEV_MODE, OUT_VENDOR, 0x12, 0)      # load IVB: start the firmware
        if not self.poll(MCU_COM_REG0, 1, 1, 0.1):
            raise OSError("MT7612U: firmware failed to start")
        self.set_bits(MCU_COM_REG0, 0x2)
        self.wr(FCE_PSE_CTRL, 0x1)
        log.info("MT7612U firmware running")

    def _configure_fce(self) -> None:
        self.wr(FCE_PSE_CTRL, 0x1)
        self.wr(TX_CPU_FROM_FCE_BASE_PTR, 0x400230)
        self.wr(TX_CPU_FROM_FCE_MAX_COUNT, 0x1)
        self.wr(FCE_PDMA_GLOBAL_CONF, 0x44)
        self.wr(FCE_SKIP_FS, 0x3)

    def _send_fw(self, src: bytes, off: int, length: int, max_payload: int, dst: int) -> None:
        max_len = max_payload - 8
        pos = 0
        while pos < length:
            n = min(length - pos, max_len)
            rounded = (n + 3) & ~3
            buf = bytearray(4 + rounded + 4)
            buf[0:4] = (DMA_INFO_BASE | (n & 0xFFFF)).to_bytes(4, "little")
            buf[4:4 + n] = src[off + pos:off + pos + n]
            self.single_wr(WRITE_FCE, FCE_DMA_ADDR, dst + pos)
            self.single_wr(WRITE_FCE, FCE_DMA_LEN, rounded << 16)
            try:
                self.h.bulkWrite(self.ep_cmd, bytes(buf), timeout=1000)
            except Exception:
                self.clear_halt(self.ep_cmd)
                time.sleep(0.005)
                self.h.bulkWrite(self.ep_cmd, bytes(buf), timeout=1000)
            self.wr(TX_CPU_FROM_FCE_CPU_DESC_IDX, self.rd(TX_CPU_FROM_FCE_CPU_DESC_IDX) + 1)
            pos += n
            time.sleep(0.005)

    # -- MCU commands -------------------------------------------------------------
    def mcu(self, cmd: int, payload: bytes, wait: bool) -> None:
        self.seq = (self.seq + 1) & 0xF or 1
        rounded = (len(payload) + 3) & ~3
        info = (rounded & 0xFFFF) | (2 << 27) | (self.seq << 16) | (cmd << 20) | (1 << 30)
        buf = info.to_bytes(4, "little") + payload + bytes(rounded - len(payload) + 4)
        self.h.bulkWrite(self.ep_cmd, buf, timeout=1000)
        if wait:
            try:
                self.h.bulkRead(self.ep_resp, 512, timeout=300)
            except Exception:
                self.mcu_no_resp += 1

    def drain_resp(self) -> None:
        for _ in range(6):
            try:
                if not self.h.bulkRead(self.ep_resp, 512, timeout=1):
                    return
            except Exception:
                return

    # -- bring-up (mt76x2/usb_init.c, usb_mac.c) ---------------------------------
    def bring_up(self) -> None:
        self.on_status("powering on the radio")
        v = self.rd(WLAN_FUN_CTRL) & ~FRC_WL_ANT_SEL
        if v & WLAN_EN:
            self.wr(WLAN_FUN_CTRL, v | WLAN_RESET_RF)
            time.sleep(0.001)
            v &= ~WLAN_RESET_RF
        self.wr(WLAN_FUN_CTRL, v)
        for bit in (WLAN_EN, WLAN_CLK_EN):
            v |= bit
            self.wr(WLAN_FUN_CTRL, v)
            time.sleep(0.001)
        self._power_on()
        end = time.monotonic() + 1.0
        while True:
            m = self.rd(MAC_CSR0)
            if m not in (0, 0xFFFFFFFF):
                break
            if time.monotonic() > end:
                raise OSError("MT7612U: MAC not ready after power-on")
            time.sleep(0.005)
        v = self.cfg_rd(USB_U3DMA_CFG)
        v = (v | DMA_RX_DROP_OR_PAD | DMA_RX_BULK_EN | DMA_TX_BULK_EN) & ~DMA_RX_BULK_AGG_EN
        self.cfg_wr(USB_U3DMA_CFG, v)
        self.mcu(CMD_FUN_SET_OP, _words(Q_SELECT, 1), False)
        self.mcu(CMD_POWER_SAVING_OP, _words(RADIO_ON, 0), False)

        self.on_status("resetting the MAC")
        self.wr(WPDMA_GLO_CFG, (1 << 4) | (1 << 5))
        self.wr(PBF_TX_MAX_PCNT, 0xEFEF3F1F)
        self.wr(PBF_RX_MAX_PCNT, 0xFEBF)
        for a, val in MAC_INITVALS:
            self.wr(a, val)
        self.wr(TX_LINK_CFG, 0x1020)
        self.wr(AUTO_RSP_CFG, 0x13)
        self.wr(MAX_LEN_CFG, 0x2F00)
        self.wr(WMM_AIFSN, 0x2273)
        self.wr(WMM_CWMIN, 0x2344)
        self.wr(WMM_CWMAX, 0x34AA)
        self.clear_bits(MAC_SYS_CTRL, SYS_CTRL_RESET_CSR | SYS_CTRL_RESET_BBP)
        self.clear_bits(COEXCFG0, 1)
        self.set_bits(EXT_CCA_CFG, 0xF000)
        self.clear_bits(TX_ALC_CFG_4, 1 << 31)

        ee = self._read_eeprom()
        self.mac = ":".join(f"{x:02x}" for x in ee[EE_MAC_ADDR:EE_MAC_ADDR + 6])
        self._rx_gain(ee)
        self._fixup_xtal(ee)
        self.rmw(US_CYC_CFG, 0xFF, 0x1E)
        self.wr(TXOP_CTRL_CFG, 0x583F)

        self.on_status("loading RF registers")
        nc0, nc1 = _le16(ee, EE_NIC_CONF_0), _le16(ee, EE_NIC_CONF_1)
        cfg = (1 << 31) | ((nc0 >> 8) & 0xFF) | ((nc1 << 8) & 0xFF00)
        m = bytearray(8)
        m[0], m[2] = MT_RF_BBP_CR, HOP[0]
        m[4:8] = cfg.to_bytes(4, "little")
        self.mcu(CMD_LOAD_CR, bytes(m), True)

        self.on_status("tuning and calibrating")
        v = self.rd(BBP_AGC0)
        self.wr(BBP_AGC0, (v & ~(1 << 4)) | (1 << 3))      # both RX chains
        self.tune(HOP[0], scan=False)

        self.wr(RX_FILTR_CFG, FILTR_CRC_ERR | FILTR_PHY_ERR)   # promiscuous, good frames only
        self.wr(MAC_SYS_CTRL, SYS_CTRL_ENABLE_TX)
        self._wait_wpdma()
        self.wr(RX_FILTR_CFG, FILTR_CRC_ERR | FILTR_PHY_ERR)
        self.wr(MAC_SYS_CTRL, SYS_CTRL_ENABLE_TX | SYS_CTRL_ENABLE_RX)
        self._wait_wpdma()
        self.wr(CH_TIME_CFG, 0x15F)
        self.rd(CH_BUSY)
        self.rd(CH_IDLE)
        sys_ctrl = self.rd(MAC_SYS_CTRL)
        if sys_ctrl in (0, 0xFFFFFFFF) or not sys_ctrl & SYS_CTRL_ENABLE_RX:
            raise OSError(f"MT7612U: bring-up incomplete (SYS_CTRL=0x{sys_ctrl:08x})")
        if self.mcu_no_resp >= 3:
            self.on_status("live but uncalibrated: unplug and replug the adapter to reset it")
        else:
            self.on_status("monitor mode live")

    def _power_on(self) -> None:
        self.cfg_set(0x148, 1)
        up = (1 << 28) | (1 << 12) | (1 << 13)
        self.poll(0x148, up, up, 1.0, cfg=True)
        self.cfg_clear(0x148, 0x7F << 16)
        time.sleep(0.001)
        self.cfg_clear(0x148, 0xF << 24)
        time.sleep(0.001)
        self.cfg_set(0x148, 0xF << 24)
        self.cfg_clear(0x148, 0xFFF)
        self.cfg_clear(0x1204, 1 << 3)
        self.cfg_set(0x80, 1)
        self.cfg_clear(0x64, 1 << 18)
        for unit in (0, 1):
            s = 8 if unit else 0
            self.cfg_set(0x130, 1 << s)
            time.sleep(0.001)
            self.cfg_set(0x130, ((1 << 1) | (1 << 3) | (1 << 4) | (1 << 5)) << s)
            time.sleep(0.001)
            self.cfg_clear(0x130, (1 << 2) << s)
            time.sleep(0.001)
            self.cfg_set(0x130, 1 | (1 << 16))
            time.sleep(0.001)
            self.cfg_clear(0x1C, 0xFF)
            self.cfg_set(0x1C, 0x30)
            self.cfg_wr(0x14, 0x484F)
            time.sleep(0.001)
            self.cfg_set(0x130, 1 << 17)
            time.sleep(0.001)
            self.cfg_clear(0x130, 1 << 16)
            time.sleep(0.001)
            self.cfg_set(0x14C, (1 << 19) | (1 << 20))
            self.wr(0x530, 0xF)

    def _wait_wpdma(self) -> None:
        end = time.monotonic() + 1.0
        while self.rd(WPDMA_GLO_CFG) & (WPDMA_TX_DMA_BUSY | WPDMA_RX_DMA_BUSY) and time.monotonic() < end:
            time.sleep(0.002)

    def _read_eeprom(self) -> bytes:
        ee = bytearray(EE_SIZE)
        for i in range(0, EE_SIZE, 4):
            try:
                b = self.h.controlRead(IN_VENDOR, READ_EEPROM, (i >> 16) & 0xFFFF, i & 0xFFFF, 4, timeout=1000)
                if len(b) == 4:
                    ee[i:i + 4] = b
            except Exception:
                pass
        return bytes(ee)

    def _rx_gain(self, ee: bytes) -> None:
        v044 = _le16(ee, EE_LNA_GAIN)
        l2, l50 = v044 & 0xFF, (v044 >> 8) & 0xFF
        l51 = (_le16(ee, EE_RSSI_OFFSET_2G_1) >> 8) & 0xFF
        if l51 in (0, 0xFF):
            l51 = l50
        self.mcu_gain = l2 | (l50 << 8) | (l51 << 16) | (l50 << 24)

        def off(v: int) -> int:
            b0 = v & 0xFF
            return 0 if b0 in (0, 0xFF) or not b0 & 0x80 else _sext(b0, 7)

        def lna(v: int) -> int:
            b0 = v & 0xFF
            return 0 if b0 == 0xFF else _sext(b0, 8)

        self.rssi_off_2g = off(_le16(ee, EE_RSSI_OFFSET_2G_0))
        self.rssi_off_5g = off(_le16(ee, EE_RSSI_OFFSET_5G_0))
        self.lna_2g = lna(v044)
        self.lna_5g = lna(v044 >> 8)

    def _fixup_xtal(self, ee: bytes) -> None:
        ev = _le16(ee, EE_XTAL_TRIM_2)
        offset = ev & 0x7F
        if ev & 0xFF == 0xFF:
            offset = 0
        elif ev & 0x80:
            offset = -offset
        ev >>= 8
        if ev in (0x00, 0xFF):
            ev = _le16(ee, EE_XTAL_TRIM_1) & 0xFF
            if ev in (0x00, 0xFF):
                ev = 0x14
        c2 = ((ev & 0x7F) + offset) & 0x7F
        self.cfg_wr(XO_CTRL5, (self.cfg_rd(XO_CTRL5) & ~(0x7F << 8)) | (c2 << 8))
        self.cfg_set(XO_CTRL6, 0x7F << 8)
        self.wr(ADDR_504, 0x06000000)
        self.wr(ADDR_50C, 0x08800000)
        time.sleep(0.005)
        self.wr(ADDR_504, 0)
        self.rmw(XIFS_TIME_CFG, 0xFF << 8, 0xD << 8)
        self.rmw(BKOFF_SLOT_CFG, 0xF << 8, 1 << 8)
        self.clear_bits(FCE_L2_STUFF, 1 << 4)
        sel = (_le16(ee, EE_NIC_CONF_2) >> 9) & 0x3
        if sel == 0:
            self.wr(XO_CTRL7, 0x5C1FEE80)
        elif sel == 1:
            self.wr(XO_CTRL7, 0x5C1FEED0)

    # -- channel (mt76x2/usb_phy.c) ------------------------------------------------
    def tune(self, ch: int, scan: bool) -> None:
        five = ch > 14
        if five:
            self.clear_bits(TX_BAND_CFG, 1 << 2)
            self.set_bits(TX_BAND_CFG, 1 << 1)
        else:
            self.set_bits(TX_BAND_CFG, 1 << 2)
            self.clear_bits(TX_BAND_CFG, 1 << 1)
        self.rmw(BBP_CORE1, 0x18, 0)
        self.rmw(BBP_AGC0, 0x7000, 1 << 12)
        wait = not scan
        m = bytearray([ch, 1 if scan else 0, 0, 0, 0x03, 0x00, 0x00, 0x00])
        self.mcu(CMD_SWITCH_CHANNEL_OP, bytes(m), wait)
        time.sleep(0.006)
        m[6] = 0xE0
        self.mcu(CMD_SWITCH_CHANNEL_OP, bytes(m), wait)
        now = time.monotonic()
        if not scan or now - self.last_cal.get(ch, 0) > CAL_TTL_S:
            self.mcu(CMD_INIT_GAIN_OP, _words(ch | (1 << 31), self.mcu_gain), wait)
            self.set_bits(BBP_RXO13, 1 << 10)
            self.mcu(CMD_CALIBRATION_OP, _words(MCU_CAL_RXDCOC, ch), wait)
            self.mcu(CMD_CALIBRATION_OP, _words(MCU_CAL_RC, 0), wait)
            self.wr(BBP_AGC61, 0xFF64A4E2)
            self.wr(BBP_AGC7, 0x08081010)
            self.wr(BBP_AGC11, 0x00000404)
            self.wr(BBP_AGC2, 0x00007070)
            self.wr(TXOP_CTRL_CFG, 0x04101B3F)
            self.set_bits(BBP_TXO4, 1 << 25)
            self.set_bits(BBP_RXO13, 1 << 8)
            self.last_cal[ch] = now
        self.channel = ch
        if scan:
            return
        if five:
            self.mcu(CMD_CALIBRATION_OP, _words(MCU_CAL_LC, 0), True)
        for cal, arg in ((MCU_CAL_TX_LOFT, int(five)), (MCU_CAL_TXIQ, int(five)), (MCU_CAL_RXIQC_FI, int(five)),
                         (MCU_CAL_TEMP, 0), (MCU_CAL_TX_SHAPING, 0)):
            self.mcu(CMD_CALIBRATION_OP, _words(cal, arg), True)
        # edcca init: wires the antenna into the RX path (TX_PIN_CFG); without it the radio is deaf
        self.set_bits(TX_LINK_CFG, 1 << 12)
        self.clear_bits(TXOP_CTRL_CFG, 1 << 20)
        self.wr(BBP_AGC2, 0x00007070)
        self.set_bits(TXOP_HLDR_ET, 1 << 1)
        self.set_bits(MAC_SYS_CTRL, SYS_CTRL_ENABLE_TX)
        self.set_bits(AUTO_RSP_CFG, 1)
        self.wr(TX_PIN_CFG, self.rd(TX_PIN_CFG) | 0xF | (0xF << 8) | (1 << 16) | (1 << 18))
        self.rd(ED_CCA_TIMER)

    def rekick_rx(self) -> None:
        self.clear_bits(MAC_SYS_CTRL, SYS_CTRL_ENABLE_RX)
        v = self.cfg_rd(USB_U3DMA_CFG)
        self.cfg_wr(USB_U3DMA_CFG, v & ~DMA_RX_BULK_EN)
        self.cfg_wr(USB_U3DMA_CFG, v | DMA_RX_BULK_EN)
        self.set_bits(MAC_SYS_CTRL, SYS_CTRL_ENABLE_RX)

    def busy_idle(self) -> tuple[int, int]:
        return self.rd(CH_BUSY), self.rd(CH_IDLE)

    # -- RX --------------------------------------------------------------------------
    def rssi(self, raw: int, ch: int) -> int:
        return raw + (self.rssi_off_2g - self.lna_2g if ch <= 14 else self.rssi_off_5g - self.lna_5g)

    def parse(self, b: bytes, n: int) -> None:
        """[4B DMA hdr][32B RXWI][802.11 frame][FCE]; one MPDU per buffer."""
        if n < DMA_HDR + RXWI_LEN + 24:
            return
        dma_len = b[0] | (b[1] << 8)
        if dma_len < RXWI_LEN + 24 or DMA_HDR + dma_len > n:
            return
        rxinfo = _le32(b, DMA_HDR)
        if rxinfo & RXINFO_CRCERR:
            return
        ctl = _le32(b, DMA_HDR + 4)
        raw = b[DMA_HDR + 12]
        raw = raw - 256 if raw > 127 else raw
        mpdu = (ctl >> 16) & 0x3FFF
        d = DMA_HDR + RXWI_LEN
        end = min(n, d + mpdu + (2 if rxinfo & RXINFO_L2PAD else 0))
        if d + 24 > end:
            return
        frame = bytes(b[d:end])
        if rxinfo & RXINFO_L2PAD:
            # the radio inserts 2 bytes after the 802.11 header; drop them so the frame is standard
            hl = _hdr_len(frame)
            frame = frame[:hl] + frame[hl + 2:]
        self.frames += 1
        self.on_frame(frame, self.rssi(raw, self.channel), self.channel)

    def run(self, hop: tuple[int, ...] = HOP, dwell: float = DWELL_S) -> None:
        """RX with 24 transfers in flight (so frames keep landing while a hop
        retunes the radio), hopping every channel in ``hop``."""
        import usb1
        nbuf, size = 24, 4096
        transfers = []

        def done(t) -> None:
            st = t.getStatus()
            if st == usb1.TRANSFER_COMPLETED:
                n = t.getActualLength()
                if n:
                    self.bytes += n
                    self.last_rx = time.monotonic()
                    data = bytes(t.getBuffer()[:n])   # copy before the buffer is resubmitted
                    try:
                        t.submit()
                    except Exception:
                        pass
                    try:
                        self.parse(data, n)
                    except Exception:
                        log.debug("parse error", exc_info=True)
                    return
            if st in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT) and not self.stop_evt.is_set():
                try:
                    t.submit()
                except Exception:
                    pass

        for _ in range(nbuf):
            t = self.h.getTransfer()
            t.setBulk(self.ep_rx, size, callback=done, timeout=0)
            t.submit()
            transfers.append(t)
        self.last_rx = time.monotonic()
        hop_at = time.monotonic()
        idx = 0
        last_watch = time.monotonic()
        try:
            while not self.stop_evt.is_set():
                self.ctx.handleEventsTimeout(0.02)
                now = time.monotonic()
                if len(hop) > 1 and now - hop_at >= dwell:
                    idx = (idx + 1) % len(hop)
                    try:
                        self.tune(hop[idx], scan=True)
                        self.drain_resp()
                    except Exception:
                        log.debug("hop failed", exc_info=True)
                    hop_at = now
                if now - last_watch > 3:
                    last_watch = now
                    busy, _ = self.busy_idle()
                    if busy > 50000 and now - self.last_rx > 6:
                        log.info("MT7612U RX stalled with RF energy present; re-kicking RX")
                        self.rekick_rx()
                        self.last_rx = now
        finally:
            for t in transfers:
                try:
                    t.cancel()
                except Exception:
                    pass
            # Every transfer must come back before the handle closes; freeing one that
            # is still in flight corrupts memory.
            end = time.monotonic() + 3
            while time.monotonic() < end and any(t.isSubmitted() for t in transfers):
                try:
                    self.ctx.handleEventsTimeout(0.05)
                except Exception:
                    time.sleep(0.05)


def _hdr_len(f: bytes) -> int:
    """802.11 MAC header length (management/control 24, data 24/26/30/32, +4 with HT control)."""
    fc0, fc1 = f[0], f[1]
    ftype, sub = (fc0 >> 2) & 3, (fc0 >> 4) & 0xF
    if ftype != 2:
        return 24
    n = 30 if (fc1 & 3) == 3 else 24
    if sub & 0x8:          # QoS data
        n += 2
        if fc1 & 0x80:     # +HTC (order bit)
            n += 4
    return n
