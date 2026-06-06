"""归档交接包模块

用于把已归档的批次整理成可交给外部人员核对的离线包，
包含 manifest.json、manifest.csv、批次报告副本和 README.txt。
"""
import csv
import hashlib
import io
import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .storage import (
    Batch,
    FileActionRecord,
    HandoverError,
    HandoverFileEntry,
    HandoverRecord,
    HandoverStore,
    StateStore,
)


def resolve_batch_for_handover(
    state_store: StateStore,
    batch_id: Optional[str] = None,
) -> Batch:
    """共享流程：为 handover-create 解析目标批次。

    规则:
      - 未指定 batch_id 时，使用最近一次 **真实 run**（跳过 dry-run）
      - 指定 batch_id 时，若指向 dry-run → 直接报错
      - 批次不存在 / 状态不允许 → 报错

    返回解析到的 Batch 对象，失败抛 HandoverError。
    """
    if batch_id:
        batch = state_store.get_batch(batch_id)
        if batch is None:
            raise HandoverError(f"批次不存在: {batch_id}")
        if batch.dry_run:
            raise HandoverError(f"DRY-RUN 批次无法生成交接包: {batch_id}")
    else:
        batch = state_store.get_last_real_run_batch()
        if batch is None:
            raise HandoverError("未找到任何已执行的真实 run 批次")

    if batch.status not in ("completed", "rolled_back"):
        raise HandoverError(
            f"批次状态不允许生成交接包，当前状态: {batch.status}"
        )
    return batch


def resolve_handover_output_dir(base_dir: Path) -> Tuple[Path, bool]:
    """共享流程：解析交接包输出目录。

    若目录不存在或为空 → 直接使用；若已存在且非空 → 自动追加 _1/_2/... 后缀。

    返回 (实际使用的目录路径, 是否发生了自动重命名)。
    """
    base_dir = Path(base_dir).resolve()
    if not base_dir.exists():
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir, False
    if not any(base_dir.iterdir()):
        return base_dir, False

    suffix = 1
    while True:
        candidate = base_dir.parent / f"{base_dir.name}_{suffix}"
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate, True
        suffix += 1


def resolve_handover_record(
    handover_store: HandoverStore,
    handover_id: Optional[str] = None,
) -> HandoverRecord:
    """共享流程：为 handover-show / handover-verify 解析交接包记录。

    未指定 handover_id 时使用最近一次；不存在则抛 HandoverError。
    """
    if handover_id:
        record = handover_store.get_handover(handover_id)
        if record is None:
            raise HandoverError(f"交接包不存在: {handover_id}")
    else:
        record = handover_store.get_last_handover()
        if record is None:
            raise HandoverError("未找到任何交接包记录")
    return record


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 SHA-256 哈希"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _collect_success_files(batch: Batch) -> List[FileActionRecord]:
    """从批次中筛选出成功归档的文件动作"""
    return [a for a in batch.actions if a.status == "success"]


def _copy_archive_files(
    batch: Batch,
    output_dir: Path,
    archive_dir: Path,
) -> Tuple[List[HandoverFileEntry], int]:
    """把批次中所有成功归档的文件复制到交接包的 files/ 子目录，
    并计算 SHA-256。返回（文件条目列表，总字节数）。"""
    files_dir = output_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    entries: List[HandoverFileEntry] = []
    total_bytes = 0
    idx = 0

    for action in _collect_success_files(batch):
        src = Path(action.target)
        if not src.exists() or not src.is_file():
            continue

        idx += 1
        try:
            rel_archive = src.relative_to(archive_dir.resolve())
        except ValueError:
            rel_archive = Path(src.name)

        rel_path = Path("files") / rel_archive
        dest = output_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))

        file_size = dest.stat().st_size
        total_bytes += file_size
        sha256 = _sha256_file(dest)

        entries.append(HandoverFileEntry(
            idx=idx,
            source=str(action.source),
            archive_path=str(src.resolve()),
            relative_path=str(rel_path).replace("\\", "/"),
            file_size=file_size,
            sha256=sha256,
            action=action.action,
            source_signature=action.source_signature or "",
        ))

    return entries, total_bytes


