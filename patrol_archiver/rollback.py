"""回滚与报告导出"""
import csv
import hashlib
import io
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    "source_signature",
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
            getattr(a, "source_signature", None) or "",
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


AUDIT_SUMMARY_FIELDS = [
    "audit_id",
    "batch_id",
    "created_at",
    "source_dir",
    "archive_dir",
    "config_path_changed",
    "success",
    "missing_in_archive",
    "missing_in_source",
    "overwritten",
    "tampered",
    "rollback_risk",
    "original_failed",
    "original_pending",
    "extra_archive_files",
    "warnings_total",
]

AUDIT_FILE_FIELDS = [
    "section",
    "audit_id",
    "batch_id",
    "idx",
    "original_status",
    "audit_status",
    "action",
    "source",
    "target",
    "source_exists",
    "archive_exists",
    "signature_match",
    "detail",
]

AUDIT_WARNING_FIELDS = [
    "section",
    "audit_id",
    "batch_id",
    "idx",
    "level",
    "code",
    "message",
]

AUDIT_EXTRA_FIELDS = [
    "section",
    "audit_id",
    "batch_id",
    "idx",
    "path",
]

AUDIT_CONFIG_DIFF_FIELDS = [
    "section",
    "audit_id",
    "batch_id",
    "idx",
    "config_key",
    "batch_value",
    "current_value",
]


def _build_audit_summary_row(audit: Any) -> dict:
    counts = audit.counts or {}
    warnings_total = len(audit.warnings) if audit.warnings else 0
    return {
        "audit_id": audit.audit_id,
        "batch_id": audit.batch_id,
        "created_at": audit.created_at,
        "source_dir": audit.source_dir,
        "archive_dir": audit.archive_dir,
        "config_path_changed": "true" if audit.config_path_changed else "false",
        "success": counts.get("success", 0),
        "missing_in_archive": counts.get("missing_in_archive", 0),
        "missing_in_source": counts.get("missing_in_source", 0),
        "overwritten": counts.get("overwritten", 0),
        "tampered": counts.get("tampered", 0),
        "rollback_risk": counts.get("rollback_risk", 0),
        "original_failed": counts.get("original_failed", 0),
        "original_pending": counts.get("original_pending", 0),
        "extra_archive_files": len(audit.extra_archive_files) if audit.extra_archive_files else 0,
        "warnings_total": warnings_total,
    }


def export_audit_csv(audit: Any, output_path: Path) -> Path:
    """导出审计报告 CSV：稳定字段名，包含 summary / file / warning / extra / config_diff 段"""
    from .auditor import AuditResult
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    summary = _build_audit_summary_row(audit)
    writer.writerow(["section"] + AUDIT_SUMMARY_FIELDS)
    writer.writerow(["audit_summary"] + [summary.get(k, "") for k in AUDIT_SUMMARY_FIELDS])
    writer.writerow([])

    writer.writerow(AUDIT_FILE_FIELDS)
    for idx, rec in enumerate(audit.file_records, start=1):
        writer.writerow([
            "audit_file",
            audit.audit_id,
            audit.batch_id,
            idx,
            rec.original_status,
            rec.audit_status,
            rec.action,
            rec.source,
            rec.target,
            "true" if rec.source_exists else "false",
            "true" if rec.archive_exists else "false",
            "" if rec.signature_match is None else ("true" if rec.signature_match else "false"),
            rec.detail or "",
        ])

    writer.writerow([])
    writer.writerow(AUDIT_WARNING_FIELDS)
    if audit.warnings:
        for idx, w in enumerate(audit.warnings, start=1):
            writer.writerow([
                "audit_warning",
                audit.audit_id,
                audit.batch_id,
                idx,
                w.level,
                w.code,
                w.message,
            ])
    else:
        writer.writerow(["audit_warning", audit.audit_id, audit.batch_id, 0, "", "", ""])

    writer.writerow([])
    writer.writerow(AUDIT_EXTRA_FIELDS)
    if audit.extra_archive_files:
        for idx, p in enumerate(audit.extra_archive_files, start=1):
            writer.writerow([
                "audit_extra",
                audit.audit_id,
                audit.batch_id,
                idx,
                p,
            ])
    else:
        writer.writerow(["audit_extra", audit.audit_id, audit.batch_id, 0, ""])

    writer.writerow([])
    writer.writerow(AUDIT_CONFIG_DIFF_FIELDS)
    if audit.config_diff:
        idx = 0
        for key, pair in (audit.config_diff or {}).items():
            idx += 1
            writer.writerow([
                "audit_config_diff",
                audit.audit_id,
                audit.batch_id,
                idx,
                key,
                pair.get("batch", ""),
                pair.get("current", ""),
            ])
    else:
        writer.writerow(["audit_config_diff", audit.audit_id, audit.batch_id, 0, "", "", ""])

    output_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return output_path


