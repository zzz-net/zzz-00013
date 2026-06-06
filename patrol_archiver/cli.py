"""CLI 入口"""
import io
import os
import sys
from pathlib import Path
from typing import List

import click

from . import __version__


def _configure_console_encoding() -> None:
    """确保 stdout/stderr 对 Unicode 安全：优先 UTF-8，失败时用 replace 兜底。

    Windows GBK/CP936 控制台输出非 ASCII 字符时默认会抛 UnicodeEncodeError。
    这里不吞异常，而是在流层用 errors='replace' 保证程序不崩；UTF-8 终端无影响。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if not isinstance(stream, io.TextIOBase):
            continue
        target_enc = "utf-8"
        try:
            stream.reconfigure(encoding=target_enc, errors="replace")
        except Exception:
            try:
                buffer = getattr(stream, "buffer", None)
                if buffer is None:
                    continue
                new_stream = io.TextIOWrapper(
                    buffer,
                    encoding=target_enc,
                    errors="replace",
                    line_buffering=getattr(stream, "line_buffering", True),
                )
                setattr(sys, stream_name, new_stream)
            except Exception:
                pass


_configure_console_encoding()


STATUS_MARKS = {
    "success": "[OK]",
    "failed": "[FAIL]",
    "pending": "[PEND]",
    "rolled_back": "[RB]",
}


from .config import ArchiverConfig, load_config
from .csv_parser import parse_patrol_csv
from .doctor import (
    DoctorResult,
    format_doctor_history,
    format_doctor_result,
    run_doctor_checks,
)
from .executor import Executor, ExecutorError
from .planner import generate_plan
from .rollback import (
    RollbackError,
    detect_format,
    export_audit_report,
    export_doctor_report,
    export_report,
    export_template_csv,
    export_template_json,
    export_templates_csv,
    export_templates_json,
    format_audit_history,
    format_audit_summary,
    format_batch_list,
    format_batch_summary,
    format_template_list,
    format_template_show,
    rollback_batch,
)
from .storage import DoctorStore, StateStore, TemplateStore, TemplateError, HandoverStore
from .auditor import Auditor, AuditResult
from .handover import (
    HandoverError,
    create_handover,
    format_handover_list,
    format_handover_show,
    format_verify_result,
    resolve_batch_for_handover,
    resolve_handover_record,
    verify_handover,
    verify_result_to_csv,
    verify_result_to_json,
)


def _resolve_config_path(config: str) -> Path:
    p = Path(config).resolve()
    if not p.exists():
        raise click.ClickException(f"配置文件不存在: {p}")
    return p


def _load_and_validate(config_path: str) -> ArchiverConfig:
    cfg = load_config(config_path)
    errors = cfg.validate()
    if errors:
        raise click.ClickException("配置校验失败:\n  - " + "\n  - ".join(errors))
    return cfg


def _print_plan(plan) -> None:
    click.echo("=== 归档计划预览 ===")
    click.echo(f"待处理文件: {len(plan.to_process)}")
    for fa in plan.to_process:
        click.echo(f"  [{fa.action.upper()}] {fa.source}")
        click.echo(f"       -> {fa.target}")

    click.echo("")
    click.echo(f"缺图 (清单中有但源目录没有): {len(plan.missing)}")
    for m in plan.missing:
        click.echo(f"  第{m.line_no}行: {m.device_id}/{m.point}/{m.date}/{m.photo_name}")

    click.echo("")
    click.echo(f"清单外文件 (源目录有但不在清单中): {len(plan.extra_files)}")
    for ef in plan.extra_files:
        click.echo(f"  {ef}")

    click.echo("")
    click.echo(f"重复目标名 (两行落到同一归档名): {len(plan.duplicate_targets)}")
    for tgt, items in plan.duplicate_targets.items():
        click.echo(f"  {tgt}")
        for r, p in items:
            click.echo(f"    - 第{r.line_no}行: {p}")

    click.echo("")
    click.echo(f"路径冲突: {len(plan.path_conflicts)}")
    for tgt, reason in plan.path_conflicts:
        click.echo(f"  {tgt}: {reason}")

    if plan.has_fatal_errors:
        click.echo("")
        click.echo("!!! 存在致命错误，run 将被拒绝 !!!")
        for msg in plan.fatal_error_messages():
            click.echo(f"  - {msg}")


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="patrol-archiver")
@click.pass_context
def main(ctx: click.Context) -> None:
    """巡检照片归档校验 CLI 工具"""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command("dry-run")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
def dry_run_cmd(config_path: str) -> None:
    """预演：输出归档计划但不执行任何文件操作"""
    try:
        cfg = _load_and_validate(config_path)
    except click.ClickException as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    try:
        records = parse_patrol_csv(cfg.csv_path, cfg.csv_columns)
    except Exception as e:
        click.echo(f"错误: 解析 CSV 失败 - {e}", err=True)
        sys.exit(1)

    plan = generate_plan(cfg, records)
    _print_plan(plan)

    store = StateStore(cfg.state_dir)
    executor = Executor(cfg, store)
    try:
        batch = executor.dry_run(plan)
        click.echo("")
        click.echo(f"预演批次已保存: {batch.batch_id}")
        click.echo(f"状态目录: {cfg.state_dir}")
    except Exception as e:
        click.echo(f"错误: 保存预演批次失败 - {e}", err=True)
        sys.exit(1)

    if plan.has_fatal_errors:
        sys.exit(2)


@main.command("run")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
def run_cmd(config_path: str) -> None:
    """执行归档操作"""
    try:
        cfg = _load_and_validate(config_path)
    except click.ClickException as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    try:
        records = parse_patrol_csv(cfg.csv_path, cfg.csv_columns)
    except Exception as e:
        click.echo(f"错误: 解析 CSV 失败 - {e}", err=True)
        sys.exit(1)

    plan = generate_plan(cfg, records)
    _print_plan(plan)

    store = StateStore(cfg.state_dir)

    last_batch = store.get_last_batch()
    if (last_batch and not last_batch.dry_run
            and last_batch.status == "completed"
            and str(cfg.source_dir) == last_batch.config_summary.get("source_dir")
            and str(cfg.csv_path) == last_batch.config_summary.get("csv_path")):
        click.echo("")
        click.echo(
            f"错误: 检测到已完成的相同批次 {last_batch.batch_id}，"
            "重复执行可能导致冲突。如需再次执行，请先 rollback。",
            err=True
        )
        sys.exit(3)

    executor = Executor(cfg, store)
    try:
        batch = executor.run(plan)
    except ExecutorError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(2)
    except Exception as e:
        click.echo(f"错误: 执行失败 - {e}", err=True)
        sys.exit(1)

    click.echo("")
    click.echo(f"执行完成，批次 ID: {batch.batch_id}")
    click.echo(f"报告路径: {batch.report_path}")
    click.echo(f"状态目录: {cfg.state_dir}")

    try:
        auditor = Auditor(store)
        audit_result = auditor.audit(
            batch,
            cfg.source_dir,
            cfg.archive_dir,
            current_action=cfg.action,
            current_target_pattern=cfg.target_pattern,
            current_csv_path=str(cfg.csv_path),
        )
        store.save_audit(audit_result)
        click.echo(f"初始审计快照已保存: {audit_result.audit_id}")
    except Exception as e:
        click.echo(f"警告: 初始审计快照保存失败 - {e}", err=True)


@main.command("rollback")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次")
def rollback_cmd(config_path: str, batch_id: str) -> None:
    """按批次回滚归档操作"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)

    if not batch_id:
        batch_id = store.get_last_batch_id()
        if not batch_id:
            click.echo("错误: 未找到任何批次记录", err=True)
            sys.exit(1)
        click.echo(f"使用最近一次批次: {batch_id}")

    try:
        batch = rollback_batch(store, batch_id)
    except RollbackError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(4)
    except Exception as e:
        click.echo(f"错误: 回滚失败 - {e}", err=True)
        sys.exit(1)

    click.echo(f"回滚完成，批次: {batch.batch_id}")
    click.echo(f"共回滚 {len([a for a in batch.actions if a.status == 'rolled_back'])} 个文件")


