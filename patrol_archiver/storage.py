"""批次状态持久化存储"""
import json
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FileActionRecord:
    source: str
    target: str
    action: str
    status: str  # "success", "failed", "pending"
    error: Optional[str] = None


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
        self.index_file = state_dir / "index.json"
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_file.exists():
            self.index_file.write_text(
                json.dumps({"batches": [], "last_batch_id": None}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )

    def _read_index(self) -> Dict[str, Any]:
        return json.loads(self.index_file.read_text(encoding="utf-8"))

    def _write_index(self, data: Dict[str, Any]) -> None:
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
        self._batch_file(batch.batch_id).write_text(
            json.dumps(batch.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def get_batch(self, batch_id: str) -> Optional[Batch]:
        f = self._batch_file(batch_id)
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        return Batch.from_dict(data)

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
