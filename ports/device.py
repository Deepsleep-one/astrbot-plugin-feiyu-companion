"""设备只读端口（M9-D）：一次一帧「这台机器现在什么样」。

设计约束（与识屏/摄像头同一套思路，但**更弱**）：

- 只读：只报读数，没有任何「设成静音 / 切显示器 / 关进程」的动作面。
- 一次一帧：`ttl_seconds` 到期就作废，没有「持续监控」。
- 白名单：只放行这里列出的字段；MAC、IP、主机名、序列号、Windows 用户名一律不进。
- 不假装：适配器拿不到就说拿不到，端口这边不做占位数据。

这个文件里**不许**出现整机读数库（读数在适配器/采集端那边），也不许出现任何控制类 API。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

DEVICE_PORT_NAME = "DevicePortV1"
DEVICE_VERSION = 1

DEFAULT_TTL_SECONDS = 60
MAX_TTL_SECONDS = 300

# 每类最多带几项（多了也看不完，还占提示词）
LIMITS = {"displays": 4, "disks": 4, "net": 6, "audio": 8}

_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HEX_RE = re.compile(r"\b[0-9A-Fa-f]{8,}\b")
_USB_RE = re.compile(r"USB\\[^\s]+", re.IGNORECASE)


class DeviceError(ValueError):
    """请求或快照本身不合契约（字段错、场景错）。"""


class DeviceUnavailable(RuntimeError):
    """拿不到读数（没配采集端、隧道没起、本机缺读数依赖…），必须如实说。"""


def redact_device_name(text: str, *, limit: int = 80) -> str:
    """把设备名里的 MAC/IP/序列号样子的串抹掉，再截断。

    设备名经常混进 `ZeroTier One [9c7b3a93cc0c42b7]`、`USB\\VID_04F2&PID_B78A`
    这类唯一标识——它们对「现在机器怎么样」没有用，却能把机器认出来。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    cleaned = _USB_RE.sub("〔已隐去〕", cleaned)
    cleaned = _MAC_RE.sub("〔已隐去〕", cleaned)
    cleaned = _IP_RE.sub("〔已隐去〕", cleaned)
    cleaned = _HEX_RE.sub("〔已隐去〕", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: max(1, limit - 1)].rstrip() + "…"
    return cleaned


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return number


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    return max(low, min(high, _num(value, default)))


def _text(value: Any, *, limit: int = 80) -> str:
    return redact_device_name(str(value or ""), limit=limit)


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "up", "开")


@dataclass(frozen=True)
class DisplayInfo:
    label: str = ""
    width: int = 0
    height: int = 0
    refresh_hz: int = 0
    primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "width": self.width,
            "height": self.height,
            "refresh_hz": self.refresh_hz,
            "primary": self.primary,
        }


@dataclass(frozen=True)
class DiskInfo:
    mount: str = ""
    total_gb: float = 0.0
    free_gb: float = 0.0
    percent: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mount": self.mount,
            "total_gb": self.total_gb,
            "free_gb": self.free_gb,
            "percent": self.percent,
        }


@dataclass(frozen=True)
class NetInfo:
    name: str = ""
    up: bool = False
    speed_mbps: int = 0
    sent_mb: float = 0.0
    recv_mb: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "up": self.up,
            "speed_mbps": self.speed_mbps,
            "sent_mb": self.sent_mb,
            "recv_mb": self.recv_mb,
        }


@dataclass(frozen=True)
class BatteryInfo:
    present: bool = False
    percent: int = 0
    plugged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"present": self.present, "percent": self.percent, "plugged": self.plugged}


@dataclass(frozen=True)
class DeviceRequest:
    scene: str = "private"
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    device: str = ""

    def __post_init__(self) -> None:
        if self.scene != "private":
            raise DeviceError("设备读数只在私聊里给：群里不报你机器上的事")
        ttl = DEFAULT_TTL_SECONDS if self.ttl_seconds is None else int(self.ttl_seconds)
        if not 1 <= ttl <= MAX_TTL_SECONDS:
            raise DeviceError(f"有效期要在 1–{MAX_TTL_SECONDS} 秒之间")
        object.__setattr__(self, "ttl_seconds", ttl)

    def to_dict(self) -> dict[str, Any]:
        return {"scene": self.scene, "ttl_seconds": self.ttl_seconds, "device": self.device}


