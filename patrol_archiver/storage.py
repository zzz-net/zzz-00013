"""批次状态持久化存储"""
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def file_signature(path: Path) -> Optional[str]:
    """计算文件签名（大小+修改时间+前8KB MD5），用于判断文件是否被覆盖/篡改。
    Executor 和 Auditor 都调用此函数以保证算法一致。"""
    try:
        if not path.exists() or not path.is_file():
            return None
        stat = path.stat()
        size = stat.st_size
        mtime = stat.st_mtime_ns
        h = hashlib.md5()
        h.update(f"{size}_{mtime}".encode("utf-8"))
        try:
            with open(path, "rb") as f:
                chunk = f.read(min(8192, size))
                h.update(chunk)
        except Exception:
            pass
        return h.hexdigest()
    except Exception:
        return None


@dataclass
class FileActionRecord:
    source: str
    target: str
    action: str
    status: str  # "success", "failed", "pending"
    error: Optional[str] = None
    source_signature: Optional[str] = None  # 源文件签名（大小+mtime+前8KB MD5，用于审计时验签


@dataclass
class PlanSummary:
    missing: List[Dict[str, Any]] = field(default_factory=list)
    extra_files: List[str] = field(default_factory=list)
    duplicate_targets: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    path_conflicts: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "PlanSummary":
        if not data:
            return cls()
        return cls(
            missing=list(data.get("missing", [])),
            extra_files=list(data.get("extra_files", [])),
            duplicate_targets=dict(data.get("duplicate_targets", {})),
            path_conflicts=list(data.get("path_conflicts", [])),
        )


