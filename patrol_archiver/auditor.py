"""批次对账审计：逐项比对状态记录与实际文件系统"""
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .storage import Batch, FileActionRecord, StateStore, file_signature


@dataclass
class FileAuditRecord:
    """单个文件动作的审计结果"""
    idx: int
    source: str
    target: str
    action: str
    original_status: str
    audit_status: str  # success / missing_in_archive / missing_in_source / overwritten / tampered / rollback_risk
    detail: str = ""
    source_exists: bool = False
    archive_exists: bool = False
    signature_match: Optional[bool] = None  # None=未比对/无可比对值, True=匹配, False=不匹配

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AuditWarning:
    """审计级别的警告（非单个文件）"""
    level: str  # "warning" / "error" / "risk"
    code: str  # 稳定代码
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AuditResult:
    """完整审计结果"""
    audit_id: str
    batch_id: str
    created_at: str
    source_dir: str
    archive_dir: str
    config_path_changed: bool
    config_diff: Dict[str, Dict[str, str]]
    warnings: List[AuditWarning] = field(default_factory=list)
    file_records: List[FileAuditRecord] = field(default_factory=list)
    extra_archive_files: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "source_dir": self.source_dir,
            "archive_dir": self.archive_dir,
            "config_path_changed": self.config_path_changed,
            "config_diff": self.config_diff,
            "warnings": [w.to_dict() for w in self.warnings],
            "file_records": [r.to_dict() for r in self.file_records],
            "extra_archive_files": list(self.extra_archive_files),
            "counts": dict(self.counts),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuditResult":
        return cls(
            audit_id=data["audit_id"],
            batch_id=data["batch_id"],
            created_at=data["created_at"],
            source_dir=data.get("source_dir", ""),
            archive_dir=data.get("archive_dir", ""),
            config_path_changed=data.get("config_path_changed", False),
            config_diff=data.get("config_diff", {}),
            warnings=[AuditWarning(**w) for w in data.get("warnings", [])],
            file_records=[FileAuditRecord(**r) for r in data.get("file_records", [])],
            extra_archive_files=list(data.get("extra_archive_files", [])),
            counts=dict(data.get("counts", {})),
        )


def _scan_archive_files(archive_dir: Path) -> List[Path]:
    """递归扫描归档目录所有文件"""
    if not archive_dir.exists():
        return []
    result: List[Path] = []
    for root, _, files in os.walk(archive_dir):
        for fname in files:
            fpath = Path(root) / fname
            # 跳过状态目录自身
            result.append(fpath.resolve())
    return result


def _collect_known_targets(batch: Batch) -> set:
    known = set()
    for a in batch.actions:
        if a.status in ("success", "rolled_back"):
            try:
                known.add(str(Path(a.target).resolve()))
            except Exception:
                known.add(a.target)
    return known


def _compare_config(
    batch: Batch,
    current_source_dir: Path,
    current_archive_dir: Path,
) -> Tuple[bool, Dict[str, Dict[str, str]]]:
    """比对批次执行时的配置路径与当前配置路径"""
    diff: Dict[str, Dict[str, str]] = {}
    changed = False

    batch_source = batch.config_summary.get("source_dir", "")
    batch_archive = batch.config_summary.get("archive_dir", "")

    current_source_str = str(current_source_dir.resolve())
    current_archive_str = str(current_archive_dir.resolve())

    if batch_source and batch_source != current_source_str:
        changed = True
        diff["source_dir"] = {"batch": batch_source, "current": current_source_str}

    if batch_archive and batch_archive != current_archive_str:
        changed = True
        diff["archive_dir"] = {"batch": batch_archive, "current": current_archive_str}

    return changed, diff


