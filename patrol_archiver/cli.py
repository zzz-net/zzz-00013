"""CLI 入口"""
import io
import os
import sys
from pathlib import Path

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
from .executor import Executor, ExecutorError
from .planner import generate_plan
from .rollback import (
    RollbackError,
    detect_format,
    export_audit_report,
    export_report,
    format_audit_summary,
    format_batch_list,
    format_batch_summary,
    rollback_batch,
)
from .storage import StateStore
from .auditor import Auditor, AuditResult


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
        click.echo(f"使用最近一次批次: {batch_id}")

    batch = store.get_batch(batch_id)
    if batch is None:
        click.echo(f"错误: 批次不存在: {batch_id}", err=True)
        sys.exit(1)

    if batch.dry_run:
        click.echo("提示: DRY-RUN 批次未执行实际文件操作，审计仅校验配置一致性。", err=True)

    auditor = Auditor(store)
    try:
        result = auditor.audit(batch, cfg.source_dir, cfg.archive_dir)
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


if __name__ == "__main__":
    main()
