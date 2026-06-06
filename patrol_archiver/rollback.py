"""回滚与报告导出"""
import csv
import hashlib
import io
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .storage import Batch, FileActionRecord, StateStore


class RollbackError(Exception):
    pass


def _file_signature(path: Path) -> Optional[str]:
    """计算文件签名（快速：大小+修改时间+部分MD5）"""
    try:
        if not path.exists() or not path.is_file():
            return None
        stat = path.stat()
        size = stat.st_size
        mtime = stat.st_mtime_ns
        h = hashlib.md5()
        h.update(f"{size}_{mtime}".encode())
        try:
            with open(path, "rb") as f:
                chunk = f.read(min(8192, size))
                h.update(chunk)
        except Exception:
            pass
        return h.hexdigest()
    except Exception:
        return None


def _precheck_rollback_conflicts(batch: Batch) -> List[str]:
    """回滚前检查：目标文件是否被其他批次/文件占用"""
    conflicts = []
    for a in batch.actions:
        if a.status != "success":
            continue
        tgt = Path(a.target)
        if not tgt.exists():
            continue
        src = Path(a.source)
        if a.action == "move":
            if src.exists():
                conflicts.append(
                    f"移动动作的源文件已重新出现，可能被其他批次占用: "
                    f"源={src}, 归档目标={tgt}"
                )
    return conflicts


def rollback_batch(store: StateStore, batch_id: str) -> Batch:
    batch = store.get_batch(batch_id)
    if batch is None:
        raise RollbackError(f"批次不存在: {batch_id}")
    if batch.dry_run:
        raise RollbackError(f"DRY-RUN 批次无法回滚: {batch_id}")
    if batch.status == "rolled_back":
        raise RollbackError(f"批次已回滚: {batch_id}")
    if batch.status not in ("completed", "failed"):
        raise RollbackError(f"批次状态不允许回滚，当前状态: {batch.status}")

    conflicts = _precheck_rollback_conflicts(batch)
    if conflicts:
        raise RollbackError(
            "回滚前检测到冲突，无法执行:\n  - " + "\n  - ".join(conflicts)
        )

    errors: List[str] = []
    for a in batch.actions:
        if a.status != "success":
            continue
        try:
            _rollback_one(a)
            a.status = "rolled_back"
        except Exception as e:
            errors.append(f"{a.target}: {e}")

    if errors:
        batch.error = "回滚部分失败:\n  - " + "\n  - ".join(errors)
        store.save_batch(batch)
        raise RollbackError(batch.error)

    batch.status = "rolled_back"
    store.save_batch(batch)
    return batch


def _rollback_one(rec: FileActionRecord) -> None:
    tgt = Path(rec.target)
    src = Path(rec.source)

    if not tgt.exists():
        return

    if rec.action == "copy":
        if tgt.exists():
            tgt.unlink()
    elif rec.action == "move":
        src.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            raise RollbackError(f"源路径已被其他文件占用，无法恢复: {src}")
        shutil.move(str(tgt), str(src))
    else:
        raise RollbackError(f"未知动作类型: {rec.action}")


SUMMARY_FIELDS = [
    "batch_id",
    "created_at",
    "status",
    "mode",
    "action",
    "source_dir",
    "archive_dir",
    "csv_path",
    "target_pattern",
    "actions_total",
    "actions_success",
    "actions_failed",
    "actions_pending",
    "actions_rolled_back",
    "missing_count",
    "extra_files_count",
    "duplicate_targets_count",
    "path_conflicts_count",
    "report_path",
    "error",
]

ACTION_FIELDS = [
    "section",
    "batch_id",
    "idx",
    "status",
    "action",
    "source",
    "target",
    "error",
]

ISSUE_FIELDS = [
    "section",
    "batch_id",
    "idx",
    "line_no",
    "device_id",
    "point",
    "date",
    "photo_name",
    "source",
    "target",
    "reason",
]


def _build_summary_row(batch: Batch) -> dict:
    actions_total = len(batch.actions)
    actions_success = sum(1 for a in batch.actions if a.status == "success")
    actions_failed = sum(1 for a in batch.actions if a.status == "failed")
    actions_pending = sum(1 for a in batch.actions if a.status == "pending")
    actions_rolled_back = sum(1 for a in batch.actions if a.status == "rolled_back")
    ps = batch.plan_summary
    return {
        "batch_id": batch.batch_id,
        "created_at": batch.created_at,
        "status": batch.status,
        "mode": "DRY-RUN" if batch.dry_run else "RUN",
        "action": batch.config_summary.get("action", ""),
        "source_dir": batch.config_summary.get("source_dir", ""),
        "archive_dir": batch.config_summary.get("archive_dir", ""),
        "csv_path": batch.config_summary.get("csv_path", ""),
        "target_pattern": batch.config_summary.get("target_pattern", ""),
        "actions_total": actions_total,
        "actions_success": actions_success,
        "actions_failed": actions_failed,
        "actions_pending": actions_pending,
        "actions_rolled_back": actions_rolled_back,
        "missing_count": len(ps.missing),
        "extra_files_count": len(ps.extra_files),
        "duplicate_targets_count": len(ps.duplicate_targets),
        "path_conflicts_count": len(ps.path_conflicts),
        "report_path": batch.report_path or "",
        "error": batch.error or "",
    }