@main.command("list")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-n", "--limit", type=int, default=20, help="显示最近 N 条")
def list_cmd(config_path: str, limit: int) -> None:
    """列出批次历史（关闭终端后仍可查看）"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)
    ids = store.list_batches()[:limit]
    batches = [b for b in (store.get_batch(i) for i in ids) if b is not None]
    click.echo(f"状态目录: {cfg.state_dir}")
    click.echo(f"共 {len(ids)} 个批次（显示最近 {len(batches)} 个）:")
    click.echo("")
    click.echo(format_batch_list(batches))


@main.command("show")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次")
def show_cmd(config_path: str, batch_id: str) -> None:
    """显示批次详情、配置摘要和日志"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)

    if not batch_id:
        batch_id = store.get_last_batch_id()
        if not batch_id:
            click.echo("错误: 未找到任何批次记录", err=True)
            sys.exit(1)

    batch = store.get_batch(batch_id)
    if batch is None:
        click.echo(f"错误: 批次不存在: {batch_id}", err=True)
        sys.exit(1)

    click.echo(format_batch_summary(batch))

    if batch.actions:
        click.echo("")
        click.echo("=== 文件动作日志 ===")
        for a in batch.actions:
            mark = STATUS_MARKS.get(a.status, "[?]")
            click.echo(f"  {mark} [{a.status:10s}] {a.action.upper():4s}  {a.source}")
            click.echo(f"           -> {a.target}")
            if a.error:
                click.echo(f"           错误: {a.error}")

    last_audit = store.get_last_audit_for_batch(batch.batch_id)
    if last_audit is not None:
        click.echo("")
        click.echo("=== 最近审计摘要 ===")
        click.echo(format_audit_summary(last_audit))