@dataclass
class Batch:
    batch_id: str
    created_at: str
    status: str  # "pending", "running", "completed", "failed", "rolled_back"
    config_summary: Dict[str, Any]
    actions: List[FileActionRecord] = field(default_factory=list)
    report_path: Optional[str] = None
    error: Optional[str] = None
    dry_run: bool = False
    plan_summary: PlanSummary = field(default_factory=PlanSummary)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "status": self.status,
            "config_summary": self.config_summary,
            "actions": [asdict(a) for a in self.actions],
            "report_path": self.report_path,
            "error": self.error,
            "dry_run": self.dry_run,
            "plan_summary": self.plan_summary.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Batch":
        return cls(
            batch_id=data["batch_id"],
            created_at=data["created_at"],
            status=data["status"],
            config_summary=data.get("config_summary", {}),
            actions=[FileActionRecord(**a) for a in data.get("actions", [])],
            report_path=data.get("report_path"),
            error=data.get("error"),
            dry_run=data.get("dry_run", False),
            plan_summary=PlanSummary.from_dict(data.get("plan_summary")),
        )


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.batches_dir = state_dir / "batches"
        self.audits_dir = state_dir / "audits"
        self.index_file = state_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)
        self.audits_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self._write_index(
                {"batches": [], "last_batch_id": None, "audits": {}, "last_audit_id": None}
            )

    def _read_index(self) -> Dict[str, Any]:
        try:
            raw = self.index_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
            backup = None
            if self.index_file.exists():
                try:
                    backup = self.index_file.read_bytes()
                except Exception:
                    pass
            data = {"batches": [], "last_batch_id": None, "audits": {}, "last_audit_id": None}
            try:
                self._write_index(data)
            except Exception:
                pass
            if backup:
                try:
                    (self.state_dir / "index.corrupted.bak").write_bytes(backup)
                except Exception:
                    pass
        data.setdefault("batches", [])
        data.setdefault("audits", {})
        data.setdefault("last_batch_id", None)
        data.setdefault("last_audit_id", None)
        return data

    def _write_index(self, data: Dict[str, Any]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _batch_file(self, batch_id: str) -> Path:
        return self.batches_dir / f"{batch_id}.json"

    def create_batch(self, config_summary: Dict[str, Any], dry_run: bool = False) -> Batch:
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        batch = Batch(
            batch_id=batch_id,
            created_at=datetime.now().isoformat(),
            status="pending",
            config_summary=config_summary,
            dry_run=dry_run,
        )
        self.save_batch(batch)
        idx = self._read_index()
        idx["batches"].insert(0, batch_id)
        idx["last_batch_id"] = batch_id
        self._write_index(idx)
        return batch

    def save_batch(self, batch: Batch) -> None:
        f = self._batch_file(batch.batch_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(batch.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        f = self._batch_file(batch_id)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return Batch.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None

    def list_batches(self) -> List[str]:
        idx = self._read_index()
        return list(idx.get("batches", []))

    def get_last_batch_id(self) -> Optional[str]:
        idx = self._read_index()
        return idx.get("last_batch_id")

    def get_last_batch(self) -> Optional[Batch]:
        bid = self.get_last_batch_id()
        if not bid:
            return None
        return self.get_batch(bid)

    def get_last_real_run_batch_id(self) -> Optional[str]:
        """获取最近一次真实 run 的批次 ID（跳过 dry-run）。"""
        for bid in self.list_batches():
            batch = self.get_batch(bid)
            if batch is not None and not batch.dry_run:
                return bid
        return None

    def get_last_real_run_batch(self) -> Optional[Batch]:
        """获取最近一次真实 run 的批次（跳过 dry-run）。"""
        bid = self.get_last_real_run_batch_id()
        if not bid:
            return None
        return self.get_batch(bid)

    def is_batch_completed(self, batch_id: str) -> bool:
        batch = self.get_batch(batch_id)
        return batch is not None and batch.status == "completed"

    def is_batch_rolled_back(self, batch_id: str) -> bool:
        batch = self.get_batch(batch_id)
        return batch is not None and batch.status == "rolled_back"

    def delete_batch(self, batch_id: str) -> None:
        f = self._batch_file(batch_id)
        if f.exists():
            f.unlink()
        idx = self._read_index()
        if batch_id in idx["batches"]:
            idx["batches"].remove(batch_id)
        if idx.get("last_batch_id") == batch_id:
            idx["last_batch_id"] = idx["batches"][0] if idx["batches"] else None
        self._write_index(idx)

    def _audit_file(self, audit_id: str) -> Path:
        return self.audits_dir / f"{audit_id}.json"

    def save_audit(self, audit_result: Any) -> None:
        """保存审计结果到持久化存储"""
        f = self._audit_file(audit_result.audit_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(audit_result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        idx = self._read_index()
        audits = idx.setdefault("audits", {})
        batch_audits = audits.setdefault(audit_result.batch_id, [])
        if audit_result.audit_id not in batch_audits:
            batch_audits.insert(0, audit_result.audit_id)
        idx["last_audit_id"] = audit_result.audit_id
        self._write_index(idx)

    def get_audit(self, audit_id: str) -> Optional[Any]:
        """读取指定审计 ID 的结果"""
        from .auditor import AuditResult
        f = self._audit_file(audit_id)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return AuditResult.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None

    def list_audits_for_batch(self, batch_id: str) -> List[str]:
        """列出指定批次的所有审计 ID（从新到旧）"""
        idx = self._read_index()
        audits = idx.get("audits", {})
        return list(audits.get(batch_id, []))

    def get_last_audit_for_batch(self, batch_id: str) -> Optional[Any]:
        """获取指定批次的最近一次审计结果"""
        ids = self.list_audits_for_batch(batch_id)
        if not ids:
            return None
        return self.get_audit(ids[0])

    def get_last_audit(self) -> Optional[Any]:
        """获取全局最近一次审计结果"""
        idx = self._read_index()
        aid = idx.get("last_audit_id")
        if not aid:
            return None
        return self.get_audit(aid)


@dataclass
class ConfigTemplate:
    """配置模板：保存命名的 YAML 配置快照"""
    name: str
    created_at: str
    updated_at: str
    config: Dict[str, Any]
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config": self.config,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConfigTemplate":
        return cls(
            name=data["name"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            config=data.get("config", {}),
            description=data.get("description", ""),
        )


class TemplateStore:
    """模板持久化存储：保存在 state_dir/templates 下"""

    REQUIRED_CONFIG_FIELDS = (
        "source_dir", "archive_dir", "csv_path", "state_dir",
        "action", "target_pattern",
    )

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.templates_dir = self.state_dir / "templates"
        self.index_file = self.templates_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.templates_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self._write_index({"templates": []})

    def _read_index(self) -> Dict[str, Any]:
        try:
            raw = self.index_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            data = {"templates": []}
            try:
                self._write_index(data)
            except Exception:
                pass
        data.setdefault("templates", [])
        return data

    def _write_index(self, data: Dict[str, Any]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _template_file(self, name: str) -> Path:
        safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return self.templates_dir / f"{safe_name}.json"

    def exists(self, name: str) -> bool:
        tpl_file = self._template_file(name)
        return tpl_file.exists()

    def save_template(
        self,
        name: str,
        config: Dict[str, Any],
        description: str = "",
        force: bool = False,
    ) -> ConfigTemplate:
        """保存模板。同名模板默认拒绝覆盖，force=True 时强制覆盖。"""
        if self.exists(name) and not force:
            raise TemplateError(
                f"模板 '{name}' 已存在。使用 --force 强制覆盖。"
            )

        now = datetime.now().isoformat()
        existing = self.get_template(name) if self.exists(name) else None
        created_at = existing.created_at if existing else now

        tpl = ConfigTemplate(
            name=name,
            created_at=created_at,
            updated_at=now,
            config=dict(config),
            description=description,
        )

        tpl_file = self._template_file(name)
        tpl_file.parent.mkdir(parents=True, exist_ok=True)
        tpl_file.write_text(
            json.dumps(tpl.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        idx = self._read_index()
        templates = idx["templates"]
        if name not in templates:
            templates.insert(0, name)
        idx["templates"] = templates
        self._write_index(idx)

        return tpl

    def get_template(self, name: str) -> Optional[ConfigTemplate]:
        """读取指定名称的模板。模板损坏（JSON 错误/字段缺失）返回 None 但记录备份。"""
        tpl_file = self._template_file(name)
        if not tpl_file.exists():
            return None
        try:
            data = json.loads(tpl_file.read_text(encoding="utf-8"))
            return ConfigTemplate.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            try:
                backup = tpl_file.read_bytes()
                (self.templates_dir / f"{name}.corrupted.bak").write_bytes(backup)
            except Exception:
                pass
            return None

    def list_templates(self) -> List[str]:
        """按创建/更新时间倒序列出所有模板名称"""
        idx = self._read_index()
        return list(idx.get("templates", []))

    def delete_template(self, name: str) -> None:
        tpl_file = self._template_file(name)
        if tpl_file.exists():
            tpl_file.unlink()
        idx = self._read_index()
        if name in idx["templates"]:
            idx["templates"].remove(name)
        self._write_index(idx)

    @classmethod
    def validate_template_config(cls, config: Dict[str, Any]) -> List[str]:
        """校验模板配置字段是否完整、合法。返回错误列表。"""
        errors = []
        if not isinstance(config, dict):
            errors.append("配置必须是字典类型")
            return errors

        for field in cls.REQUIRED_CONFIG_FIELDS:
            if field not in config:
                errors.append(f"缺少必填字段: {field}")

        action = config.get("action")
        if action is not None and action not in ("copy", "move"):
            errors.append(f"action 必须是 'copy' 或 'move'，当前为: {action}")

        return errors


class TemplateError(Exception):
    pass


class DoctorStore:
    """体检记录持久化存储：保存在 state_dir/doctors 下"""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.doctors_dir = self.state_dir / "doctors"
        self.index_file = self.doctors_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.doctors_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self._write_index({"doctors": [], "last_doctor_id": None})

    def _read_index(self) -> Dict[str, Any]:
        try:
            raw = self.index_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            data = {"doctors": [], "last_doctor_id": None}
            try:
                self._write_index(data)
            except Exception:
                pass
        data.setdefault("doctors", [])
        data.setdefault("last_doctor_id", None)
        return data

    def _write_index(self, data: Dict[str, Any]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _doctor_file(self, doctor_id: str) -> Path:
        return self.doctors_dir / f"{doctor_id}.json"

    def _resolve_unique_id(self, base_id: str) -> str:
        """处理同一秒多次运行的记录名冲突，追加后缀直到唯一"""
        if not self._doctor_file(base_id).exists():
            return base_id
        suffix = 1
        while True:
            candidate = f"{base_id}_{suffix}"
            if not self._doctor_file(candidate).exists():
                return candidate
            suffix += 1

    def save_doctor(self, result: Any) -> Any:
        """保存体检结果。处理同一秒多次运行的记录名冲突"""
        from .doctor import DoctorResult
        original_id = result.doctor_id
        unique_id = self._resolve_unique_id(original_id)
        if unique_id != original_id:
            result.doctor_id = unique_id

        f = self._doctor_file(result.doctor_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        idx = self._read_index()
        if result.doctor_id not in idx["doctors"]:
            idx["doctors"].insert(0, result.doctor_id)
        idx["last_doctor_id"] = result.doctor_id
        self._write_index(idx)
        return result

    def get_doctor(self, doctor_id: str) -> Optional[Any]:
        """读取指定体检 ID 的结果"""
        from .doctor import DoctorResult
        f = self._doctor_file(doctor_id)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return DoctorResult.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None

    def list_doctors(self) -> List[str]:
        """按时间倒序列出所有体检 ID"""
        idx = self._read_index()
        return list(idx.get("doctors", []))

    def get_last_doctor_id(self) -> Optional[str]:
        """获取最近一次体检 ID"""
        idx = self._read_index()
        return idx.get("last_doctor_id")

    def get_last_doctor(self) -> Optional[Any]:
        """获取最近一次体检结果"""
        did = self.get_last_doctor_id()
        if not did:
            return None
        return self.get_doctor(did)


@dataclass
class HandoverFileEntry:
    """交接包中的单个文件条目"""
    idx: int
    source: str
    archive_path: str
    relative_path: str
    file_size: int
    sha256: str
    action: str = ""
    source_signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandoverFileEntry":
        return cls(
            idx=data["idx"],
            source=data["source"],
            archive_path=data["archive_path"],
            relative_path=data["relative_path"],
            file_size=data.get("file_size", 0),
            sha256=data["sha256"],
            action=data.get("action", ""),
            source_signature=data.get("source_signature", ""),
        )


@dataclass
class HandoverRecord:
    """交接包记录"""
    handover_id: str
    created_at: str
    batch_id: str
    output_dir: str
    config_summary: Dict[str, Any]
    files: List[HandoverFileEntry]
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handover_id": self.handover_id,
            "created_at": self.created_at,
            "batch_id": self.batch_id,
            "output_dir": self.output_dir,
            "config_summary": self.config_summary,
            "files": [f.to_dict() for f in self.files],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HandoverRecord":
        return cls(
            handover_id=data["handover_id"],
            created_at=data["created_at"],
            batch_id=data["batch_id"],
            output_dir=data["output_dir"],
            config_summary=data.get("config_summary", {}),
            files=[HandoverFileEntry.from_dict(f) for f in data.get("files", [])],
            notes=data.get("notes", ""),
        )


class HandoverError(Exception):
    pass


class HandoverStore:
    """交接包记录持久化存储：保存在 state_dir/handovers 下"""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.handovers_dir = self.state_dir / "handovers"
        self.index_file = self.handovers_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.handovers_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self._write_index({"handovers": [], "last_handover_id": None})

    def _read_index(self) -> Dict[str, Any]:
        try:
            raw = self.index_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            data = {"handovers": [], "last_handover_id": None}
            try:
                self._write_index(data)
            except Exception:
                pass
        data.setdefault("handovers", [])
        data.setdefault("last_handover_id", None)
        return data

    def _write_index(self, data: Dict[str, Any]) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _handover_file(self, handover_id: str) -> Path:
        safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in handover_id)
        return self.handovers_dir / f"{safe_id}.json"

    def save_handover(self, record: HandoverRecord) -> HandoverRecord:
        """保存交接包记录"""
        f = self._handover_file(record.handover_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        idx = self._read_index()
        if record.handover_id not in idx["handovers"]:
            idx["handovers"].insert(0, record.handover_id)
        idx["last_handover_id"] = record.handover_id
        self._write_index(idx)
        return record

    def get_handover(self, handover_id: str) -> Optional[HandoverRecord]:
        """读取指定交接包记录"""
        f = self._handover_file(handover_id)
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return HandoverRecord.from_dict(data)
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None

    def list_handovers(self) -> List[str]:
        """按时间倒序列出所有交接包 ID"""
        idx = self._read_index()
        return list(idx.get("handovers", []))

    def get_last_handover_id(self) -> Optional[str]:
        """获取最近一次交接包 ID"""
        idx = self._read_index()
        return idx.get("last_handover_id")

    def get_last_handover(self) -> Optional[HandoverRecord]:
        """获取最近一次交接包记录"""
        hid = self.get_last_handover_id()
        if not hid:
            return None
        return self.get_handover(hid)