def export_csv_report(batch: Batch, output_path: Path) -> Path:
    """导出 CSV 报告，包含批次摘要、文件动作、缺图、清单外文件、重复目标名、路径冲突。
    使用 section 字段区分不同数据段，字段名稳定，Excel/Numbers 可直接打开。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    summary_row = _build_summary_row(batch)
    writer.writerow(["section"] + SUMMARY_FIELDS)
    writer.writerow(["summary"] + [summary_row.get(k, "") for k in SUMMARY_FIELDS])
    writer.writerow([])

    writer.writerow(ACTION_FIELDS)
    for idx, a in enumerate(batch.actions, start=1):
        writer.writerow([
            "action",
            batch.batch_id,
            idx,
            a.status,
            a.action,
            a.source,
            a.target,
            a.error or "",
        ])

    ps = batch.plan_summary
    writer.writerow([])
    writer.writerow(ISSUE_FIELDS)

    for idx, m in enumerate(ps.missing, start=1):
        writer.writerow([
            "missing",
            batch.batch_id,
            idx,
            m.get("line_no", ""),
            m.get("device_id", ""),
            m.get("point", ""),
            m.get("date", ""),
            m.get("photo_name", ""),
            "",
            "",
            "",
        ])

    for idx, ef in enumerate(ps.extra_files, start=1):
        writer.writerow([
            "extra_file",
            batch.batch_id,
            idx,
            "",
            "",
            "",
            "",
            "",
            ef,
            "",
            "",
        ])

    idx = 0
    for tgt, items in ps.duplicate_targets.items():
        for item in items:
            idx += 1
            writer.writerow([
                "duplicate_target",
                batch.batch_id,
                idx,
                item.get("line_no", ""),
                item.get("device_id", ""),
                item.get("point", ""),
                item.get("date", ""),
                item.get("photo_name", ""),
                item.get("source", ""),
                tgt,
                "",
            ])

    for idx, pc in enumerate(ps.path_conflicts, start=1):
        writer.writerow([
            "path_conflict",
            batch.batch_id,
            idx,
            "",
            "",
            "",
            "",
            "",
            "",
            pc.get("target", ""),
            pc.get("reason", ""),
        ])

    output_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return output_path


def detect_format(output_path: Path) -> str:
    suffix = Path(output_path).suffix.lower()
    if suffix == ".csv":
        return "csv"
    return "json"


def export_report(batch: Batch, output_path: Path, fmt: Optional[str] = None) -> Path:
    """导出批次报告。fmt 为 None 时按扩展名自动识别 (json/csv)。"""
    fmt = (fmt or detect_format(output_path)).lower()
    if fmt == "csv":
        return export_csv_report(batch, output_path)
    if fmt == "json":
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(batch.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return output_path
    raise ValueError(f"不支持的导出格式: {fmt}，请使用 json 或 csv")


def format_batch_summary(batch: Batch) -> str:
    lines = []
    lines.append(f"批次 ID: {batch.batch_id}")
    lines.append(f"创建时间: {batch.created_at}")
    lines.append(f"状态: {batch.status}")
    lines.append(f"模式: {'DRY-RUN' if batch.dry_run else 'RUN'}")
    if batch.error:
        lines.append(f"错误: {batch.error}")
    lines.append("")
    lines.append("配置摘要:")
    for k, v in batch.config_summary.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"文件动作统计: 共 {len(batch.actions)} 个")
    by_status: dict = {}
    for a in batch.actions:
        by_status[a.status] = by_status.get(a.status, 0) + 1
    for s, c in by_status.items():
        lines.append(f"  {s}: {c}")
    if batch.report_path:
        lines.append(f"报告路径: {batch.report_path}")
    return "\n".join(lines)


def format_batch_list(batches: List[Batch]) -> str:
    if not batches:
        return "(无批次记录)"
    lines = []
    for b in batches:
        tag = "[DRY]" if b.dry_run else "[RUN]"
        lines.append(
            f"{b.batch_id}  {tag}  {b.status:12s}  {b.created_at}  "
            f"actions={len(b.actions)}"
        )
    return "\n".join(lines)