@dataclass(frozen=True)
class DeviceSnapshot:
    """一次读数。所有数值都夹在合理区间里，名字都脱敏过。"""

    captured_at: str = ""
    expires_at: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    device: str = ""
    cpu_percent: float = 0.0
    cpu_cores_physical: int = 0
    cpu_cores_logical: int = 0
    memory_total_gb: float = 0.0
    memory_available_gb: float = 0.0
    memory_percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_percent: float = 0.0
    boot_hours: float = 0.0
    battery: BatteryInfo = field(default_factory=BatteryInfo)
    disks: tuple[DiskInfo, ...] = ()
    net: tuple[NetInfo, ...] = ()
    displays: tuple[DisplayInfo, ...] = ()
    audio: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": DEVICE_PORT_NAME,
            "version": DEVICE_VERSION,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "ttl_seconds": self.ttl_seconds,
            "device": self.device,
            "cpu_percent": self.cpu_percent,
            "cpu_cores_physical": self.cpu_cores_physical,
            "cpu_cores_logical": self.cpu_cores_logical,
            "memory_total_gb": self.memory_total_gb,
            "memory_available_gb": self.memory_available_gb,
            "memory_percent": self.memory_percent,
            "swap_total_gb": self.swap_total_gb,
            "swap_percent": self.swap_percent,
            "boot_hours": self.boot_hours,
            "battery": self.battery.to_dict(),
            "disks": [item.to_dict() for item in self.disks],
            "net": [item.to_dict() for item in self.net],
            "displays": [item.to_dict() for item in self.displays],
            "audio": list(self.audio),
            "notes": list(self.notes),
        }

    def is_expired(self, now: datetime) -> bool:
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        moment = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return moment >= expires


class DevicePortV1(Protocol):
    """适配器要实现的东西：给一个请求，回一次读数或如实说不。"""

    def status(self) -> tuple[str, str]: ...

    def snapshot(self, request: DeviceRequest | None = None) -> DeviceSnapshot: ...


def normalize_snapshot(raw: Any, request: DeviceRequest | None = None) -> DeviceSnapshot:
    """把本机 agent 回的原始 dict 收成白名单形状。

    任何不认识的键都丢掉；数值夹到合理区间；设备名脱敏；缺时间就给空串
    （过期与否交给调用方按 ttl 判断，不在这里编一个假时间）。
    """
    request = request or DeviceRequest()
    if not isinstance(raw, dict):
        raise DeviceError("采集端返回的不是一个对象")
    version = raw.get("version", DEVICE_VERSION)
    if int(_num(version, 0)) not in (0, DEVICE_VERSION):
        raise DeviceError(f"采集端版本 {version} 和这边的 {DEVICE_VERSION} 对不上")

    battery_raw = raw.get("battery") or {}
    battery = BatteryInfo(
        present=_flag(battery_raw.get("present")) if isinstance(battery_raw, dict) else False,
        percent=int(_clamp(battery_raw.get("percent") if isinstance(battery_raw, dict) else 0, 0, 100)),
        plugged=_flag(battery_raw.get("plugged")) if isinstance(battery_raw, dict) else False,
    )

    disks: list[DiskInfo] = []
    for item in (raw.get("disks") or [])[: LIMITS["disks"]]:
        if not isinstance(item, dict):
            continue
        total = _clamp(item.get("total_gb"), 0, 100000)
        if total <= 0:
            continue
        disks.append(
            DiskInfo(
                mount=_text(item.get("mount"), limit=16),
                total_gb=round(total, 1),
                free_gb=round(min(_clamp(item.get("free_gb"), 0, total), total), 1),
                percent=round(_clamp(item.get("percent"), 0, 100), 1),
            )
        )

    net: list[NetInfo] = []
    for item in (raw.get("net") or [])[: LIMITS["net"]]:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), limit=40)
        if not name:
            continue
        net.append(
            NetInfo(
                name=name,
                up=_flag(item.get("up")),
                speed_mbps=int(_clamp(item.get("speed_mbps"), 0, 100000)),
                sent_mb=round(_clamp(item.get("sent_mb"), 0, 10**9), 1),
                recv_mb=round(_clamp(item.get("recv_mb"), 0, 10**9), 1),
            )
        )
    # 通着的网卡排前面，其余按名字稳定排序
    net.sort(key=lambda item: (not item.up, item.name))

    displays: list[DisplayInfo] = []
    for item in (raw.get("displays") or [])[: LIMITS["displays"]]:
        if not isinstance(item, dict):
            continue
        displays.append(
            DisplayInfo(
                label=_text(item.get("label"), limit=48),
                width=int(_clamp(item.get("width"), 0, 16384)),
                height=int(_clamp(item.get("height"), 0, 16384)),
                refresh_hz=int(_clamp(item.get("refresh_hz"), 0, 1000)),
                primary=_flag(item.get("primary")),
            )
        )
    displays.sort(key=lambda item: (not item.primary, item.label))

    audio: list[str] = []
    for item in (raw.get("audio") or [])[: LIMITS["audio"]]:
        name = _text(item, limit=48)
        if name and name not in audio:
            audio.append(name)

    notes: list[str] = []
    for item in (raw.get("notes") or [])[:6]:
        note = redact_device_name(str(item or ""), limit=120)
        if note:
            notes.append(note)

    return DeviceSnapshot(
        captured_at=str(raw.get("captured_at") or "")[:40],
        expires_at=str(raw.get("expires_at") or "")[:40],
        ttl_seconds=int(_clamp(raw.get("ttl_seconds"), 1, MAX_TTL_SECONDS, request.ttl_seconds)),
        device=_text(raw.get("device") or request.device, limit=40),
        cpu_percent=round(_clamp(raw.get("cpu_percent"), 0, 100), 1),
        cpu_cores_physical=int(_clamp(raw.get("cpu_cores_physical"), 0, 1024)),
        cpu_cores_logical=int(_clamp(raw.get("cpu_cores_logical"), 0, 4096)),
        memory_total_gb=round(_clamp(raw.get("memory_total_gb"), 0, 100000), 1),
        memory_available_gb=round(
            min(
                _clamp(raw.get("memory_available_gb"), 0, 100000),
                _clamp(raw.get("memory_total_gb"), 0, 100000),
            ),
            1,
        ),
        memory_percent=round(_clamp(raw.get("memory_percent"), 0, 100), 1),
        swap_total_gb=round(_clamp(raw.get("swap_total_gb"), 0, 100000), 1),
        swap_percent=round(_clamp(raw.get("swap_percent"), 0, 100), 1),
        boot_hours=round(_clamp(raw.get("boot_hours"), 0, 24 * 365), 1),
        battery=battery,
        disks=tuple(disks),
        net=tuple(net),
        displays=tuple(displays),
        audio=tuple(audio),
        notes=tuple(notes),
    )