def export_audit_report(audit: Any, output_path: Path, fmt: Optional[str] = None) -> Path:
    """导出审计报告。fmt=None 时按扩展名自动识别（json/csv）"""
    fmt = (fmt or detect_format(output_path)).lower()
    if fmt == "csv":
        return export_audit_csv(audit, output_path)
    if fmt == "json":
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(audit.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        return output_path
    raise ValueError(f"不支持的审计导出格式: {fmt}，请使用 json 或 csv")


def format_audit_summary(audit: Any) -> str:
    """格式化审计摘要用于终端输出"""
    from .auditor import AuditResult
    lines = []
    lines.append(f"审计 ID: {audit.audit_id}")
    lines.append(f"对应批次: {audit.batch_id}")
    lines.append(f"审计时间: {audit.created_at}")
    lines.append(f"源目录: {audit.source_dir}")
    lines.append(f"归档目录: {audit.archive_dir}")

    if audit.config_path_changed:
        lines.append("")
        lines.append("!!! 配置已变更（对账结果可能不准确） !!!")
        for key, pair in audit.config_diff.items():
            lines.append(f"  {key}: 批次='{pair.get('batch')}' 当前='{pair.get('current')}'")

    lines.append("")
    lines.append("=== 对账统计 ===")
    counts = audit.counts or {}
    lines.append(f"  [OK] 成功一致:              {counts.get('success', 0)}")
    lines.append(f"  [MISS-A] 归档缺失:          {counts.get('missing_in_archive', 0)}")
    lines.append(f"  [MISS-S] 源文件缺失:        {counts.get('missing_in_source', 0)}")
    lines.append(f"  [OVER] 被覆盖/替换:         {counts.get('overwritten', 0)}")
    lines.append(f"  [TAMP] 被篡改:              {counts.get('tampered', 0)}")
    lines.append(f"  [RISK] 回滚风险:            {counts.get('rollback_risk', 0)}")
    lines.append(f"  [FAIL] 原始失败动作:        {counts.get('original_failed', 0)}")
    lines.append(f"  [PEND] 原始待执行动作:      {counts.get('original_pending', 0)}")
    lines.append(f"  [EXTRA] 归档额外文件:       {len(audit.extra_archive_files) if audit.extra_archive_files else 0}")

    if audit.warnings:
        lines.append("")
        lines.append("=== 警告/风险 ===")
        for w in audit.warnings:
            tag = {"warning": "[WARN]", "error": "[ERR]", "risk": "[RISK]"}.get(w.level, "[?]")
            lines.append(f"  {tag} [{w.code}] {w.message}")

    return "\n".join(lines)


def format_audit_history(audits: List[Any]) -> str:
    """格式化审计历史列表用于终端输出"""
    if not audits:
        return "(无审计记录)"
    lines = []
    for a in audits:
        counts = a.counts or {}
        total_issues = (
            counts.get("missing_in_archive", 0)
            + counts.get("missing_in_source", 0)
            + counts.get("overwritten", 0)
            + counts.get("tampered", 0)
            + counts.get("rollback_risk", 0)
        )
        warnings_total = len(a.warnings) if a.warnings else 0
        cfg_tag = "[CFG-CHG]" if a.config_path_changed else "         "
        health = "[HEALTHY]" if total_issues == 0 and warnings_total == 0 else "[ISSUES]"
        lines.append(
            f"{a.audit_id}  {a.created_at}  {cfg_tag}  {health}  "
            f"OK={counts.get('success', 0)}  ISSUES={total_issues}  "
            f"WARN={warnings_total}  EXTRA={len(a.extra_archive_files) if a.extra_archive_files else 0}"
        )
    return "\n".join(lines)


TEMPLATE_SUMMARY_FIELDS = [
    "name",
    "created_at",
    "updated_at",
    "description",
]

TEMPLATE_CONFIG_FIELDS = [
    "section",
    "name",
    "idx",
    "config_key",
    "config_value",
]


def _template_config_rows(template: Any) -> List[Dict[str, Any]]:
    from .storage import ConfigTemplate
    rows = []
    config = template.config or {}
    idx = 0
    for key, value in config.items():
        idx += 1
        if isinstance(value, (list, dict)):
            val_str = json.dumps(value, ensure_ascii=False)
        else:
            val_str = str(value)
        rows.append({
            "section": "template_config",
            "name": template.name,
            "idx": idx,
            "config_key": key,
            "config_value": val_str,
        })
    return rows


def export_template_csv(template: Any, output_path: Path) -> Path:
    """导出单个模板为 CSV：包含模板摘要 + 配置键值对"""
    from .storage import ConfigTemplate
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(["section"] + TEMPLATE_SUMMARY_FIELDS)
    writer.writerow([
        "template_summary",
        template.name,
        template.created_at,
        template.updated_at,
        template.description or "",
    ])
    writer.writerow([])

    writer.writerow(TEMPLATE_CONFIG_FIELDS)
    for row in _template_config_rows(template):
        writer.writerow([
            row["section"], row["name"], row["idx"], row["config_key"], row["config_value"]
        ])

    output_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return output_path


def export_template_json(template: Any, output_path: Path) -> Path:
    """导出单个模板为 JSON"""
    from .storage import ConfigTemplate
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(template.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return output_path


def export_templates_csv(templates: List[Any], output_path: Path) -> Path:
    """导出多个模板列表为 CSV"""
    from .storage import ConfigTemplate
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(["section"] + TEMPLATE_SUMMARY_FIELDS + ["config_keys"])
    for tpl in templates:
        config_keys = ",".join(sorted((tpl.config or {}).keys()))
        writer.writerow([
            "template_summary",
            tpl.name,
            tpl.created_at,
            tpl.updated_at,
            tpl.description or "",
            config_keys,
        ])

    output_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    return output_path


def export_templates_json(templates: List[Any], output_path: Path) -> Path:
    """导出多个模板列表为 JSON"""
    from .storage import ConfigTemplate
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps([t.to_dict() for t in templates],
        ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return output_path


def format_template_list(templates: List[Any]) -> str:
    """格式化模板列表用于终端输出"""
    from .storage import ConfigTemplate
    if not templates:
        return "(无模板)"
    lines = []
    for tpl in templates:
        desc = f" - {tpl.description}" if tpl.description else ""
        lines.append(
            f"{tpl.name}  创建: {tpl.created_at}  更新: {tpl.updated_at}{desc}"
        )
    return "\n".join(lines)


def format_template_show(template: Any) -> str:
    """格式化单个模板详情用于终端输出"""
    from .storage import ConfigTemplate
    lines = []
    lines.append(f"模板名称: {template.name}")
    lines.append(f"创建时间: {template.created_at}")
    lines.append(f"更新时间: {template.updated_at}")
    if template.description:
        lines.append(f"描述: {template.description}")
    lines.append("")
    lines.append("配置内容:")
    config = template.config or {}
    for key, value in config.items():
        if isinstance(value, (list, dict)):
            val_str = json.dumps(value, ensure_ascii=False, indent=2)
            lines.append(f"  {key}:")
            for line in val_str.splitlines():
                lines.append(f"    {line}")
        else:
            lines.append(f"  {key}: {value}")
    return "\n".join(lines)