@main.command("export")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次")
@click.option("-o", "--output", "output_path", required=True, help="导出文件路径 (.json 或 .csv)")
@click.option(
    "-f", "--format", "fmt",
    type=click.Choice(["json", "csv", "auto"], case_sensitive=False),
    default="auto",
    help="导出格式：json / csv / auto（按扩展名自动识别，默认 auto）"
)
def export_cmd(config_path: str, batch_id: str, output_path: str, fmt: str) -> None:
    """导出批次报告（JSON 或 CSV）"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)

    if not batch_id:
        batch_id = store.get_last_batch_id()
        if not batch_id:
            click.echo("错误: 未找到任何批次记录", err=True)
            sys.exit(1)

    batch = store.get_batch(batch_id)
    if batch is None:
        click.echo(f"错误: 批次不存在: {batch_id}", err=True)
        sys.exit(1)

    resolved_fmt = None if fmt.lower() == "auto" else fmt.lower()
    actual_fmt = resolved_fmt or detect_format(Path(output_path))

    try:
        out = export_report(batch, Path(output_path), fmt=resolved_fmt)
    except Exception as e:
        click.echo(f"错误: 导出失败 - {e}", err=True)
        sys.exit(1)

    click.echo(f"已导出 [{actual_fmt.upper()}] 到: {out}")


@main.command("audit")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次")
@click.option("--json", "json_out", is_flag=True, default=False, help="以 JSON 格式输出到 stdout（不写文件）")
@click.option("--csv", "csv_out", is_flag=True, default=False, help="以 CSV 格式输出到 stdout（不写文件）")
@click.option("-o", "--output", "output_path", default=None, help="导出审计报告到文件 (.json 或 .csv)")
@click.option(
    "-f", "--format", "fmt",
    type=click.Choice(["json", "csv", "auto"], case_sensitive=False),
    default="auto",
    help="导出格式：json / csv / auto（按扩展名自动识别，默认 auto）"
)
def audit_cmd(config_path: str, batch_id: str, json_out: bool, csv_out: bool, output_path: str, fmt: str) -> None:
    """按批次对账审计：逐项比对状态记录与当前源目录、归档目录"""
    pure_output = json_out or csv_out

    try:
        cfg = _load_and_validate(config_path)
    except click.ClickException as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)

    if not batch_id:
        batch_id = store.get_last_batch_id()
        if not batch_id:
            click.echo("错误: 未找到任何批次记录", err=True)
            sys.exit(1)
        if not pure_output:
            click.echo(f"使用最近一次批次: {batch_id}")

    batch = store.get_batch(batch_id)
    if batch is None:
        click.echo(f"错误: 批次不存在: {batch_id}", err=True)
        sys.exit(1)

    if batch.dry_run and not pure_output:
        click.echo("提示: DRY-RUN 批次未执行实际文件操作，审计仅校验配置一致性。", err=True)

    auditor = Auditor(store)
    try:
        result = auditor.audit(
            batch,
            cfg.source_dir,
            cfg.archive_dir,
            current_action=cfg.action,
            current_target_pattern=cfg.target_pattern,
            current_csv_path=str(cfg.csv_path),
        )
    except Exception as e:
        click.echo(f"错误: 执行审计失败 - {e}", err=True)
        sys.exit(1)

    try:
        store.save_audit(result)
    except Exception as e:
        click.echo(f"警告: 保存审计记录失败 - {e}", err=True)

    has_errors = any(w.level in ("error", "risk") for w in result.warnings)

    if output_path:
        resolved_fmt = None if fmt.lower() == "auto" else fmt.lower()
        actual_fmt = resolved_fmt or detect_format(Path(output_path))
        try:
            out = export_audit_report(result, Path(output_path), fmt=resolved_fmt)
            click.echo(f"已导出审计报告 [{actual_fmt.upper()}] 到: {out}")
        except Exception as e:
            click.echo(f"错误: 导出审计报告失败 - {e}", err=True)
            sys.exit(1)
    elif json_out or csv_out:
        requested_fmt = "json" if json_out else "csv"
        import io as _io
        if requested_fmt == "json":
            import json as _json
            click.echo(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            from .rollback import export_audit_csv
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8-sig") as tf:
                tmp_path = Path(tf.name)
            try:
                export_audit_csv(result, tmp_path)
                click.echo(tmp_path.read_text(encoding="utf-8-sig"))
            finally:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
    else:
        click.echo("=== 批次审计报告 ===")
        click.echo(format_audit_summary(result))

        non_success = [r for r in result.file_records if r.audit_status != "success" and r.original_status in ("success", "rolled_back")]
        if non_success:
            click.echo("")
            click.echo("=== 异常明细（仅非 success） ===")
            for r in non_success:
                mark_map = {
                    "missing_in_archive": "[MISS-A]",
                    "missing_in_source": "[MISS-S]",
                    "overwritten": "[OVER]",
                    "tampered": "[TAMP]",
                    "rollback_risk": "[RISK]",
                }
                mark = mark_map.get(r.audit_status, "[?]")
                click.echo(f"  {mark} #{r.idx} [{r.audit_status}]")
                click.echo(f"         动作: {r.action}  原状态: {r.original_status}")
                click.echo(f"         源:   {r.source}  {'(存在)' if r.source_exists else '(缺失)'}")
                click.echo(f"         归档: {r.target}  {'(存在)' if r.archive_exists else '(缺失)'}")
                if r.detail:
                    click.echo(f"         说明: {r.detail}")

        if result.extra_archive_files:
            click.echo("")
            click.echo(f"=== 归档目录额外文件（共 {len(result.extra_archive_files)} 个） ===")
            for p in result.extra_archive_files[:20]:
                click.echo(f"  {p}")
            if len(result.extra_archive_files) > 20:
                click.echo(f"  ... 省略 {len(result.extra_archive_files) - 20} 个")

    if has_errors:
        sys.exit(5)


@main.command("audit-list")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次")
@click.option("-n", "--limit", type=int, default=20, help="显示最近 N 条审计记录")
def audit_list_cmd(config_path: str, batch_id: str, limit: int) -> None:
    """列出指定批次的审计历史（按时间倒序）"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)

    if not batch_id:
        batch_id = store.get_last_batch_id()
        if not batch_id:
            click.echo("错误: 未找到任何批次记录", err=True)
            sys.exit(1)
        click.echo(f"使用最近一次批次: {batch_id}")

    batch = store.get_batch(batch_id)
    if batch is None:
        click.echo(f"错误: 批次不存在: {batch_id}", err=True)
        sys.exit(1)

    audit_ids = store.list_audits_for_batch(batch_id)[:limit]
    audits = [a for a in (store.get_audit(aid) for aid in audit_ids) if a is not None]

    click.echo(f"批次 ID: {batch_id}")
    click.echo(f"状态目录: {cfg.state_dir}")
    click.echo(f"共 {len(audit_ids)} 次审计（显示最近 {len(audits)} 个）:")
    click.echo("")
    click.echo(format_audit_history(audits))