def _write_manifest_json(
    output_dir: Path,
    handover_id: str,
    batch: Batch,
    entries: List[HandoverFileEntry],
    total_bytes: int,
) -> Path:
    """生成 manifest.json"""
    manifest = {
        "handover_id": handover_id,
        "created_at": datetime.now().isoformat(),
        "batch": {
            "batch_id": batch.batch_id,
            "created_at": batch.created_at,
            "status": batch.status,
            "config_summary": batch.config_summary,
        },
        "summary": {
            "file_count": len(entries),
            "total_bytes": total_bytes,
        },
        "files": [e.to_dict() for e in entries],
    }
    path = output_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return path


def _write_manifest_csv(
    output_dir: Path,
    entries: List[HandoverFileEntry],
) -> Path:
    """生成 manifest.csv（带 UTF-8 BOM，Excel 可直接打开）"""
    headers = [
        "idx", "source", "archive_path", "relative_path",
        "file_size", "sha256", "action", "source_signature",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for e in entries:
        writer.writerow([
            e.idx, e.source, e.archive_path, e.relative_path,
            e.file_size, e.sha256, e.action, e.source_signature,
        ])
    path = output_dir / "manifest.csv"
    path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return path


def _copy_batch_report(batch: Batch, output_dir: Path) -> Optional[Path]:
    """复制批次报告到交接包"""
    if not batch.report_path:
        return None
    src = Path(batch.report_path)
    if not src.exists():
        return None
    dest = output_dir / f"batch_report_{batch.batch_id}{src.suffix}"
    shutil.copy2(str(src), str(dest))
    return dest


def _write_readme(
    output_dir: Path,
    handover_id: str,
    batch: Batch,
    entries: List[HandoverFileEntry],
    total_bytes: int,
) -> Path:
    """生成 README.txt"""
    lines = []
    lines.append("=" * 60)
    lines.append("巡检照片归档交接包")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"交接包 ID: {handover_id}")
    lines.append(f"生成时间:   {datetime.now().isoformat()}")
    lines.append(f"对应批次:   {batch.batch_id}")
    lines.append(f"批次时间:   {batch.created_at}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("目录内容")
    lines.append("-" * 60)
    lines.append("  manifest.json   - JSON 格式的文件清单（含哈希）")
    lines.append("  manifest.csv    - CSV 格式的文件清单（Excel 可打开）")
    lines.append(f"  batch_report_{batch.batch_id}.txt - 原始批次报告副本")
    lines.append("  files/          - 归档照片副本（按原始层级组织）")
    lines.append("  README.txt      - 本说明文件")
    lines.append("")
    lines.append("-" * 60)
    lines.append("统计信息")
    lines.append("-" * 60)
    lines.append(f"  文件总数: {len(entries)}")
    lines.append(f"  总字节数: {total_bytes} ({total_bytes / 1024 / 1024:.2f} MB)")
    lines.append("")
    lines.append("-" * 60)
    lines.append("外部核对说明")
    lines.append("-" * 60)
    lines.append("  1. 核对文件数量是否与 manifest.json 中的 file_count 一致")
    lines.append("  2. 核对每个文件大小 (file_size) 是否匹配")
    lines.append("  3. 用 SHA-256 校验每个文件完整性:")
    lines.append("     Windows:  certutil -hashfile <文件> SHA256")
    lines.append("     Linux/Mac: sha256sum <文件>")
    lines.append("  4. 对比 manifest 中的 source 与实际归档来源")
    lines.append("")
    lines.append("-" * 60)
    lines.append("配置摘要")
    lines.append("-" * 60)
    for k, v in batch.config_summary.items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    path = output_dir / "README.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def create_handover(
    state_store: StateStore,
    handover_store: HandoverStore,
    batch: Batch,
    output_dir: Path,
    archive_dir: Path,
) -> Tuple[HandoverRecord, Path, bool]:
    """创建交接包。

    参数:
        state_store: 批次状态存储
        handover_store: 交接包记录存储
        batch: 要打包的批次
        output_dir: 输出目录（若已存在会自动追加序号）
        archive_dir: 归档根目录（用于计算相对路径）

    返回:
        (HandoverRecord, 实际使用的输出目录路径, 是否因冲突自动重命名)
    """
    success_files = _collect_success_files(batch)
    if not success_files:
        raise HandoverError(f"批次中没有成功归档的文件: {batch.batch_id}")

    actual_dir, renamed = resolve_handover_output_dir(output_dir)

    handover_id = f"handover_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    entries, total_bytes = _copy_archive_files(batch, actual_dir, archive_dir)

    if not entries:
        shutil.rmtree(actual_dir, ignore_errors=True)
        raise HandoverError("没有可复制的归档文件（可能已被移动或删除）")

    _write_manifest_json(actual_dir, handover_id, batch, entries, total_bytes)
    _write_manifest_csv(actual_dir, entries)
    _copy_batch_report(batch, actual_dir)
    _write_readme(actual_dir, handover_id, batch, entries, total_bytes)

    record = HandoverRecord(
        handover_id=handover_id,
        created_at=datetime.now().isoformat(),
        batch_id=batch.batch_id,
        output_dir=str(actual_dir),
        config_summary=dict(batch.config_summary),
        files=entries,
    )
    handover_store.save_handover(record)

    return record, actual_dir, renamed


