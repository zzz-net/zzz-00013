"""配置体检模块：在 dry-run/run 前检查 YAML 配置和巡检 CSV 是否可用"""
import csv
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any

from .config import ArchiverConfig


@dataclass
class CheckIssue:
    """单个检查项"""
    level: str
    code: str
    message: str
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CheckIssue":
        return cls(
            level=data.get("level", "info"),
            code=data.get("code", ""),
            message=data.get("message", ""),
            detail=data.get("detail", ""),
        )


@dataclass
class DoctorResult:
    """体检结果"""
    doctor_id: str
    created_at: str
    config_path: str
    issues: List[CheckIssue] = field(default_factory=list)
    config_summary: Dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> List[CheckIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> List[CheckIssue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def infos(self) -> List[CheckIssue]:
        return [i for i in self.issues if i.level == "info"]

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "doctor_id": self.doctor_id,
            "created_at": self.created_at,
            "config_path": self.config_path,
            "issues": [i.to_dict() for i in self.issues],
            "config_summary": self.config_summary,
            "counts": {
                "error": len(self.errors),
                "warn": len(self.warnings),
                "info": len(self.infos),
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DoctorResult":
        return cls(
            doctor_id=data["doctor_id"],
            created_at=data["created_at"],
            config_path=data.get("config_path", ""),
            issues=[CheckIssue.from_dict(i) for i in data.get("issues", [])],
            config_summary=data.get("config_summary", {}),
        )


def _check_source_dir(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    src = cfg.source_dir
    if not src.exists():
        issues.append(CheckIssue(
            level="error",
            code="source_dir_missing",
            message="源目录不存在",
            detail=str(src),
        ))
    elif not src.is_dir():
        issues.append(CheckIssue(
            level="error",
            code="source_dir_not_dir",
            message="源路径不是目录",
            detail=str(src),
        ))
    else:
        issues.append(CheckIssue(
            level="info",
            code="source_dir_ok",
            message="源目录存在且可访问",
            detail=str(src),
        ))
    return issues


def _check_archive_dir(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    arc = cfg.archive_dir
    parent = arc.parent
    if not parent.exists():
        issues.append(CheckIssue(
            level="error",
            code="archive_parent_missing",
            message="归档目录的父目录不存在",
            detail=str(parent),
        ))
        return issues
    if not parent.is_dir():
        issues.append(CheckIssue(
            level="error",
            code="archive_parent_not_dir",
            message="归档目录的父路径不是目录",
            detail=str(parent),
        ))
        return issues
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / f".write_probe_{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        issues.append(CheckIssue(
            level="info",
            code="archive_parent_writable",
            message="归档目录父级可写",
            detail=str(parent),
        ))
    except PermissionError:
        issues.append(CheckIssue(
            level="error",
            code="archive_parent_no_write",
            message="归档目录父级没有写入权限",
            detail=str(parent),
        ))
    except Exception as e:
        issues.append(CheckIssue(
            level="error",
            code="archive_parent_unreachable",
            message="归档目录父级无法访问",
            detail=f"{parent}: {e}",
        ))
    return issues


def _check_state_dir(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    sd = cfg.state_dir
    try:
        sd.mkdir(parents=True, exist_ok=True)
        probe = sd / f".write_probe_{os.getpid()}"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
        issues.append(CheckIssue(
            level="info",
            code="state_dir_writable",
            message="状态目录可写",
            detail=str(sd),
        ))
    except PermissionError:
        issues.append(CheckIssue(
            level="error",
            code="state_dir_no_write",
            message="状态目录没有写入权限",
            detail=str(sd),
        ))
    except Exception as e:
        issues.append(CheckIssue(
            level="error",
            code="state_dir_unreachable",
            message="状态目录无法访问",
            detail=f"{sd}: {e}",
        ))
    return issues


def _check_csv_file(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    csv_path = cfg.csv_path
    if not csv_path.exists():
        issues.append(CheckIssue(
            level="error",
            code="csv_missing",
            message="CSV 文件不存在",
            detail=str(csv_path),
        ))
        return issues
    if not csv_path.is_file():
        issues.append(CheckIssue(
            level="error",
            code="csv_not_file",
            message="CSV 路径不是文件",
            detail=str(csv_path),
        ))
        return issues
    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            if not fieldnames:
                issues.append(CheckIssue(
                    level="error",
                    code="csv_empty_header",
                    message="CSV 没有表头行",
                    detail=str(csv_path),
                ))
                return issues
            issues.append(CheckIssue(
                level="info",
                code="csv_readable",
                message="CSV 文件可读",
                detail=str(csv_path),
            ))
            required = ["device_id", "point", "date", "photo_name"]
            missing_cols = []
            for key in required:
                col_name = cfg.csv_columns.get(key, key)
                if col_name not in fieldnames:
                    missing_cols.append(col_name)
            if missing_cols:
                issues.append(CheckIssue(
                    level="error",
                    code="csv_missing_columns",
                    message="CSV 缺少必要列",
                    detail=f"缺少: {', '.join(missing_cols)}; 当前列: {', '.join(fieldnames)}",
                ))
            else:
                issues.append(CheckIssue(
                    level="info",
                    code="csv_columns_ok",
                    message="CSV 包含所有必要列",
                    detail=f"列: {', '.join(fieldnames)}",
                ))
            row_count = sum(1 for _ in reader)
            if row_count == 0:
                issues.append(CheckIssue(
                    level="warn",
                    code="csv_no_rows",
                    message="CSV 没有数据行",
                    detail=str(csv_path),
                ))
            else:
                issues.append(CheckIssue(
                    level="info",
                    code="csv_has_rows",
                    message=f"CSV 包含 {row_count} 条数据行",
                    detail=str(csv_path),
                ))
    except UnicodeDecodeError as e:
        issues.append(CheckIssue(
            level="error",
            code="csv_encoding_error",
            message="CSV 文件编码错误（需 UTF-8 或 UTF-8-BOM）",
            detail=f"{csv_path}: {e}",
        ))
    except Exception as e:
        issues.append(CheckIssue(
            level="error",
            code="csv_read_error",
            message="CSV 文件读取失败",
            detail=f"{csv_path}: {e}",
        ))
    return issues


def _check_action(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    if cfg.action not in ("copy", "move"):
        issues.append(CheckIssue(
            level="error",
            code="action_invalid",
            message="action 必须是 'copy' 或 'move'",
            detail=f"当前值: {cfg.action}",
        ))
    else:
        issues.append(CheckIssue(
            level="info",
            code="action_ok",
            message=f"action 设置正确: {cfg.action}",
            detail=cfg.action,
        ))
    return issues


def _check_target_pattern(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    tp = cfg.target_pattern
    if not isinstance(tp, str) or not tp:
        issues.append(CheckIssue(
            level="error",
            code="target_pattern_empty",
            message="target_pattern 必须是非空字符串",
            detail=f"当前值: {tp!r}",
        ))
        return issues
    required_placeholders = ["{device_id}", "{point}", "{date}", "{filename}"]
    missing_ph = []
    for ph in required_placeholders:
        if ph not in tp:
            missing_ph.append(ph)
    if missing_ph:
        issues.append(CheckIssue(
            level="error",
            code="target_pattern_missing_placeholder",
            message="target_pattern 缺少必要占位符",
            detail=f"缺少: {', '.join(missing_ph)}; 当前值: {tp}",
        ))
    else:
        issues.append(CheckIssue(
            level="info",
            code="target_pattern_ok",
            message="target_pattern 包含所有必要占位符",
            detail=tp,
        ))
    return issues


def _check_photo_extensions(cfg: ArchiverConfig) -> List[CheckIssue]:
    issues: List[CheckIssue] = []
    exts = cfg.photo_extensions
    if not isinstance(exts, list) or not exts:
        issues.append(CheckIssue(
            level="error",
            code="photo_extensions_empty",
            message="photo_extensions 必须是非空列表",
            detail=f"当前值: {exts!r}",
        ))
        return issues
    invalid = []
    normalized = []
    for e in exts:
        if not isinstance(e, str):
            invalid.append(f"{e!r}")
            continue
        ne = e.lower()
        if not ne.startswith("."):
            invalid.append(e)
        normalized.append(ne)
    if invalid:
        issues.append(CheckIssue(
            level="warn",
            code="photo_extensions_invalid",
            message="部分 photo_extensions 格式异常（应以点号开头）",
            detail=f"异常项: {', '.join(invalid)}",
        ))
    issues.append(CheckIssue(
        level="info",
        code="photo_extensions_ok",
        message=f"photo_extensions 已配置 {len(normalized)} 个扩展名",
        detail=", ".join(normalized),
    ))
    return issues


def run_doctor_checks(cfg: ArchiverConfig, config_path: str) -> DoctorResult:
    """执行所有体检检查，返回 DoctorResult"""
    from datetime import datetime
    import uuid

    issues: List[CheckIssue] = []
    issues.extend(_check_source_dir(cfg))
    issues.extend(_check_archive_dir(cfg))
    issues.extend(_check_state_dir(cfg))
    issues.extend(_check_csv_file(cfg))
    issues.extend(_check_action(cfg))
    issues.extend(_check_target_pattern(cfg))
    issues.extend(_check_photo_extensions(cfg))

    doctor_id = f"doctor_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    created_at = datetime.now().isoformat()

    config_summary = {
        "source_dir": str(cfg.source_dir),
        "archive_dir": str(cfg.archive_dir),
        "csv_path": str(cfg.csv_path),
        "state_dir": str(cfg.state_dir),
        "photo_extensions": list(cfg.photo_extensions),
        "action": cfg.action,
        "target_pattern": cfg.target_pattern,
        "csv_columns": dict(cfg.csv_columns),
    }

    return DoctorResult(
        doctor_id=doctor_id,
        created_at=created_at,
        config_path=str(Path(config_path).resolve()),
        issues=issues,
        config_summary=config_summary,
    )


def format_doctor_result(result: DoctorResult) -> str:
    """格式化体检结果用于终端输出"""
    lines = []
    lines.append(f"体检 ID: {result.doctor_id}")
    lines.append(f"检查时间: {result.created_at}")
    lines.append(f"配置文件: {result.config_path}")
    lines.append("")
    counts = {
        "error": len(result.errors),
        "warn": len(result.warnings),
        "info": len(result.infos),
    }
    lines.append(
        f"总计: {len(result.issues)} 项检查  "
        f"[ERROR] {counts['error']}  "
        f"[WARN] {counts['warn']}  "
        f"[INFO] {counts['info']}"
    )
    lines.append("")

    level_marks = {"error": "[ERROR]", "warn": "[WARN] ", "info": "[INFO] "}

    for level in ("error", "warn", "info"):
        level_issues = [i for i in result.issues if i.level == level]
        if not level_issues:
            continue
        for issue in level_issues:
            mark = level_marks[level]
            lines.append(f"{mark} [{issue.code}] {issue.message}")
            if issue.detail:
                lines.append(f"          {issue.detail}")

    if result.has_errors:
        lines.append("")
        lines.append("!!! 存在错误，建议先修复后再执行 dry-run/run !!!")

    return "\n".join(lines)


def format_doctor_history(results: List[DoctorResult]) -> str:
    """格式化体检历史列表用于终端输出"""
    if not results:
        return "(无体检记录)"
    lines = []
    for r in results:
        status = "[FAIL]" if r.has_errors else "[PASS]"
        counts = {
            "error": len(r.errors),
            "warn": len(r.warnings),
            "info": len(r.infos),
        }
        lines.append(
            f"{r.doctor_id}  {status}  {r.created_at}  "
            f"E={counts['error']}  W={counts['warn']}  I={counts['info']}  "
            f"{Path(r.config_path).name}"
        )
    return "\n".join(lines)