def _config_to_dict(cfg: ArchiverConfig) -> dict:
    """把 ArchiverConfig 转为可序列化的 dict（路径转字符串）"""
    return {
        "source_dir": str(cfg.source_dir),
        "archive_dir": str(cfg.archive_dir),
        "csv_path": str(cfg.csv_path),
        "state_dir": str(cfg.state_dir),
        "photo_extensions": list(cfg.photo_extensions),
        "action": cfg.action,
        "target_pattern": cfg.target_pattern,
        "csv_columns": dict(cfg.csv_columns),
    }


def _validate_template_for_apply(template_config: dict) -> List[str]:
    """应用模板时的额外校验：目录、路径可写等"""
    errors = []

    source_dir = Path(template_config.get("source_dir", ""))
    archive_dir = Path(template_config.get("archive_dir", ""))
    csv_path = Path(template_config.get("csv_path", ""))
    state_dir = Path(template_config.get("state_dir", ""))

    if not source_dir.exists():
        errors.append(f"源目录不存在: {source_dir}")
    elif not source_dir.is_dir():
        errors.append(f"源路径不是目录: {source_dir}")

    if not archive_dir.parent.exists():
        errors.append(f"归档目录的父目录不存在: {archive_dir.parent}")
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        probe = archive_dir / ".write_probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except PermissionError:
        errors.append(f"归档目录没有写入权限: {archive_dir}")
    except Exception as e:
        errors.append(f"归档目录无法访问: {archive_dir} - {e}")

    if not csv_path.exists():
        errors.append(f"CSV 文件不存在: {csv_path}")

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        probe = state_dir / ".write_probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except PermissionError:
        errors.append(f"状态目录没有写入权限: {state_dir}")
    except Exception as e:
        errors.append(f"状态目录无法访问: {state_dir} - {e}")

    action = template_config.get("action")
    if action not in ("copy", "move"):
        errors.append(f"action 必须是 'copy' 或 'move'，当前为: {action}")

    target_pattern = template_config.get("target_pattern", "")
    if not isinstance(target_pattern, str) or not target_pattern:
        errors.append("target_pattern 必须是非空字符串")
    else:
        required_placeholders = ["{device_id}", "{point}", "{date}", "{filename}"]
        for ph in required_placeholders:
            if ph not in target_pattern:
                errors.append(
                    f"target_pattern 缺少必要占位符 {ph}，当前为: {target_pattern}"
                )
                break

    return errors


