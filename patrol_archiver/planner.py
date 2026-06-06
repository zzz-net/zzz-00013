"""归档计划生成与冲突检测"""
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional

from .config import ArchiverConfig
from .csv_parser import PatrolRecord


@dataclass
class FileAction:
    source: Path
    target: Path
    record: Optional[PatrolRecord] = None
    action: str = "copy"  # "copy" 或 "move"


@dataclass
class ArchivePlan:
    to_process: List[FileAction] = field(default_factory=list)
    missing: List[PatrolRecord] = field(default_factory=list)
    extra_files: List[Path] = field(default_factory=list)
    duplicate_targets: Dict[str, List[Tuple[PatrolRecord, Path]]] = field(default_factory=dict)
    path_conflicts: List[Tuple[Path, str]] = field(default_factory=list)

    @property
    def has_fatal_errors(self) -> bool:
        return len(self.duplicate_targets) > 0 or len(self.path_conflicts) > 0

    def fatal_error_messages(self) -> List[str]:
        msgs = []
        for target, items in self.duplicate_targets.items():
            lines = ", ".join([f"第{r.line_no}行({p.name})" for r, p in items])
            msgs.append(f"归档目标名冲突: {target} <- {lines}")
        for target, reason in self.path_conflicts:
            msgs.append(f"路径冲突: {target} - {reason}")
        return msgs


def _scan_source(source_dir: Path, extensions: List[str]) -> Dict[str, Path]:
    """扫描源目录，返回 {文件名小写: 完整路径}"""
    result: Dict[str, Path] = {}
    ext_lower = {e.lower() for e in extensions}
    for root, _, files in os.walk(source_dir):
        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() in ext_lower:
                key = fname.lower()
                if key not in result:
                    result[key] = fpath
    return result


def _build_target_path(cfg: ArchiverConfig, record: PatrolRecord, filename: str) -> Path:
    relative = cfg.target_pattern.format(
        device_id=record.device_id,
        point=record.point,
        date=record.date,
        filename=filename
    )
    return (cfg.archive_dir / relative).resolve()


def generate_plan(cfg: ArchiverConfig, records: List[PatrolRecord]) -> ArchivePlan:
    plan = ArchivePlan()

    source_files = _scan_source(cfg.source_dir, cfg.photo_extensions)
    source_names: Set[str] = set(source_files.keys())
    used_sources: Set[str] = set()

    target_to_records: Dict[str, List[Tuple[PatrolRecord, Path]]] = {}

    for rec in records:
        photo_key = rec.photo_name.lower()
        if not photo_key:
            plan.missing.append(rec)
            continue

        if photo_key not in source_files:
            plan.missing.append(rec)
            continue

        src_path = source_files[photo_key]
        used_sources.add(photo_key)
        tgt_path = _build_target_path(cfg, rec, rec.photo_name)
        tgt_str = str(tgt_path)

        if tgt_str not in target_to_records:
            target_to_records[tgt_str] = []
        target_to_records[tgt_str].append((rec, src_path))

    for tgt_str, items in target_to_records.items():
        if len(items) > 1:
            plan.duplicate_targets[tgt_str] = items
        else:
            rec, src_path = items[0]
            tgt_path = Path(tgt_str)
            if tgt_path.exists():
                plan.path_conflicts.append(
                    (tgt_path, "目标文件已存在")
                )
            else:
                plan.to_process.append(FileAction(
                    source=src_path,
                    target=tgt_path,
                    record=rec,
                    action=cfg.action
                ))

    for name_lower, fpath in source_files.items():
        if name_lower not in used_sources:
            plan.extra_files.append(fpath)

    return plan