class Auditor:
    """批次审计器：读取批次记录，与当前文件系统逐项对账"""

    def __init__(self, store: StateStore):
        self.store = store

    def audit(
        self,
        batch: Batch,
        current_source_dir: Path,
        current_archive_dir: Path,
    ) -> AuditResult:
        """执行对账审计，返回 AuditResult"""
        audit_id = f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        # 1) 配置路径比对
        cfg_changed, cfg_diff = _compare_config(batch, current_source_dir, current_archive_dir)

        # 2) 扫描归档目录中所有实际存在的文件
        archive_files = _scan_archive_files(current_archive_dir)
        known_targets = _collect_known_targets(batch)

        # 找出额外文件（归档目录中存在但批次未记录的文件）
        extra_files: List[str] = []
        for fp in archive_files:
            fp_str = str(fp)
            if fp_str not in known_targets:
                extra_files.append(fp_str)

        # 3) 逐项对账文件动作
        file_records: List[FileAuditRecord] = []
        warnings: List[AuditWarning] = []
        counts: Dict[str, int] = {
            "success": 0,
            "missing_in_archive": 0,
            "missing_in_source": 0,
            "overwritten": 0,
            "tampered": 0,
            "rollback_risk": 0,
            "original_failed": 0,
            "original_pending": 0,
        }

        # 归档目录下，已知目标的文件签名快照（用于评估回滚时是否被篡改）
        # 注意：我们不持久化签名，而是用动作记录的原始状态推断合理期望
        for idx, action in enumerate(batch.actions, start=1):
            rec = self._audit_one_action(
                idx, action, current_source_dir, current_archive_dir
            )
            file_records.append(rec)

            # 归类计数
            if rec.original_status not in ("success", "rolled_back"):
                if rec.original_status == "failed":
                    counts["original_failed"] = counts.get("original_failed", 0) + 1
                elif rec.original_status == "pending":
                    counts["original_pending"] = counts.get("original_pending", 0) + 1
                continue

            counts[rec.audit_status] = counts.get(rec.audit_status, 0) + 1

        # 4) 配置变更警告
        if cfg_changed:
            for key, pair in cfg_diff.items():
                warnings.append(AuditWarning(
                    level="warning",
                    code=f"config_{key}_changed",
                    message=f"配置路径 {key} 已变更：批次时='{pair['batch']}'，当前='{pair['current']}'，对账结果可能不准确",
                ))

        # 5) 回滚风险聚合评估
        rollback_risks = [r for r in file_records if r.audit_status == "rollback_risk"]
        if rollback_risks:
            warnings.append(AuditWarning(
                level="risk",
                code="rollback_risk_present",
                message=f"检测到 {len(rollback_risks)} 个文件存在回滚风险，执行 rollback 可能失败或导致数据丢失",
            ))

        # 6) 归档目录额外文件警告
        if extra_files:
            warnings.append(AuditWarning(
                level="warning",
                code="extra_archive_files",
                message=f"归档目录中存在 {len(extra_files)} 个本批次未记录的额外文件",
            ))

        # 7) 被篡改/覆盖聚合警告
        overwritten = [r for r in file_records if r.audit_status == "overwritten"]
        tampered = [r for r in file_records if r.audit_status == "tampered"]
        if overwritten:
            warnings.append(AuditWarning(
                level="error",
                code="files_overwritten",
                message=f"{len(overwritten)} 个归档目标文件被其他内容覆盖/替换",
            ))
        if tampered:
            warnings.append(AuditWarning(
                level="error",
                code="files_tampered",
                message=f"{len(tampered)} 个文件内容被篡改（大小或修改时间异常）",
            ))

        missing_in_archive = [r for r in file_records if r.audit_status == "missing_in_archive"]
        if missing_in_archive:
            warnings.append(AuditWarning(
                level="error",
                code="files_missing_in_archive",
                message=f"{len(missing_in_archive)} 个归档目标文件缺失（可能被手动删除或移动）",
            ))

        result = AuditResult(
            audit_id=audit_id,
            batch_id=batch.batch_id,
            created_at=datetime.now().isoformat(),
            source_dir=str(current_source_dir.resolve()),
            archive_dir=str(current_archive_dir.resolve()),
            config_path_changed=cfg_changed,
            config_diff=cfg_diff,
            warnings=warnings,
            file_records=file_records,
            extra_archive_files=extra_files,
            counts=counts,
        )

        return result

    def _audit_one_action(
        self,
        idx: int,
        action: FileActionRecord,
        current_source_dir: Path,
        current_archive_dir: Path,
    ) -> FileAuditRecord:
        """审计单个文件动作记录"""
        src = Path(action.source)
        tgt = Path(action.target)
        src_exists = src.exists() and src.is_file()
        tgt_exists = tgt.exists() and tgt.is_file()

        audit_status = "success"
        detail = ""

        # 原始动作没成功，直接跳过深度审计
        if action.status not in ("success", "rolled_back"):
            audit_status = "success" if action.status == "rolled_back" else "success"
            detail = f"原始状态={action.status}"
            return FileAuditRecord(
                idx=idx,
                source=action.source,
                target=action.target,
                action=action.action,
                original_status=action.status,
                audit_status=audit_status,
                detail=detail,
                source_exists=src_exists,
                archive_exists=tgt_exists,
            )

        if action.status == "rolled_back":
            # 已回滚：目标应不存在，源（move 的话）应存在
            if action.action == "move":
                if not src_exists:
                    audit_status = "rollback_risk"
                    detail = "已标记回滚，但源文件仍缺失，可能回滚未完整执行或被二次修改"
                elif tgt_exists:
                    audit_status = "rollback_risk"
                    detail = "已标记回滚，但归档目标仍存在"
                else:
                    audit_status = "success"
                    detail = "回滚状态正常"
            else:  # copy
                if tgt_exists:
                    audit_status = "rollback_risk"
                    detail = "已标记回滚（copy），但归档目标文件仍存在"
                else:
                    audit_status = "success"
                    detail = "回滚状态正常"
            return FileAuditRecord(
                idx=idx,
                source=action.source,
                target=action.target,
                action=action.action,
                original_status=action.status,
                audit_status=audit_status,
                detail=detail,
                source_exists=src_exists,
                archive_exists=tgt_exists,
            )

        # 以下是 original_status == "success" 的场景
        signature_match: Optional[bool] = None
        if not tgt_exists:
            audit_status = "missing_in_archive"
            detail = "归档目标文件缺失（可能被手动删除/移动）"
        else:
            # 目标存在，根据动作类型评估
            if action.action == "copy":
                # copy: 源应仍然存在，目标应存在
                if not src_exists:
                    audit_status = "missing_in_source"
                    detail = "copy 动作记录成功，但源文件已不存在（可能被手动删除/move）"
                else:
                    # 优先用签名比对（识别同大小异内容）
                    recorded_sig = getattr(action, "source_signature", None)
                    try:
                        if recorded_sig:
                            # 新批次：用签名严格比对
                            tgt_sig = file_signature(tgt)
                            if tgt_sig is None:
                                audit_status = "tampered"
                                detail = "无法读取归档文件签名"
                                signature_match = False
                            elif tgt_sig != recorded_sig:
                                audit_status = "overwritten"
                                signature_match = False
                                src_stat = src.stat()
                                tgt_stat = tgt.stat()
                                if src_stat.st_size == tgt_stat.st_size:
                                    detail = (
                                        "归档目标文件与源签名不一致，文件大小相同但内容已被替换（同字节数异内容）"
                                    )
                                else:
                                    detail = (
                                        f"归档目标文件签名与源不一致（源={src_stat.st_size}B, "
                                        f"归档={tgt_stat.st_size}B）"
                                    )
                            else:
                                audit_status = "success"
                                signature_match = True
                                detail = "归档目标与源签名一致，文件完整"
                        else:
                            # 老批次兼容：无签名记录，回退到大小比对
                            src_stat = src.stat()
                            tgt_stat = tgt.stat()
                            if src_stat.st_size != tgt_stat.st_size:
                                audit_status = "overwritten"
                                signature_match = None
                                detail = (
                                    f"目标文件大小与源不一致（源={src_stat.st_size}, "
                                    f"归档={tgt_stat.st_size}），可能被其他内容覆盖"
                                    "（老批次无签名记录，无法做内容级验签）"
                                )
                            else:
                                audit_status = "success"
                                signature_match = None
                                detail = "文件存在且大小一致（老批次无签名记录，未做内容级验签）"
                    except Exception as e:
                        audit_status = "tampered"
                        detail = f"无法读取文件属性进行比对: {e}"
            elif action.action == "move":
                # move: 源应不存在，目标应存在
                recorded_sig = getattr(action, "source_signature", None)
                if recorded_sig:
                    # move 动作签名校验：归档目标签名应与记录的源签名一致
                    tgt_sig = file_signature(tgt)
                    if tgt_sig and tgt_sig != recorded_sig:
                        signature_match = False
                        audit_status = "overwritten"
                        detail = "归档目标签名与记录的源不一致，文件内容可能已被替换/篡改"
                    elif tgt_sig is None:
                        signature_match = False
                        audit_status = "tampered"
                        detail = "无法读取归档文件签名"
                    else:
                        signature_match = True
                if src_exists:
                    if audit_status == "success":
                        audit_status = "rollback_risk"
                    detail = detail or "move 动作记录成功，但源文件仍存在（可能被其他批次重新写入或回滚过）"
                else:
                    if audit_status == "success":
                        detail = detail or "move 状态正常（源已移走，归档存在且签名一致）"

        return FileAuditRecord(
            idx=idx,
            source=action.source,
            target=action.target,
            action=action.action,
            original_status=action.status,
            audit_status=audit_status,
            detail=detail,
            source_exists=src_exists,
            archive_exists=tgt_exists,
            signature_match=signature_match,
        )
