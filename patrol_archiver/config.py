"""配置加载与校验"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class ArchiverConfig:
    source_dir: Path
    archive_dir: Path
    csv_path: Path
    state_dir: Path
    photo_extensions: List[str] = field(default_factory=lambda: [".jpg", ".jpeg", ".png", ".bmp", ".gif"])
    action: str = "copy"  # "copy" 或 "move"
    target_pattern: str = "{device_id}/{point}/{date}/{filename}"
    csv_columns: dict = field(default_factory=lambda: {
        "device_id": "设备编号",
        "point": "点位",
        "date": "日期",
        "photo_name": "照片名"
    })

    def validate(self) -> List[str]:
        errors = []
        if not self.source_dir.exists():
            errors.append(f"源目录不存在: {self.source_dir}")
        if not self.source_dir.is_dir():
            errors.append(f"源路径不是目录: {self.source_dir}")
        if not self.csv_path.exists():
            errors.append(f"CSV 文件不存在: {self.csv_path}")
        if self.action not in ("copy", "move"):
            errors.append(f"action 必须是 'copy' 或 'move'，当前为: {self.action}")
        return errors


def load_config(config_path: str) -> ArchiverConfig:
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(config_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    base_dir = config_file.parent

    def resolve_path(p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = base_dir / path
        return path.resolve()

    cfg = ArchiverConfig(
        source_dir=resolve_path(raw.get("source_dir", "./source_photos")),
        archive_dir=resolve_path(raw.get("archive_dir", "./archive")),
        csv_path=resolve_path(raw.get("csv_path", "./patrol.csv")),
        state_dir=resolve_path(raw.get("state_dir", "./.patrol_state")),
        photo_extensions=raw.get("photo_extensions", [".jpg", ".jpeg", ".png", ".bmp", ".gif"]),
        action=raw.get("action", "copy"),
        target_pattern=raw.get("target_pattern", "{device_id}/{point}/{date}/{filename}"),
        csv_columns=raw.get("csv_columns", {
            "device_id": "设备编号",
            "point": "点位",
            "date": "日期",
            "photo_name": "照片名"
        })
    )

    return cfg
