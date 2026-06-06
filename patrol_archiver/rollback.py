"""回滚与报告导出"""
import hashlib
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


def export_report(batch: Batch, output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(batch.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return output_path


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