def format_handover_list(records: List[HandoverRecord]) -> str:
    """格式化交接包列表用于终端输出"""
    if not records:
        return "(无交接包记录)"
    lines = []
    for r in records:
        lines.append(
            f"{r.handover_id}  {r.created_at}  "
            f"batch={r.batch_id}  files={len(r.files)}  "
            f"output={r.output_dir}"
        )
    return "\n".join(lines)


def format_handover_show(record: HandoverRecord) -> str:
    """格式化单个交接包详情用于终端输出"""
    lines = []
    lines.append(f"交接包 ID: {record.handover_id}")
    lines.append(f"创建时间:   {record.created_at}")
    lines.append(f"对应批次:   {record.batch_id}")
    lines.append(f"输出目录:   {record.output_dir}")
    lines.append("")
    lines.append("配置摘要:")
    for k, v in record.config_summary.items():
        lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(f"文件列表 (共 {len(record.files)} 个):")
    for e in record.files:
        lines.append(
            f"  #{e.idx:>3}  {e.relative_path}  "
            f"({e.file_size} bytes, sha256={e.sha256[:12]}...)"
        )
    return "\n".join(lines)


@dataclass
class VerifyIssue:
    """校验发现的问题"""
    level: str  # "error" / "warning"
    code: str
    message: str
    entry_idx: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "entry_idx": self.entry_idx,
        }


@dataclass
class VerifyResult:
    """交接包校验结果"""
    handover_id: str
    created_at: str
    total_files: int
    checked_files: int
    issues: List[VerifyIssue]

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def error_codes(self) -> List[str]:
        """所有 error 级别问题的 code 去重列表。"""
        return sorted({i.code for i in self.issues if i.level == "error"})

    @property
    def exit_code(self) -> int:
        """根据错误类型返回合适的退出码（0 = 全通过）。

        分类:
          12 = 一般性校验失败（源文件缺失、哈希不一致、manifest 异常 等）
          13 = 归档路径缺失（交接包引用的原始归档文件已不存在）
        """
        if not self.has_errors:
            return 0
        codes = self.error_codes
        if "archive_path_missing" in codes:
            return 13
        return 12

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handover_id": self.handover_id,
            "created_at": self.created_at,
            "total_files": self.total_files,
            "checked_files": self.checked_files,
            "has_errors": self.has_errors,
            "error_codes": self.error_codes,
            "exit_code": self.exit_code,
            "issues": [i.to_dict() for i in self.issues],
        }


