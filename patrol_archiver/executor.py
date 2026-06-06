"""执行器：执行 dry-run 或实际归档操作"""
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import List

from .config import ArchiverConfig
from .planner import ArchivePlan, FileAction
from .storage import Batch, FileActionRecord, StateStore


class ExecutorError(Exception):
    pass


class Executor:
    def __init__(self, cfg: ArchiverConfig, store: StateStore):
        self.cfg = cfg
        self.store = store

    def _config_summary(self) -> dict:
        return {
            "source_dir": str(self.cfg.source_dir),
            "archive_dir": str(self.cfg.archive_dir),
            "csv_path": str(self.cfg.csv_path),
            "action": self.cfg.action,
            "target_pattern": self.cfg.target_pattern,
        }

    def dry_run(self, plan: ArchivePlan) -> Batch:
        batch = self.store.create_batch(self._config_summary(), dry_run=True)
        batch.status = "completed"
        for fa in plan.to_process:
            batch.actions.append(FileActionRecord(
                source=str(fa.source),
                target=str(fa.target),
                action=fa.action,
                status="pending",
            ))
        self.store.save_batch(batch)
        return batch

    def run(self, plan: ArchivePlan) -> Batch:
        if plan.has_fatal_errors:
            errors = plan.fatal_error_messages()
            raise ExecutorError("存在致命错误，无法执行:\n  - " + "\n  - ".join(errors))

        batch = self.store.create_batch(self._config_summary(), dry_run=False)
        batch.status = "running"
        self.store.save_batch(batch)

        try:
            for fa in plan.to_process:
                rec = self._execute_action(fa)
                batch.actions.append(rec)

            report_path = self._generate_report(batch, plan)
            batch.report_path = str(report_path)
            batch.status = "completed"
        except Exception as e:
            batch.status = "failed"
            batch.error = str(e)
            self.store.save_batch(batch)
            raise

        self.store.save_batch(batch)
        return batch

    def _execute_action(self, fa: FileAction) -> FileActionRecord:
        try:
            fa.target.parent.mkdir(parents=True, exist_ok=True)
            if fa.target.exists():
                raise FileExistsError(f"目标已存在: {fa.target}")

            if fa.action == "copy":
                shutil.copy2(str(fa.source), str(fa.target))
            elif fa.action == "move":
                shutil.move(str(fa.source), str(fa.target))
            else:
                raise ValueError(f"未知动作: {fa.action}")

            return FileActionRecord(
                source=str(fa.source),
                target=str(fa.target),
                action=fa.action,
                status="success",
            )
        except Exception as e:
            return FileActionRecord(
                source=str(fa.source),
                target=str(fa.target),
                action=fa.action,
                status="failed",
                error=str(e),
            )

    def _generate_report(self, batch: Batch, plan: ArchivePlan) -> Path:
        report_dir = self.cfg.state_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{batch.batch_id}_report.txt"

        lines = []
        lines.append(f"批次报告: {batch.batch_id}")
        lines.append(f"生成时间: {datetime.now().isoformat()}")
        lines.append(f"执行模式: {'DRY-RUN' if batch.dry_run else 'RUN'}")
        lines.append(f"状态: {batch.status}")
        lines.append("")
        lines.append("=== 配置摘要 ===")
        for k, v in batch.config_summary.items():
            lines.append(f"  {k}: {v}")
        lines.append("")
        lines.append(f"=== 文件动作 (共 {len(batch.actions)} 个) ===")
        success = [a for a in batch.actions if a.status == "success"]
        failed = [a for a in batch.actions if a.status == "failed"]
        lines.append(f"  成功: {len(success)}")
        lines.append(f"  失败: {len(failed)}")
        for a in batch.actions:
            mark = "✓" if a.status == "success" else "✗"
            lines.append(f"  {mark} [{a.action.upper()}] {a.source} -> {a.target}")
            if a.error:
                lines.append(f"      错误: {a.error}")

        lines.append("")
        lines.append(f"=== 缺图 (共 {len(plan.missing)} 个) ===")
        for m in plan.missing:
            lines.append(f"  第{m.line_no}行: {m.device_id}/{m.point}/{m.date}/{m.photo_name}")

        lines.append("")
        lines.append(f"=== 清单外文件 (共 {len(plan.extra_files)} 个) ===")
        for ef in plan.extra_files:
            lines.append(f"  {ef}")

        lines.append("")
        lines.append(f"=== 重复目标名 (共 {len(plan.duplicate_targets)} 组) ===")
        for tgt, items in plan.duplicate_targets.items():
            lines.append(f"  {tgt}:")
            for r, p in items:
                lines.append(f"    - 第{r.line_no}行: {p}")

        lines.append("")
        lines.append(f"=== 路径冲突 (共 {len(plan.path_conflicts)} 个) ===")
        for tgt, reason in plan.path_conflicts:
            lines.append(f"  {tgt}: {reason}")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