@main.command("template-save")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-n", "--name", required=True, help="模板名称")
@click.option("-d", "--description", default="", help="模板描述")
@click.option("--force", is_flag=True, default=False, help="同名模板强制覆盖")
def template_save_cmd(config_path: str, name: str, description: str, force: bool) -> None:
    """把当前 YAML 配置保存为命名模板"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = TemplateStore(cfg.state_dir)
    config_dict = _config_to_dict(cfg)

    try:
        tpl = store.save_template(name, config_dict, description=description, force=force)
    except TemplateError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(6)

    if force:
        click.echo(f"模板已覆盖保存: {tpl.name}")
    else:
        click.echo(f"模板已保存: {tpl.name}")
    click.echo(f"存储位置: {store.templates_dir}")


@main.command("template-list")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径（用于读取 state_dir）")
@click.option("--json", "json_out", is_flag=True, default=False, help="以 JSON 格式输出到 stdout")
@click.option("--csv", "csv_out", is_flag=True, default=False, help="以 CSV 格式输出到 stdout")
def template_list_cmd(config_path: str, json_out: bool, csv_out: bool) -> None:
    """列出所有已保存的配置模板"""
    pure_output = json_out or csv_out

    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = TemplateStore(cfg.state_dir)
    names = store.list_templates()
    templates = [t for t in (store.get_template(n) for n in names) if t is not None]

    if not pure_output:
        click.echo(f"状态目录: {cfg.state_dir}")
        click.echo(f"模板存储: {store.templates_dir}")
        click.echo(f"共 {len(templates)} 个模板:")
        click.echo("")

    if json_out:
        import json as _json
        click.echo(_json.dumps([t.to_dict() for t in templates], ensure_ascii=False, indent=2))
    elif csv_out:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8-sig") as tf:
            tmp_path = Path(tf.name)
        try:
            export_templates_csv(templates, tmp_path)
            click.echo(tmp_path.read_text(encoding="utf-8-sig"))
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    else:
        click.echo(format_template_list(templates))


@main.command("template-show")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径（用于读取 state_dir）")
@click.option("-n", "--name", required=True, help="模板名称")
@click.option("--json", "json_out", is_flag=True, default=False, help="以 JSON 格式输出到 stdout")
def template_show_cmd(config_path: str, name: str, json_out: bool) -> None:
    """查看单个模板的详情"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = TemplateStore(cfg.state_dir)
    tpl = store.get_template(name)

    if tpl is None:
        if store.exists(name):
            click.echo(f"错误: 模板 '{name}' 已损坏，无法读取。"
                       f" 备份已保存到 {store.templates_dir / (name + '.corrupted.bak')}", err=True)
        else:
            click.echo(f"错误: 模板不存在: {name}", err=True)
        sys.exit(7)

    if json_out:
        import json as _json
        click.echo(_json.dumps(tpl.to_dict(), ensure_ascii=False, indent=2))
    else:
        click.echo(format_template_show(tpl))