def describe(snapshot: DeviceSnapshot, *, limit: int = 8) -> str:
    """把读数说成人话。**只描述现状**，不猜测、不建议、不编。"""
    lines: list[str] = []
    if snapshot.cpu_percent or snapshot.memory_total_gb:
        cores = ""
        if snapshot.cpu_cores_physical:
            cores = f"（{snapshot.cpu_cores_physical} 核"
            if snapshot.cpu_cores_logical and snapshot.cpu_cores_logical != snapshot.cpu_cores_physical:
                cores += f" / {snapshot.cpu_cores_logical} 线程"
            cores += "）"
        lines.append(f"CPU {snapshot.cpu_percent:.0f}%{cores}")
    if snapshot.memory_total_gb > 0:
        lines.append(
            f"内存 {snapshot.memory_available_gb:.1f}/{snapshot.memory_total_gb:.1f} GB 可用"
            f"（已用 {snapshot.memory_percent:.0f}%）"
        )
    if snapshot.swap_total_gb > 0:
        lines.append(f"交换区 {snapshot.swap_total_gb:.1f} GB，已用 {snapshot.swap_percent:.0f}%")
    if snapshot.disks:
        parts = []
        for disk in snapshot.disks:
            parts.append(f"{disk.mount} 剩 {disk.free_gb:.1f}/{disk.total_gb:.1f} GB")
        lines.append("磁盘：" + "，".join(parts))
    if snapshot.battery.present:
        state = "插着电" if snapshot.battery.plugged else "用电池"
        lines.append(f"电量 {snapshot.battery.percent}%（{state}）")
    up = [item for item in snapshot.net if item.up]
    if up:
        parts = []
        for item in up:
            speed = f" {item.speed_mbps} Mbps" if item.speed_mbps else ""
            parts.append(f"{item.name} 通着{speed}（发 {item.sent_mb:.1f} / 收 {item.recv_mb:.1f} MB）")
        lines.append("网络：" + "；".join(parts))
    elif snapshot.net:
        lines.append(f"网络：{len(snapshot.net)} 块网卡都没连")
    if snapshot.displays:
        parts = []
        for display in snapshot.displays:
            mark = "，主屏" if display.primary else ""
            hz = f" {display.refresh_hz}Hz" if display.refresh_hz else ""
            parts.append(f"{display.width}×{display.height}{hz}{mark}")
        lines.append(f"屏幕 {len(snapshot.displays)} 块：" + "；".join(parts))
    if snapshot.audio:
        lines.append("声音设备：" + "、".join(snapshot.audio))
    if snapshot.boot_hours > 0:
        lines.append(f"已经开了 {snapshot.boot_hours:.0f} 小时")
    if snapshot.notes:
        lines.extend(snapshot.notes[:3])
    return "\n".join(lines[:limit])


def failed(reason: str, *, device: str = "") -> DeviceSnapshot:
    """失败也用同一个形状回，`notes[0]` 是原话；不伪造任何读数。"""
    return DeviceSnapshot(device=device, notes=(str(reason or "")[:200],))


def now_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def expiry_iso(seconds: int, *, moment: datetime | None = None) -> str:
    base = moment or datetime.now(timezone.utc)
    return (base + timedelta(seconds=max(1, int(seconds)))).isoformat(timespec="seconds")


__all__ = [
    "DEVICE_PORT_NAME",
    "DEVICE_VERSION",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "LIMITS",
    "DeviceError",
    "DeviceUnavailable",
    "DeviceRequest",
    "DeviceSnapshot",
    "DisplayInfo",
    "DiskInfo",
    "NetInfo",
    "BatteryInfo",
    "DevicePortV1",
    "normalize_snapshot",
    "describe",
    "failed",
    "redact_device_name",
    "now_iso",
    "expiry_iso",
]
