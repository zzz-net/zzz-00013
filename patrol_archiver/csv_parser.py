"""CSV 巡检清单解析"""
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Tuple


@dataclass
class PatrolRecord:
    device_id: str
    point: str
    date: str
    photo_name: str
    line_no: int

    @property
    def key_tuple(self) -> Tuple[str, str, str, str]:
        return (self.device_id, self.point, self.date, self.photo_name)


def parse_patrol_csv(csv_path: Path, columns: Dict[str, str]) -> List[PatrolRecord]:
    records: List[PatrolRecord] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        required = ["device_id", "point", "date", "photo_name"]
        for key in required:
            col_name = columns.get(key, key)
            if col_name not in fieldnames:
                raise ValueError(f"CSV 缺少必要列: '{col_name}'。当前列: {fieldnames}")

        for line_no, row in enumerate(reader, start=2):
            records.append(PatrolRecord(
                device_id=(row.get(columns.get("device_id", "设备编号")) or "").strip(),
                point=(row.get(columns.get("point", "点位")) or "").strip(),
                date=(row.get(columns.get("date", "日期")) or "").strip(),
                photo_name=(row.get(columns.get("photo_name", "照片名")) or "").strip(),
                line_no=line_no
            ))
    return records