@main.command("template-export")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径（用于读取 state_dir）")
@click.option("-n", "--name", default=None, help="模板名称（不指定则导出全部）")
@click.option("-o", "--output", "output_path", required=True, help="导出文件路径 (.json 或 .csv)")
@click.option(
    "-f", "--format", "fmt",
    type=click.Choice(["json", "csv", "auto"], case_sensitive=False),
    default="auto",
    help="导出格式：json / csv / auto（按扩展名自动识别，默认 auto）"
)
def template_export_cmd(config_path: str, name: str, output_path: str, fmt: str) -> None:
    """导出模板为 JSON 或 CSV"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = TemplateStore(cfg.state_dir)

    resolved_fmt = None if fmt.lower() == "auto" else fmt.lower()
    actual_fmt = resolved_fmt or detect_format(Path(output_path))

    if name:
        tpl = store.get_template(name)
        if tpl is None:
            if store.exists(name):
                click.echo(f"错误: 模板 '{name}' 已损坏，无法读取。", err=True)
            else:
                click.echo(f"错误: 模板不存在: {name}", err=True)
            sys.exit(7)

        try:
            if actual_fmt == "csv":
                out = export_template_csv(tpl, Path(output_path))
            else:
                out = export_template_json(tpl, Path(output_path))
        except Exception as e:
            click.echo(f"错误: 导出失败 - {e}", err=True)
            sys.exit(1)
    else:
        names = store.list_templates()
        templates = [t for t in (store.get_template(n) for n in names) if t is not None]
        try:
            if actual_fmt == "csv":
                out = export_templates_csv(templates, Path(output_path))
            else:
                out = export_templates_json(templates, Path(output_path))
        except Exception as e:
            click.echo(f"错误: 导出失败 - {e}", err=True)
            sys.exit(1)

    click.echo(f"已导出模板 [{actual_fmt.upper()}] 到: {out}")


@main.command("template-apply")
@click.option("-c", "--config", "config_path", required=True, help="输出 YAML 配置文件路径")
@click.option("-s", "--state-dir", "state_dir_path", required=True, help="状态目录路径（模板存储位置）")
@click.option("-n", "--name", required=True, help="模板名称")
@click.option("--force", is_flag=True, default=False, help="输出文件已存在时强制覆盖")
def template_apply_cmd(config_path: str, state_dir_path: str, name: str, force: bool) -> None:
    """从模板生成 YAML 配置文件（校验字段和路径）"""
    state_dir = Path(state_dir_path).resolve()
    if not state_dir.exists():
        click.echo(f"错误: 状态目录不存在: {state_dir}", err=True)
        sys.exit(1)

    store = TemplateStore(state_dir)
    tpl = store.get_template(name)

    if tpl is None:
        if store.exists(name):
            click.echo(f"错误: 模板 '{name}' 已损坏，无法读取。"
                       f" 备份已保存到 {store.templates_dir / (name + '.corrupted.bak')}", err=True)
        else:
            click.echo(f"错误: 模板不存在: {name}", err=True)
        sys.exit(7)

    template_config = tpl.config or {}

    field_errors = TemplateStore.validate_template_config(template_config)
    if field_errors:
        click.echo("错误: 模板字段校验失败:", err=True)
        for e in field_errors:
            click.echo(f"  - {e}", err=True)
        sys.exit(8)

    path_errors = _validate_template_for_apply(template_config)
    if path_errors:
        click.echo("警告: 模板路径存在问题（文件仍将生成，但后续 run 可能失败）:", err=True)
        for e in path_errors:
            click.echo(f"  - {e}", err=True)

    out_path = Path(config_path).resolve()
    if out_path.exists() and not force:
        click.echo(f"错误: 输出文件已存在: {out_path}。使用 --force 强制覆盖。", err=True)
        sys.exit(6)

    try:
        out_parent = out_path.parent
        out_parent.mkdir(parents=True, exist_ok=True)
        probe = out_parent / ".write_probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except PermissionError:
        click.echo(f"错误: 输出目录没有写入权限: {out_parent}", err=True)
        sys.exit(9)
    except Exception as e:
        click.echo(f"错误: 无法写入输出目录: {out_parent} - {e}", err=True)
        sys.exit(9)

    import yaml as _yaml
    out_config = {}
    for key in ("source_dir", "archive_dir", "csv_path", "state_dir",
                "photo_extensions", "action", "target_pattern", "csv_columns"):
        if key in template_config:
            out_config[key] = template_config[key]

    try:
        with open(out_path, "w", encoding="utf-8") as f:
            _yaml.dump(out_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception as e:
        click.echo(f"错误: 写入配置文件失败 - {e}", err=True)
        sys.exit(1)

    click.echo(f"已从模板 '{name}' 生成配置: {out_path}")
    if path_errors:
        click.echo("注意: 上述路径问题需手动修复后再执行 run/dry-run。", err=True)


@main.command("doctor")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("--json", "json_out", is_flag=True, default=False, help="以 JSON 格式输出到 stdout（不写文件）")
@click.option("--csv", "csv_out", is_flag=True, default=False, help="以 CSV 格式输出到 stdout（不写文件）")
def doctor_cmd(config_path: str, json_out: bool, csv_out: bool) -> None:
    """配置体检：检查 YAML 和巡检 CSV 是否可用（dry-run/run 前推荐执行）"""
    pure_output = json_out or csv_out

    try:
        cfg_path_obj = _resolve_config_path(config_path)
    except click.ClickException as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    result = run_doctor_checks(cfg, config_path)

    try:
        store = DoctorStore(cfg.state_dir)
        result = store.save_doctor(result)
    except Exception as e:
        if not pure_output:
            click.echo(f"警告: 保存体检记录失败 - {e}", err=True)

    if json_out:
        import json as _json
        click.echo(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    elif csv_out:
        import tempfile
        from .rollback import export_doctor_csv
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8-sig") as tf:
            tmp_path = Path(tf.name)
        try:
            export_doctor_csv(result, tmp_path)
            click.echo(tmp_path.read_text(encoding="utf-8-sig"))
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
    else:
        click.echo(format_doctor_result(result))
        try:
            click.echo(f"\n体检记录已保存到: {cfg.state_dir / 'doctors'}")
        except Exception:
            pass

    if result.has_errors:
        sys.exit(10)


@main.command("doctor-history")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径（用于读取 state_dir）")
@click.option("-n", "--limit", type=int, default=20, help="显示最近 N 条")
def doctor_history_cmd(config_path: str, limit: int) -> None:
    """列出体检历史记录（跨重启可查）"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = DoctorStore(cfg.state_dir)
    ids = store.list_doctors()[:limit]
    results = [r for r in (store.get_doctor(i) for i in ids) if r is not None]
    click.echo(f"状态目录: {cfg.state_dir}")
    click.echo(f"体检记录存储: {store.doctors_dir}")
    click.echo(f"共 {len(ids)} 次体检（显示最近 {len(results)} 次）:")
    click.echo("")
    click.echo(format_doctor_history(results))


