"""Strict new-import folder identities; legacy project readers may ignore failures.

Parsing never moves files, infers a cow from a device, or deduplicates records.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

RULE = "完整设备编号-牛耳标号-现场记号（设备 12 位十六进制、耳标纯数字、记号 ASCII 字母数字）"
DEVICE = re.compile(r"[0-9A-Fa-f]{12}")
FOLDER = re.compile(r"([0-9A-Fa-f]{12})-([0-9]+)-([A-Za-z0-9]+)")
DATE_FOLDER = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:至[0-9]{4}-[0-9]{2}-[0-9]{2})?(?:_[0-9]+)?")


@dataclass(frozen=True)
class DeviceIdentity:
    device_id: str
    cow_id: str
    field_mark: str

    @property
    def folder_name(self):
        return f"{self.device_id}-{self.cow_id}-{self.field_mark}"


def parse_device_folder(name):
    match = FOLDER.fullmatch(str(name))
    if not match:
        raise ValueError("设备目录需规范为：" + RULE)
    return DeviceIdentity(match[1].upper(), match[2], match[3])


def source_device_folder(source_path):
    """Return the nearest device-like ancestor; dates are never device codes."""
    parents = list(Path(source_path).parents)
    for folder in parents:
        if folder.parent.name == "九轴":
            return folder
    for folder in parents:
        if not DATE_FOLDER.fullmatch(folder.name) and re.match(r"^[0-9A-Fa-f]{4,12}(?:-|$)", folder.name):
            return folder
    return None


def resolve_device_identity(source_path, json_device):
    folder = source_device_folder(source_path)
    name = folder.name if folder else ""
    device = str(json_device or "")
    result = {"status": "blocked", "device_id": device.upper() if DEVICE.fullmatch(device) else "",
              "cow_id": "", "field_mark": "", "source_folder": name, "suggested_folder": "",
              "identity_provenance": "folder_and_json_device", "message": ""}
    if not DEVICE.fullmatch(device):
        result["message"] = "JSON 内设备编号不是完整 12 位十六进制编号；请核对原始记录，保留原件。"
        return result
    if folder is None:
        result["message"] = "未找到三段设备目录；请人工确认耳标和现场记号，按" + RULE + "规范来源目录。原件保留。"
        return result
    try:
        identity = parse_device_folder(name)
    except ValueError:
        identity = None
    if identity is not None:
        if identity.device_id != device.upper():
            result["message"] = f"目录设备 {identity.device_id} 与 JSON 设备 {device.upper()} 不一致；请核对原件，不自动更正。"
        else:
            result.update(status="ready", device_id=identity.device_id, cow_id=identity.cow_id,
                          field_mark=identity.field_mark, folder_name=identity.folder_name,
                          message="三段目录与 JSON 设备一致；耳标及现场记号按本份记录保留。")
        return result

    parts = name.split("-")
    suggestion, reason = "", ""
    prefix = parts[0]
    full = prefix.upper() if DEVICE.fullmatch(prefix) else ""
    if full and full != device.upper():
        result["message"] = f"目录设备 {full} 与 JSON 设备 {device.upper()} 不一致，且名称不规范；不推测替换。"
        return result
    if not full and re.fullmatch(r"[0-9A-Fa-f]{4,11}", prefix) and device.upper().endswith(prefix.upper()):
        full = device.upper()
        reason = "短码与本份 JSON 完整设备编号后缀相符；需核对同目录所有记录后人工补齐"
    if full and len(parts) == 3:
        if re.fullmatch(r"[0-9]+", parts[1]) and re.fullmatch(r"[A-Za-z0-9]+", parts[2]):
            suggestion = f"{full}-{parts[1]}-{parts[2]}"
        elif re.fullmatch(r"[A-Za-z0-9]+", parts[1]) and re.search(r"[A-Za-z]", parts[1]) and re.fullmatch(r"[0-9]+", parts[2]):
            suggestion = f"{full}-{parts[2]}-{parts[1]}"
            reason = "中段含字母、末段为数字，可能把现场记号与耳标顺序写反；需人工确认"
    elif full and len(parts) == 2:
        joined = re.fullmatch(r"([0-9]+)([A-Za-z][A-Za-z0-9]*)", parts[1])
        if joined:
            suggestion = f"{full}-{joined[1]}-{joined[2]}"
            reason = "耳标数字后紧接字母记号，可能缺少分隔符；需人工确认"
    result["suggested_folder"] = suggestion
    result["message"] = ("目录命名待规范；" + reason + "。建议：" + suggestion + "。不自动改名。") if suggestion else (
        "目录命名待规范，无法唯一推断耳标或现场记号；请按" + RULE + "人工核对，原件保留。")
    return result