def verify_result_to_json(result: VerifyResult) -> str:
    """纯 JSON 输出（不夹杂任何提示文本，可管道到 jq 等工具）。"""
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def verify_result_to_csv(result: VerifyResult) -> str:
    """纯 CSV 输出（不夹杂任何提示文本，带 UTF-8 BOM）。

    用 section 字段区分:
      - verify_summary: 整体摘要（1 行）
      - verify_issue: 每个问题一行
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "section", "handover_id", "created_at", "idx",
        "level", "code", "message",
        "total_files", "checked_files", "has_errors", "exit_code",
    ])
    writer.writerow([
        "verify_summary",
        result.handover_id,
        result.created_at,
        "",
        "",
        "",
        "",
        result.total_files,
        result.checked_files,
        result.has_errors,
        result.exit_code,
    ])
    for idx, issue in enumerate(result.issues, start=1):
        writer.writerow([
            "verify_issue",
            result.handover_id,
            result.created_at,
            idx,
            issue.level,
            issue.code,
            issue.message,
            "",
            "",
            "",
            "",
        ])
    return "\ufeff" + buf.getvalue()


def verify_handover(record: HandoverRecord) -> VerifyResult:
    """校验交接包：检查清单里的源/归档路径和哈希是否还匹配。

    校验项:
      - 交接包目录是否存在
      - manifest.json 是否存在且可解析
      - 每个文件是否存在于交接包内
      - 每个文件的 SHA-256 是否与清单匹配
      - 原始归档路径 archive_path 是否仍存在（若存在则校验大小）
      - 原始源路径 source 是否仍存在（提示信息）
    """
    issues: List[VerifyIssue] = []
    total_files = len(record.files)
    checked = 0

    output_dir = Path(record.output_dir)
    if not output_dir.exists():
        issues.append(VerifyIssue(
            level="error",
            code="output_dir_missing",
            message=f"交接包输出目录不存在: {output_dir}",
        ))
        return VerifyResult(
            handover_id=record.handover_id,
            created_at=datetime.now().isoformat(),
            total_files=total_files,
            checked_files=0,
            issues=issues,
        )

    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        issues.append(VerifyIssue(
            level="error",
            code="manifest_missing",
            message=f"manifest.json 不存在: {manifest_path}",
        ))
    else:
        try:
            json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as e:
            issues.append(VerifyIssue(
                level="error",
                code="manifest_corrupted",
                message=f"manifest.json 无法解析: {e}",
            ))

    for entry in record.files:
        checked += 1
        package_file = output_dir / entry.relative_path
        if not package_file.exists():
            issues.append(VerifyIssue(
                level="error",
                code="package_file_missing",
                message=f"交接包内文件缺失: {entry.relative_path}",
                entry_idx=entry.idx,
            ))
            continue

        try:
            actual_sha256 = _sha256_file(package_file)
            if actual_sha256 != entry.sha256:
                issues.append(VerifyIssue(
                    level="error",
                    code="hash_mismatch",
                    message=(
                        f"文件哈希不匹配: {entry.relative_path} "
                        f"(expected={entry.sha256[:12]}..., actual={actual_sha256[:12]}...)"
                    ),
                    entry_idx=entry.idx,
                ))
        except Exception as e:
            issues.append(VerifyIssue(
                level="error",
                code="hash_read_error",
                message=f"无法读取文件哈希 {entry.relative_path}: {e}",
                entry_idx=entry.idx,
            ))

        archive_path = Path(entry.archive_path)
        if not archive_path.exists():
            issues.append(VerifyIssue(
                level="error",
                code="archive_path_missing",
                message=f"原始归档路径已不存在: {entry.archive_path}",
                entry_idx=entry.idx,
            ))
        else:
            try:
                actual_size = archive_path.stat().st_size
                if actual_size != entry.file_size:
                    issues.append(VerifyIssue(
                        level="warning",
                        code="archive_size_mismatch",
                        message=(
                            f"原始归档文件大小不一致: {entry.archive_path} "
                            f"(expected={entry.file_size}, actual={actual_size})"
                        ),
                        entry_idx=entry.idx,
                    ))
            except Exception as e:
                issues.append(VerifyIssue(
                    level="warning",
                    code="archive_stat_error",
                    message=f"无法读取原始归档文件信息 {entry.archive_path}: {e}",
                    entry_idx=entry.idx,
                ))

        source_path = Path(entry.source)
        if not source_path.exists():
            issues.append(VerifyIssue(
                level="error",
                code="source_path_missing",
                message=f"原始源路径已不存在: {entry.source}",
                entry_idx=entry.idx,
            ))

    return VerifyResult(
        handover_id=record.handover_id,
        created_at=datetime.now().isoformat(),
        total_files=total_files,
        checked_files=checked,
        issues=issues,
    )


def format_verify_result(result: VerifyResult) -> str:
    """格式化校验结果用于终端输出"""
    lines = []
    lines.append(f"交接包 ID: {result.handover_id}")
    lines.append(f"校验时间:   {result.created_at}")
    lines.append(f"清单文件:   {result.total_files} 个")
    lines.append(f"已校验:     {result.checked_files} 个")
    lines.append("")

    errors = [i for i in result.issues if i.level == "error"]
    warnings = [i for i in result.issues if i.level == "warning"]

    if not errors and not warnings:
        lines.append("[OK] 所有校验通过！")
    else:
        lines.append(f"错误: {len(errors)} 个")
        for e in errors:
            lines.append(f"  [ERROR] [{e.code}] {e.message}")
        lines.append("")
        lines.append(f"警告: {len(warnings)} 个")
        for w in warnings:
            lines.append(f"  [WARN]  [{w.code}] {w.message}")

    return "\n".join(lines)