@main.command("doctor-export")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径（用于读取 state_dir）")
@click.option("-d", "--doctor", "doctor_id", default=None, help="体检 ID，不指定则使用最近一次")
@click.option("-o", "--output", "output_path", default=None, help="导出文件路径 (.json 或 .csv)")
@click.option(
    "-f", "--format", "fmt",
    type=click.Choice(["json", "csv", "auto"], case_sensitive=False),
    default="auto",
    help="导出格式：json / csv / auto（按扩展名自动识别，默认 auto）"
)
@click.option("--json", "json_stdout", is_flag=True, default=False, help="以 JSON 格式输出到 stdout（不写文件，纯输出）")
@click.option("--csv", "csv_stdout", is_flag=True, default=False, help="以 CSV 格式输出到 stdout（不写文件，纯输出）")
def doctor_export_cmd(
    config_path: str, doctor_id: str, output_path: str, fmt: str,
    json_stdout: bool, csv_stdout: bool
) -> None:
    """导出体检记录为 JSON 或 CSV"""
    pure_output = json_stdout or csv_stdout

    if not pure_output and not output_path:
        raise click.UsageError("缺少 '-o/--output'，或使用 '--json' / '--csv' 输出到 stdout")

    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = DoctorStore(cfg.state_dir)

    if not doctor_id:
        doctor_id = store.get_last_doctor_id()
        if not doctor_id:
            click.echo("错误: 未找到任何体检记录", err=True)
            sys.exit(1)

    result = store.get_doctor(doctor_id)
    if result is None:
        click.echo(f"错误: 体检记录不存在: {doctor_id}", err=True)
        sys.exit(1)

    if json_stdout:
        import json as _json
        click.echo(_json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return
    if csv_stdout:
        import tempfile
        from .rollback import export_doctor_csv
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8-sig") as tf:
            tmp_path = Path(tf.name)
        try:
            export_doctor_csv(result, tmp_path)
            click.echo(tmp_path.read_text(encoding="utf-8-sig"))
        finally:
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return

    resolved_fmt = None if fmt.lower() == "auto" else fmt.lower()
    actual_fmt = resolved_fmt or detect_format(Path(output_path))

    try:
        out = export_doctor_report(result, Path(output_path), fmt=resolved_fmt)
    except Exception as e:
        click.echo(f"错误: 导出失败 - {e}", err=True)
        sys.exit(1)

    click.echo(f"已导出体检报告 [{actual_fmt.upper()}] 到: {out}")


@main.command("handover-create")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-b", "--batch", "batch_id", default=None, help="批次 ID，不指定则使用最近一次真实 run 批次（跳过 dry-run）")
@click.option("-o", "--output", "output_dir", required=True, help="交接包输出目录（已存在时自动追加序号）")
def handover_create_cmd(config_path: str, batch_id: str, output_dir: str) -> None:
    """生成归档交接包（离线包，含 manifest、报告副本和 README）"""
    try:
        cfg = _load_and_validate(config_path)
    except click.ClickException as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(1)

    store = StateStore(cfg.state_dir)
    handover_store = HandoverStore(cfg.state_dir)

    try:
        batch = resolve_batch_for_handover(store, batch_id)
    except HandoverError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(11)

    if not batch_id:
        click.echo(f"使用最近一次真实 run 批次: {batch.batch_id}")

    try:
        record, actual_dir, renamed = create_handover(
            state_store=store,
            handover_store=handover_store,
            batch=batch,
            output_dir=Path(output_dir),
            archive_dir=cfg.archive_dir,
        )
    except HandoverError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(11)
    except Exception as e:
        click.echo(f"错误: 生成交接包失败 - {e}", err=True)
        sys.exit(1)

    if renamed:
        click.echo(f"提示: 输出目录已存在且非空，已自动改用: {actual_dir}")

    click.echo(f"交接包已生成: {actual_dir}")
    click.echo(f"交接包 ID: {record.handover_id}")
    click.echo(f"对应批次:   {record.batch_id}")
    click.echo(f"文件数量:   {len(record.files)}")
    click.echo("")
    click.echo("包内文件:")
    for name in ("manifest.json", "manifest.csv", "README.txt"):
        p = actual_dir / name
        if p.exists():
            click.echo(f"  {name}  ({p.stat().st_size} bytes)")
    report_files = sorted(actual_dir.glob("batch_report_*"))
    for rf in report_files:
        click.echo(f"  {rf.name}  ({rf.stat().st_size} bytes)")
    files_subdir = actual_dir / "files"
    if files_subdir.exists():
        photo_count = sum(1 for _ in files_subdir.rglob("*") if _.is_file())
        click.echo(f"  files/    ({photo_count} 个文件)")


@main.command("handover-list")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-n", "--limit", type=int, default=20, help="显示最近 N 条")
def handover_list_cmd(config_path: str, limit: int) -> None:
    """列出历史交接包记录（跨重启可查）"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = HandoverStore(cfg.state_dir)
    ids = store.list_handovers()[:limit]
    records = [r for r in (store.get_handover(i) for i in ids) if r is not None]

    click.echo(f"状态目录: {cfg.state_dir}")
    click.echo(f"交接包记录存储: {store.handovers_dir}")
    click.echo(f"共 {len(ids)} 个交接包（显示最近 {len(records)} 个）:")
    click.echo("")
    click.echo(format_handover_list(records))


@main.command("handover-show")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-d", "--handover", "handover_id", default=None, help="交接包 ID，不指定则使用最近一次")
def handover_show_cmd(config_path: str, handover_id: str) -> None:
    """查看交接包详情及包内文件列表"""
    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = HandoverStore(cfg.state_dir)

    try:
        record = resolve_handover_record(store, handover_id)
    except HandoverError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(11)

    if not handover_id:
        click.echo(f"使用最近一次交接包: {record.handover_id}")

    click.echo(format_handover_show(record))


@main.command("handover-verify")
@click.option("-c", "--config", "config_path", required=True, help="YAML 配置文件路径")
@click.option("-d", "--handover", "handover_id", default=None, help="交接包 ID，不指定则使用最近一次")
@click.option("--json", "json_out", is_flag=True, default=False, help="以 JSON 格式输出到 stdout（纯输出，不夹杂提示文本）")
@click.option("--csv", "csv_out", is_flag=True, default=False, help="以 CSV 格式输出到 stdout（纯输出，不夹杂提示文本）")
def handover_verify_cmd(config_path: str, handover_id: str, json_out: bool, csv_out: bool) -> None:
    """校验交接包：验证清单内文件、哈希、源/归档路径是否匹配"""
    pure_output = json_out or csv_out

    try:
        cfg = load_config(config_path)
    except Exception as e:
        click.echo(f"错误: 加载配置失败 - {e}", err=True)
        sys.exit(1)

    store = HandoverStore(cfg.state_dir)

    try:
        record = resolve_handover_record(store, handover_id)
    except HandoverError as e:
        click.echo(f"错误: {e}", err=True)
        sys.exit(11)

    if not handover_id and not pure_output:
        click.echo(f"使用最近一次交接包: {record.handover_id}")

    try:
        result = verify_handover(record)
    except Exception as e:
        click.echo(f"错误: 校验过程异常 - {e}", err=True)
        sys.exit(1)

    if json_out:
        click.echo(verify_result_to_json(result))
    elif csv_out:
        click.echo(verify_result_to_csv(result))
    else:
        click.echo(format_verify_result(result))

    if result.has_errors:
        sys.exit(result.exit_code)


if __name__ == "__main__":
    main()
