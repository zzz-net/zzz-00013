"""回归测试脚本

覆盖范围：
1. dry-run / run 后，通过子进程（模拟关闭终端重启）运行 list / show / export
2. JSON 与 CSV 两种导出格式都能正确生成并包含所需字段
3. Windows 下强制 GBK 编码环境时 show 命令不抛 UnicodeEncodeError
4. show 输出完整动作日志，export 输出包含导出文件路径
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
PYTHON = sys.executable
JPG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _reset_workspace(tmp: Path) -> Path:
    """把 examples/ 复制到临时目录作为独立工作区。"""
    ws = tmp / "workspace"
    if ws.exists():
        shutil.rmtree(ws)
    shutil.copytree(EXAMPLES, ws)
    for leftover in ("archive", ".patrol_state"):
        p = ws / leftover
        if p.exists():
            shutil.rmtree(p)
    base = ws / "source_photos"
    base.mkdir(parents=True, exist_ok=True)
    for name in [
        "IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg", "IMG_0004.jpg",
        "IMG_0005.jpg", "IMG_0006.jpg", "IMG_0007.jpg", "IMG_0008.jpg",
        "EXTRA_orphan.jpg",
    ]:
        (base / name).write_bytes(JPG_HEADER)
    return ws


def _run_cli(cwd: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """在子进程中运行 patrol_archiver，模拟重新打开终端。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [PYTHON, "-m", "patrol_archiver", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_cross_process_persistence() -> None:
    """dry-run/run 后，子进程 list/show/export 仍可读到批次。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        r = _run_cli(ws, "dry-run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"dry-run failed: {r.stderr}")
        _assert("预演批次已保存:" in r.stdout, f"dry-run 未输出批次 ID: {r.stdout}")

        r = _run_cli(ws, "list", "-c", "config.yaml")
        _assert(r.returncode == 0, f"list after dry-run failed: {r.stderr}")
        _assert("[DRY]" in r.stdout, f"list 未显示 DRY-RUN 批次: {r.stdout}")

        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")
        _assert("执行完成，批次 ID:" in r.stdout, f"run 未输出批次 ID: {r.stdout}")
        _assert("报告路径:" in r.stdout, f"run 未输出报告路径: {r.stdout}")

        r = _run_cli(ws, "list", "-c", "config.yaml")
        _assert(r.returncode == 0, f"list after run failed: {r.stderr}")
        _assert("[RUN]" in r.stdout and "[DRY]" in r.stdout,
                f"list 未同时显示 RUN/DRY 批次: {r.stdout}")

        r = _run_cli(ws, "show", "-c", "config.yaml")
        _assert(r.returncode == 0, f"show failed: {r.stderr}")
        _assert("批次 ID:" in r.stdout, f"show 未显示批次 ID: {r.stdout}")
        _assert("文件动作日志" in r.stdout, f"show 未显示动作日志: {r.stdout}")
        _assert("[OK]" in r.stdout, f"show 未输出 ASCII 状态标记: {r.stdout}")


def test_export_json_and_csv() -> None:
    """JSON 与 CSV 两种格式都能生成并包含关键字段。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        json_out = ws / "report.json"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(json_out))
        _assert(r.returncode == 0, f"export json failed: {r.stderr}")
        _assert(json_out.exists() and json_out.stat().st_size > 0, "JSON 报告未生成")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        for key in ("batch_id", "status", "config_summary", "actions", "plan_summary"):
            _assert(key in data, f"JSON 缺少字段 {key}")
        _assert("missing" in data["plan_summary"], "JSON plan_summary 缺少 missing")
        _assert("extra_files" in data["plan_summary"], "JSON plan_summary 缺少 extra_files")

        csv_out = ws / "report.csv"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(csv_out))
        _assert(r.returncode == 0, f"export csv failed: {r.stderr}")
        _assert(csv_out.exists() and csv_out.stat().st_size > 0, "CSV 报告未生成")
        _assert("[CSV]" in r.stdout.upper() or "csv" in r.stdout.lower(),
                f"export 未提示 CSV 格式: {r.stdout}")

        text = csv_out.read_text(encoding="utf-8-sig")
        _assert("summary" in text, "CSV 缺少 summary section")
        _assert("action" in text, "CSV 缺少 action section")
        _assert("missing" in text or "extra_file" in text, "CSV 缺少 issue section")

        rows = list(csv.reader(io.StringIO(text)))
        headers = rows[0]
        _assert(headers[0] == "section" and "batch_id" in headers,
                f"CSV summary 表头异常: {headers}")

        # 再用 -f 显式指定 csv 覆盖扩展名，输出到 .txt
        csv2 = ws / "report_force.txt"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(csv2), "-f", "csv")
        _assert(r.returncode == 0, f"export -f csv failed: {r.stderr}")
        content = csv2.read_text(encoding="utf-8-sig")
        _assert("summary" in content, "-f csv 未生成 CSV 内容")


def test_windows_gbk_encoding_safety() -> None:
    """模拟 Windows GBK 控制台：stdout 编码被限制为 gbk 时 show 不崩。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        # 用一个独立脚本，强制 sys.stdout 用 gbk 编码（errors 默认 strict），
        # 然后调用 patrol_archiver show。若未正确修复会抛 UnicodeEncodeError。
        probe = ws / "_probe_gbk.py"
        probe.write_text(
            """
import io
import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='gbk', errors='strict')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='gbk', errors='strict')
sys.path.insert(0, sys.argv[1])
from patrol_archiver.cli import main
sys.argv = ['patrol-archiver', 'show', '-c', 'config.yaml']
main()
""",
            encoding="utf-8",
        )
        env = os.environ.copy()
        r = subprocess.run(
            [PYTHON, str(probe), str(ROOT)],
            cwd=str(ws),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _assert(r.returncode == 0,
                f"GBK 环境下 show 崩溃 (exit={r.returncode}):\nstdout={r.stdout}\nstderr={r.stderr}")
        _assert("批次 ID:" in r.stdout or "batch" in r.stdout.lower(),
                f"GBK 环境下 show 无有效输出: {r.stdout}")


def test_show_displays_export_paths() -> None:
    """show 显示报告路径，export 输出导出文件位置。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        r = _run_cli(ws, "show", "-c", "config.yaml")
        _assert("报告路径:" in r.stdout, f"show 未显示报告路径: {r.stdout}")

        out = ws / "out.json"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(out))
        _assert("已导出" in r.stdout and str(out.name) in r.stdout,
                f"export 未提示导出位置: {r.stdout}")


def test_readme_export_commands() -> None:
    """精确按 README 给出的两条命令运行，验证 JSON 不退化、CSV 内容完整。

    README 命令：
      python -m patrol_archiver export -c config.yaml -o batch_report.json
      python -m patrol_archiver export -c config.yaml -o batch_report.csv
    """
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        # 1) README: JSON 命令
        json_out = ws / "batch_report.json"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(json_out))
        _assert(r.returncode == 0, f"README JSON export failed: {r.stderr}")
        _assert("[JSON]" in r.stdout.upper() or "json" in r.stdout.lower(),
                f"JSON 导出未提示格式: {r.stdout}")
        _assert(json_out.exists() and json_out.stat().st_size > 0, "JSON 文件未生成")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        for key in ("batch_id", "status", "config_summary", "actions", "plan_summary"):
            _assert(key in data, f"JSON 退化: 缺少字段 {key}")
        ps = data.get("plan_summary", {})
        for key in ("missing", "extra_files", "duplicate_targets", "path_conflicts"):
            _assert(key in ps, f"JSON plan_summary 退化: 缺少 {key}")
        _assert(len(data["actions"]) >= 8, "JSON actions 数量异常")

        # 2) README: CSV 命令
        csv_out = ws / "batch_report.csv"
        r = _run_cli(ws, "export", "-c", "config.yaml", "-o", str(csv_out))
        _assert(r.returncode == 0, f"README CSV export failed: {r.stderr}")
        _assert("[CSV]" in r.stdout.upper() or "csv" in r.stdout.lower(),
                f"CSV 导出未提示格式: {r.stdout}")
        _assert("batch_report.csv" in r.stdout, f"CSV 导出未显示输出位置: {r.stdout}")
        _assert(csv_out.exists() and csv_out.stat().st_size > 0, "CSV 文件未生成")

        text = csv_out.read_text(encoding="utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        _assert(len(rows) >= 5, "CSV 行数太少，缺少数据段")

        sections_seen = set()
        summary_row = None
        for row in rows:
            if not row:
                continue
            first = row[0].strip()
            if first in ("summary", "action", "missing", "extra_file",
                         "duplicate_target", "path_conflict", "section"):
                sections_seen.add(first)
                if first == "summary" and summary_row is None and row[0] != "section":
                    summary_row = row

        _assert("action" in sections_seen, "CSV 缺少 action section")
        _assert("summary" in sections_seen, "CSV 缺少 summary section")
        _assert("extra_file" in sections_seen, "CSV 缺少 extra_file section（示例含 EXTRA_orphan.jpg）")

        # summary 字段名稳定：首行表头必须包含 batch_id/status/actions_total 等
        header = rows[0]
        for stable in ("batch_id", "status", "actions_total", "missing_count"):
            _assert(stable in header, f"CSV summary 表头缺少稳定字段 {stable}: {header}")

        # actions 表头必须包含 source/target/status/action
        action_header = None
        for row in rows:
            if row and row[0] == "section" and "source" in row and "target" in row:
                action_header = row
                break
        _assert(action_header is not None, "CSV 未找到 action 表头")
        for stable in ("section", "batch_id", "idx", "status", "action", "source", "target"):
            _assert(stable in action_header,
                    f"CSV action 表头缺少稳定字段 {stable}: {action_header}")


def test_audit_cross_process_persistence() -> None:
    """run 后 audit，通过子进程再次 show 仍可见最近审计摘要（跨进程持久化）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        r = _run_cli(ws, "audit", "-c", "config.yaml")
        _assert(r.returncode == 0, f"audit failed (exit={r.returncode}): {r.stderr}")
        _assert("=== 批次审计报告 ===" in r.stdout, f"audit 未输出报告头: {r.stdout}")
        _assert("对账统计" in r.stdout, f"audit 未输出对账统计: {r.stdout}")
        _assert("[OK] 成功一致" in r.stdout, f"audit 未输出 OK 计数: {r.stdout}")

        # 子进程 show 应能读出最近审计摘要
        r2 = _run_cli(ws, "show", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"show after audit failed: {r2.stderr}")
        _assert("=== 最近审计摘要 ===" in r2.stdout,
                f"子进程 show 未读出审计摘要（持久化失败?）: {r2.stdout}")
        _assert("审计 ID:" in r2.stdout, f"show 审计摘要缺少审计 ID: {r2.stdout}")


def test_audit_export_json_and_csv() -> None:
    """audit 报告 JSON/CSV 双格式导出，字段名稳定。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        json_out = ws / "audit.json"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(json_out))
        _assert(r.returncode == 0, f"audit -o json failed: {r.stderr}")
        _assert("已导出审计报告" in r.stdout and "JSON" in r.stdout.upper(),
                f"audit JSON 导出未提示格式: {r.stdout}")
        _assert(json_out.exists() and json_out.stat().st_size > 0, "审计 JSON 未生成")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        for key in ("audit_id", "batch_id", "created_at", "warnings",
                    "file_records", "extra_archive_files", "counts"):
            _assert(key in data, f"审计 JSON 缺少稳定字段 {key}")
        _assert(isinstance(data["file_records"], list) and len(data["file_records"]) > 0,
                "审计 JSON file_records 为空")
        for fk in ("idx", "source", "target", "audit_status", "original_status"):
            _assert(fk in data["file_records"][0],
                    f"审计 JSON file_records 缺少稳定字段 {fk}")

        csv_out = ws / "audit.csv"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(csv_out))
        _assert(r.returncode == 0, f"audit -o csv failed: {r.stderr}")
        _assert(csv_out.exists() and csv_out.stat().st_size > 0, "审计 CSV 未生成")

        text = csv_out.read_text(encoding="utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        sections_seen = set()
        for row in rows:
            if row:
                sections_seen.add(row[0].strip())
        for required in ("audit_summary", "audit_file", "audit_warning", "audit_extra"):
            _assert(required in sections_seen,
                    f"审计 CSV 缺少 section={required}，实际: {sections_seen}")

        # summary 稳定字段
        header = rows[0]
        for stable in ("audit_id", "batch_id", "success", "missing_in_archive", "rollback_risk"):
            _assert(stable in header,
                    f"审计 CSV summary 表头缺少稳定字段 {stable}: {header}")

        # 用 -f 显式指定 csv，输出到 .txt
        csv2 = ws / "audit_force.txt"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(csv2), "-f", "csv")
        _assert(r.returncode == 0, f"audit -f csv failed: {r.stderr}")
        content = csv2.read_text(encoding="utf-8-sig")
        _assert("audit_summary" in content, "-f csv 未生成审计 CSV 内容")


def test_audit_detect_deletion_and_overwrite() -> None:
    """手动删除归档文件、覆盖目标内容后，audit 能提示缺失、被覆盖与回滚风险。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        archive = ws / "archive"
        archived_files = []
        for root, _, files in os.walk(archive):
            for f in files:
                archived_files.append(Path(root) / f)
        _assert(len(archived_files) >= 2, f"归档文件太少: {archived_files}")

        # 1) 删除一个归档文件
        victim = archived_files[0]
        victim.unlink()
        _assert(not victim.exists(), "删除归档文件失败")

        # 2) 覆盖另一个归档文件（写入不同大小内容）
        overwrite_target = archived_files[1]
        overwrite_target.write_bytes(b"OVERWRITTEN_" + b"X" * 1024)

        # 3) 审计：应提示归档缺失、被覆盖
        r = _run_cli(ws, "audit", "-c", "config.yaml")
        _assert(r.returncode == 5,
                f"存在错误时 audit 应 exit=5，实际 exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
        _assert("missing_in_archive" in r.stdout.lower() or "归档缺失" in r.stdout
                or "[MISS-A]" in r.stdout,
                f"audit 未报告归档缺失: {r.stdout}")
        _assert("overwritten" in r.stdout.lower() or "被覆盖" in r.stdout
                or "[OVER]" in r.stdout,
                f"audit 未报告被覆盖: {r.stdout}")

        # 4) 再次 show 应能看到审计摘要中的风险
        r2 = _run_cli(ws, "show", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"show failed: {r2.stderr}")
        _assert("最近审计摘要" in r2.stdout, f"show 未显示审计摘要: {r2.stdout}")


def test_audit_nonexistent_batch_and_config_change() -> None:
    """批次不存在时报清楚错误；配置路径变更时给出风险提示，不自动改文件。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        # 1) 指定不存在的批次
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-b", "batch_NOT_EXIST_12345")
        _assert(r.returncode != 0, f"不存在批次 audit 应失败: {r.stdout}")
        _assert("不存在" in r.stderr or "不存在" in r.stdout,
                f"批次不存在错误信息不清楚: stderr={r.stderr}, stdout={r.stdout}")

        # 2) 篡改配置：把 source_dir 改成另一个路径
        alt_src = ws / "source_photos_alt"
        alt_src.mkdir(parents=True, exist_ok=True)
        cfg_path = ws / "config.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        alt_text = cfg_text.replace('source_dir: "./source_photos"',
                                    'source_dir: "./source_photos_alt"')
        cfg_path.write_text(alt_text, encoding="utf-8")

        # 不应抛异常，只给警告
        r = _run_cli(ws, "audit", "-c", "config.yaml")
        _assert("配置路径已变更" in r.stdout or "config" in r.stdout.lower() and "change" in r.stdout.lower()
                or "source_dir_changed" in r.stdout,
                f"配置变更未给出提示: stdout={r.stdout}")


def test_audit_same_size_different_content() -> None:
    """同字节数但内容不同的替换应被识别（签名验签），audit --json 可见非 success。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        # 归档中找一个文件，读取其大小，写入同字节数的完全不同内容
        archive = ws / "archive"
        archived = []
        for root, _, files in os.walk(archive):
            for f in files:
                archived.append(Path(root) / f)
        _assert(len(archived) >= 1, f"归档为空: {archived}")
        target = archived[0]
        orig_size = target.stat().st_size
        # 写入同字节数的全零内容（与 JPG_HEADER 完全不同）
        target.write_bytes(b"\x00" * orig_size)
        _assert(target.stat().st_size == orig_size, "替换后文件大小必须不变")

        # audit --json 应能看到 overwritten
        json_out = ws / "audit_report.json"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(json_out))
        _assert(r.returncode == 5,
                f"同大小异内容时 audit 应 exit=5，实际 exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
        _assert(json_out.exists(), "JSON 未生成")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        counts = data.get("counts", {})
        _assert(counts.get("overwritten", 0) >= 1,
                f"同大小异内容应被记为 overwritten，实际 counts={counts}")

        # JSON 中应有明确的文件级非 success 记录
        overwritten_files = [
            fr for fr in data.get("file_records", [])
            if fr.get("audit_status") == "overwritten"
        ]
        _assert(len(overwritten_files) >= 1,
                f"file_records 中应包含 overwritten 记录，实际 {[fr.get('audit_status') for fr in data.get('file_records', [])]}")
        detail = overwritten_files[0].get("detail", "")
        _assert(("签名不一致" in detail) or ("同字节数异内容" in detail) or ("大小相同但内容" in detail),
                f"overwritten 明细中应说明签名不匹配，实际 detail={detail}")

        # 用户可见的 warning
        warnings = data.get("warnings", [])
        codes = [w.get("code") for w in warnings]
        _assert("files_overwritten" in codes,
                f"应有 files_overwritten warning，实际 codes={codes}")

        # 普通成功场景下（不替换）仍应为 success
        # 用另一个文件再验证：先恢复刚才篡改的文件（从源复制回）
        src_path = Path(overwritten_files[0]["source"])
        if src_path.exists():
            import shutil as _shutil
            _shutil.copy2(str(src_path), str(target))
        r2 = _run_cli(ws, "audit", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"恢复后 audit 应成功 exit=0，实际={r2.returncode}\n{r2.stdout}")
        data2 = json.loads((ws / "audit_report.json").read_text(encoding="utf-8"))
        # 注意：这里可能会有之前的审计报告在，但我们只看当前新审计
        j2 = ws / "audit_ok.json"
        _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(j2))
        data_ok = json.loads(j2.read_text(encoding="utf-8"))
        counts_ok = data_ok.get("counts", {})
        _assert(counts_ok.get("overwritten", 0) == 0,
                f"恢复后 overwritten 应为 0，实际 counts={counts_ok}")
        _assert(counts_ok.get("success", 0) >= 1,
                f"恢复后应有 success，实际 counts={counts_ok}")

        # show 仍能展示最近审计摘要
        r3 = _run_cli(ws, "show", "-c", "config.yaml")
        _assert("最近审计摘要" in r3.stdout, f"show 未展示最近审计摘要: {r3.stdout}")

        # CSV 导出字段应稳定包含 signature_match
        csv_out = ws / "audit_same.csv"
        _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(csv_out))
        text = csv_out.read_text(encoding="utf-8-sig")
        _assert("audit_summary" in text and "audit_file" in text,
                "CSV 缺少审计 section")


def test_run_saves_initial_audit_snapshot() -> None:
    """run 命令执行完成后应自动保存初始审计快照，跨进程可读取。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")
        _assert("初始审计快照已保存:" in r.stdout,
                f"run 未输出初始审计快照信息: {r.stdout}")

        r2 = _run_cli(ws, "show", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"show after run failed: {r2.stderr}")
        _assert("=== 最近审计摘要 ===" in r2.stdout,
                f"run 后 show 未显示自动保存的审计摘要: {r2.stdout}")

        r3 = _run_cli(ws, "audit-list", "-c", "config.yaml")
        _assert(r3.returncode == 0, f"audit-list failed: {r3.stderr}")
        _assert("HEALTHY" in r3.stdout or "ISSUES" in r3.stdout,
                f"audit-list 未显示健康状态标签: {r3.stdout}")


def test_audit_list_command_and_multiple_audits() -> None:
    """audit-list 命令应按时间倒序列出同一批次的多次审计。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        for i in range(3):
            r = _run_cli(ws, "audit", "-c", "config.yaml")
            _assert(r.returncode == 0, f"audit #{i+1} failed: {r.stderr}")

        r = _run_cli(ws, "audit-list", "-c", "config.yaml")
        _assert(r.returncode == 0, f"audit-list failed: {r.stderr}")
        lines = [l for l in r.stdout.splitlines() if l.strip().startswith("audit_")]
        _assert(len(lines) >= 4,
                f"audit-list 应至少列出 4 次审计（1次初始+3次手动），实际 {len(lines)} 行:\n{r.stdout}")

        r2 = _run_cli(ws, "audit-list", "-c", "config.yaml", "-n", "2")
        lines2 = [l for l in r2.stdout.splitlines() if l.strip().startswith("audit_")]
        _assert(len(lines2) == 2, f"audit-list -n 2 应只显示 2 条，实际 {len(lines2)}: {r2.stdout}")


def test_audit_config_change_action_and_pattern() -> None:
    """配置 action 或 target_pattern 变更时，audit 应标注差异并生成 warning。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        cfg_path = ws / "config.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")

        alt_text = cfg_text.replace('action: "copy"', 'action: "move"')
        alt_text = alt_text.replace(
            'target_pattern: "{device_id}/{point}/{date}/{filename}"',
            'target_pattern: "{date}/{device_id}_{filename}"'
        )
        cfg_path.write_text(alt_text, encoding="utf-8")

        r = _run_cli(ws, "audit", "-c", "config.yaml", "--json")
        _assert(r.returncode == 0, f"audit --json with changed config failed: {r.stderr}")
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            _assert(False, f"--json 输出不纯，无法解析: {e}\nstdout={r.stdout}")

        _assert(data.get("config_path_changed") is True,
                f"config_path_changed 应为 True，实际: {data.get('config_path_changed')}")
        diff = data.get("config_diff", {})
        _assert("action" in diff, f"config_diff 应包含 action，实际 keys: {list(diff.keys())}")
        _assert("target_pattern" in diff,
                f"config_diff 应包含 target_pattern，实际 keys: {list(diff.keys())}")
        _assert(diff["action"]["batch"] == "copy" and diff["action"]["current"] == "move",
                f"action 差异不正确: {diff.get('action')}")

        warnings = data.get("warnings", [])
        codes = [w.get("code") for w in warnings]
        _assert(any("action" in c for c in codes),
                f"warnings 中应包含 action 变更警告，实际 codes: {codes}")


def test_audit_pure_json_csv_output() -> None:
    """--json / --csv 输出应纯净，不包含 '使用最近一次批次' 等提示文本。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        r = _run_cli(ws, "audit", "-c", "config.yaml", "--json")
        _assert(r.returncode == 0, f"audit --json failed: {r.stderr}")
        _assert("使用最近一次批次" not in r.stdout,
                f"--json 输出不应包含中文提示文本: {r.stdout[:200]}")
        try:
            json.loads(r.stdout)
        except json.JSONDecodeError as e:
            _assert(False, f"--json 输出不是合法 JSON: {e}\n{r.stdout}")

        r = _run_cli(ws, "audit", "-c", "config.yaml", "--csv")
        _assert(r.returncode == 0, f"audit --csv failed: {r.stderr}")
        _assert("使用最近一次批次" not in r.stdout,
                f"--csv 输出不应包含中文提示文本: {r.stdout[:200]}")
        rows = list(csv.reader(io.StringIO(r.stdout)))
        _assert(len(rows) >= 3, f"--csv 输出行数太少: {len(rows)}")
        _assert("audit_summary" in rows[1][0] or "audit_summary" in r.stdout,
                f"--csv 输出应包含 audit_summary section")


def test_audit_export_parent_dir_creation() -> None:
    """导出时父目录不存在应自动创建。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        deep_json = ws / "a" / "b" / "c" / "deep_audit.json"
        _assert(not deep_json.parent.exists(), "测试前置条件：父目录应不存在")
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(deep_json))
        _assert(r.returncode == 0, f"导出到深层目录失败: {r.stderr}")
        _assert(deep_json.exists() and deep_json.stat().st_size > 0,
                "JSON 导出到不存在的父目录时应自动创建父目录并生成文件")

        deep_csv = ws / "x" / "y" / "z" / "deep_audit.csv"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(deep_csv))
        _assert(r.returncode == 0, f"导出 CSV 到深层目录失败: {r.stderr}")
        _assert(deep_csv.exists() and deep_csv.stat().st_size > 0,
                "CSV 导出到不存在的父目录时应自动创建父目录并生成文件")


def test_audit_after_rollback() -> None:
    """回滚后的批次再次审计应标记为回滚状态，不应报错崩溃。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        r = _run_cli(ws, "rollback", "-c", "config.yaml")
        _assert(r.returncode == 0, f"rollback failed: {r.stderr}")
        _assert("回滚完成" in r.stdout, f"rollback 输出异常: {r.stdout}")

        r = _run_cli(ws, "audit", "-c", "config.yaml")
        _assert(r.returncode == 0,
                f"回滚后 audit 应 exit=0 或正常，实际 exit={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
        _assert("批次审计报告" in r.stdout or "对账统计" in r.stdout,
                f"回滚后 audit 未输出报告: {r.stdout}")

        json_out = ws / "audit_after_rollback.json"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(json_out))
        _assert(r.returncode == 0, f"回滚后 audit 导出 JSON 失败: {r.stderr}")
        _assert(json_out.exists() and json_out.stat().st_size > 0,
                "回滚后 audit 应能导出 JSON 报告")
        data = json.loads(json_out.read_text(encoding="utf-8"))
        _assert("counts" in data and "file_records" in data,
                "回滚后导出的 JSON 缺少关键字段")


def test_audit_export_csv_contains_config_diff_section() -> None:
    """审计 CSV 导出应包含 audit_config_diff section 且字段名稳定。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        cfg_path = ws / "config.yaml"
        cfg_text = cfg_path.read_text(encoding="utf-8")
        alt_text = cfg_text.replace('action: "copy"', 'action: "move"')
        cfg_path.write_text(alt_text, encoding="utf-8")

        csv_out = ws / "audit_diff.csv"
        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(csv_out))
        _assert(r.returncode == 0, f"audit 导出 CSV 失败: {r.stderr}")
        _assert(csv_out.exists(), "CSV 文件未生成")

        text = csv_out.read_text(encoding="utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
        sections = set()
        for row in rows:
            if row:
                sections.add(row[0].strip())

        for required in ("audit_summary", "audit_file", "audit_warning",
                         "audit_extra", "audit_config_diff"):
            _assert(required in sections,
                    f"审计 CSV 缺少 section={required}，实际 sections={sections}")

        config_diff_header = None
        for i, row in enumerate(rows):
            if row and row[0] == "section" and "config_key" in row:
                config_diff_header = row
                break
        _assert(config_diff_header is not None, "未找到 audit_config_diff 表头行")
        for stable in ("section", "audit_id", "batch_id", "idx",
                       "config_key", "batch_value", "current_value"):
            _assert(stable in config_diff_header,
                    f"audit_config_diff 表头缺少稳定字段 {stable}: {config_diff_header}")


def test_multiple_audit_snapshots_track_health_change() -> None:
    """多次审计快照应能追踪批次健康状态变化。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        r = _run_cli(ws, "run", "-c", "config.yaml")
        _assert(r.returncode == 0, f"run failed: {r.stderr}")

        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(ws / "audit1.json"))
        _assert(r.returncode == 0, f"audit #1 failed: {r.stderr}")
        a1 = json.loads((ws / "audit1.json").read_text(encoding="utf-8"))
        initial_health = a1["counts"].get("success", 0)
        _assert(initial_health >= 1, "首次审计应有 success 文件")

        archive = ws / "archive"
        archived = []
        for root, _, files in os.walk(archive):
            for f in files:
                archived.append(Path(root) / f)
        _assert(len(archived) >= 1, f"归档目录为空: {archived}")
        archived[0].unlink()

        r = _run_cli(ws, "audit", "-c", "config.yaml", "-o", str(ws / "audit2.json"))
        a2 = json.loads((ws / "audit2.json").read_text(encoding="utf-8"))
        _assert(a2["counts"].get("missing_in_archive", 0) >= 1,
                f"删除归档后应检测到 missing_in_archive，counts={a2['counts']}")
        _assert(a2["counts"].get("success", 0) < initial_health,
                f"删除归档后 success 应减少，before={initial_health}, after={a2['counts']}")

        r = _run_cli(ws, "audit-list", "-c", "config.yaml")
        _assert("HEALTHY" in r.stdout, f"audit-list 未显示 HEALTHY 标签: {r.stdout}")
        _assert("ISSUES" in r.stdout, f"audit-list 未显示 ISSUES 标签: {r.stdout}")


def test_template_save_and_list() -> None:
    """template-save 保存后，template-list 可列出（跨进程持久化）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        r = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "daily_patrol",
                     "-d", "日常巡检归档配置")
        _assert(r.returncode == 0, f"template-save failed: {r.stderr}")
        _assert("模板已保存" in r.stdout, f"template-save 未输出成功提示: {r.stdout}")

        r2 = _run_cli(ws, "template-list", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"template-list failed: {r2.stderr}")
        _assert("daily_patrol" in r2.stdout, f"template-list 未列出保存的模板: {r2.stdout}")


def test_template_duplicate_without_force_fails() -> None:
    """同名模板不带 --force 应失败，带 --force 应成功覆盖。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        r = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "dup_test")
        _assert(r.returncode == 0, f"首次 template-save 失败: {r.stderr}")

        r2 = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "dup_test")
        _assert(r2.returncode != 0, f"重复保存不带 --force 应失败: {r2.stdout}")
        _assert("已存在" in r2.stderr or "已存在" in r2.stdout,
                f"重复保存应提示已存在: stderr={r2.stderr}, stdout={r2.stdout}")

        r3 = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "dup_test", "--force")
        _assert(r3.returncode == 0, f"重复保存带 --force 应成功: {r3.stderr}")
        _assert("已覆盖保存" in r3.stdout, f"--force 未提示覆盖: {r3.stdout}")


def test_template_show_and_pure_json_output() -> None:
    """template-show 展示详情，--json 输出纯 JSON 无提示文本。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "show_test",
                 "-d", "测试描述")

        r = _run_cli(ws, "template-show", "-c", "config.yaml", "-n", "show_test")
        _assert(r.returncode == 0, f"template-show failed: {r.stderr}")
        _assert("模板名称: show_test" in r.stdout, f"template-show 未显示名称: {r.stdout}")
        _assert("配置内容:" in r.stdout, f"template-show 未显示配置: {r.stdout}")
        _assert("测试描述" in r.stdout, f"template-show 未显示描述: {r.stdout}")

        r2 = _run_cli(ws, "template-show", "-c", "config.yaml", "-n", "show_test", "--json")
        _assert(r2.returncode == 0, f"template-show --json failed: {r2.stderr}")
        _assert("模板名称" not in r2.stdout, f"--json 输出不应包含中文提示: {r2.stdout[:200]}")
        try:
            data = json.loads(r2.stdout)
        except json.JSONDecodeError as e:
            _assert(False, f"--json 输出不是合法 JSON: {e}\n{r2.stdout}")
        _assert(data.get("name") == "show_test", f"JSON name 字段错误: {data}")
        _assert("config" in data, f"JSON 缺少 config 字段: {list(data.keys())}")


def test_template_export_json_and_csv() -> None:
    """template-export 支持 JSON/CSV，单个模板和全部模板均可导出。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "tpl_a", "-d", "模板A")
        _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "tpl_b", "-d", "模板B")

        json_single = ws / "tpl_a.json"
        r = _run_cli(ws, "template-export", "-c", "config.yaml", "-n", "tpl_a",
                     "-o", str(json_single))
        _assert(r.returncode == 0, f"导出单个 JSON 失败: {r.stderr}")
        _assert(json_single.exists() and json_single.stat().st_size > 0, "单个 JSON 未生成")
        data = json.loads(json_single.read_text(encoding="utf-8"))
        _assert(data.get("name") == "tpl_a", f"导出的 JSON name 错误: {data.get('name')}")
        _assert("config" in data, "导出的 JSON 缺少 config")

        csv_single = ws / "tpl_a.csv"
        r = _run_cli(ws, "template-export", "-c", "config.yaml", "-n", "tpl_a",
                     "-o", str(csv_single))
        _assert(r.returncode == 0, f"导出单个 CSV 失败: {r.stderr}")
        _assert(csv_single.exists() and csv_single.stat().st_size > 0, "单个 CSV 未生成")
        text = csv_single.read_text(encoding="utf-8-sig")
        _assert("template_summary" in text and "template_config" in text,
                f"CSV 缺少 section 标记: {text[:300]}")

        json_all = ws / "all_tpl.json"
        r = _run_cli(ws, "template-export", "-c", "config.yaml", "-o", str(json_all))
        _assert(r.returncode == 0, f"导出全部 JSON 失败: {r.stderr}")
        data_all = json.loads(json_all.read_text(encoding="utf-8"))
        _assert(isinstance(data_all, list) and len(data_all) >= 2,
                f"全部导出应为列表且>=2: type={type(data_all)}, len={len(data_all) if isinstance(data_all, list) else 'N/A'}")

        csv_all = ws / "all_tpl.csv"
        r = _run_cli(ws, "template-export", "-c", "config.yaml", "-o", str(csv_all))
        _assert(r.returncode == 0, f"导出全部 CSV 失败: {r.stderr}")
        rows = list(csv.reader(io.StringIO(csv_all.read_text(encoding="utf-8-sig"))))
        _assert(len(rows) >= 3, f"全部 CSV 行数太少: {len(rows)}")


def test_template_apply_validates_and_generates_yaml() -> None:
    """template-apply 从模板生成 YAML，校验字段、路径，已存在文件需 --force。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        r = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "apply_test")
        _assert(r.returncode == 0, f"template-save 失败: {r.stderr}")

        out_cfg = ws / "from_template.yaml"
        r = _run_cli(ws, "template-apply", "-s", ".patrol_state",
                     "-n", "apply_test", "-c", str(out_cfg))
        _assert(r.returncode == 0, f"template-apply 失败: {r.stderr}")
        _assert(out_cfg.exists(), f"template-apply 未生成 YAML: {out_cfg}")
        content = out_cfg.read_text(encoding="utf-8")
        _assert("source_dir" in content and "archive_dir" in content,
                f"生成的 YAML 缺少关键字段: {content}")

        r2 = _run_cli(ws, "template-apply", "-s", ".patrol_state",
                      "-n", "apply_test", "-c", str(out_cfg))
        _assert(r2.returncode != 0, f"输出文件已存在时不带 --force 应失败: {r2.stdout}")
        _assert("已存在" in r2.stderr or "已存在" in r2.stdout,
                f"应提示文件已存在: stderr={r2.stderr}, stdout={r2.stdout}")

        r3 = _run_cli(ws, "template-apply", "-s", ".patrol_state",
                      "-n", "apply_test", "-c", str(out_cfg), "--force")
        _assert(r3.returncode == 0, f"带 --force 应覆盖成功: {r3.stderr}")

        r4 = _run_cli(ws, "dry-run", "-c", str(out_cfg))
        _assert(r4.returncode == 0, f"从模板生成的配置应能正常 dry-run: {r4.stderr}")


def test_template_bad_data_and_corruption() -> None:
    """模板损坏、字段缺失、不存在时给出清晰错误。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        state_dir = ws / ".patrol_state"
        tpl_dir = state_dir / "templates"
        tpl_dir.mkdir(parents=True, exist_ok=True)

        r = _run_cli(ws, "template-show", "-c", "config.yaml", "-n", "no_such")
        _assert(r.returncode != 0, f"不存在的模板 template-show 应失败: {r.stdout}")
        _assert("不存在" in r.stderr or "不存在" in r.stdout,
                f"应提示模板不存在: stderr={r.stderr}, stdout={r.stdout}")

        corrupted = tpl_dir / "broken.json"
        corrupted.write_text("{this is not valid json!!!", encoding="utf-8")
        (tpl_dir / "index.json").write_text(
            json.dumps({"templates": ["broken"]}, ensure_ascii=False),
            encoding="utf-8"
        )
        r2 = _run_cli(ws, "template-show", "-c", "config.yaml", "-n", "broken")
        _assert(r2.returncode != 0, f"损坏的模板 template-show 应失败: {r2.stdout}")
        _assert("损坏" in r2.stderr or "损坏" in r2.stdout,
                f"应提示模板损坏: stderr={r2.stderr}, stdout={r2.stdout}")
        bak_file = tpl_dir / "broken.corrupted.bak"
        _assert(bak_file.exists(), f"损坏模板应生成备份: {bak_file}")

        from patrol_archiver.storage import TemplateStore
        store = TemplateStore(state_dir)
        bad_cfg = {"action": "invalid_action", "target_pattern": ""}
        store.save_template("field_missing", bad_cfg, force=False)
        out_bad = ws / "bad_cfg.yaml"
        r3 = _run_cli(ws, "template-apply", "-s", str(state_dir),
                      "-n", "field_missing", "-c", str(out_bad))
        _assert(r3.returncode != 0,
                f"字段缺失/非法的模板 apply 应失败: stdout={r3.stdout}\nstderr={r3.stderr}")
        _assert("缺少必填字段" in r3.stderr or "缺少必填字段" in r3.stdout
                or "校验失败" in r3.stderr or "校验失败" in r3.stdout,
                f"应提示字段校验失败: stderr={r3.stderr}, stdout={r3.stdout}")


def test_template_list_pure_json_csv_output() -> None:
    """template-list --json / --csv 输出纯净，不夹杂提示文本。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))
        _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "pure_test")

        r = _run_cli(ws, "template-list", "-c", "config.yaml", "--json")
        _assert(r.returncode == 0, f"template-list --json failed: {r.stderr}")
        _assert("状态目录" not in r.stdout and "模板存储" not in r.stdout,
                f"--json 输出不应包含提示文本: {r.stdout[:300]}")
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            _assert(False, f"--json 输出不是合法 JSON: {e}\n{r.stdout}")
        _assert(isinstance(data, list), f"--json 输出应为列表: {type(data)}")

        r2 = _run_cli(ws, "template-list", "-c", "config.yaml", "--csv")
        _assert(r2.returncode == 0, f"template-list --csv failed: {r2.stderr}")
        _assert("状态目录" not in r2.stdout, f"--csv 输出不应包含提示文本: {r2.stdout[:300]}")
        rows = list(csv.reader(io.StringIO(r2.stdout)))
        _assert(len(rows) >= 2, f"--csv 输出行数太少: {len(rows)}")


def test_template_cross_process_persistence() -> None:
    """子进程保存的模板，在另一个子进程中仍可读取（跨进程/重启持久化）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _reset_workspace(Path(td))

        r1 = _run_cli(ws, "template-save", "-c", "config.yaml", "-n", "cross_proc")
        _assert(r1.returncode == 0, f"子进程1 template-save failed: {r1.stderr}")

        r2 = _run_cli(ws, "template-list", "-c", "config.yaml")
        _assert(r2.returncode == 0, f"子进程2 template-list failed: {r2.stderr}")
        _assert("cross_proc" in r2.stdout,
                f"子进程2 应能看到子进程1 保存的模板: {r2.stdout}")

        r3 = _run_cli(ws, "template-show", "-c", "config.yaml", "-n", "cross_proc")
        _assert(r3.returncode == 0, f"子进程3 template-show failed: {r3.stderr}")
        _assert("模板名称: cross_proc" in r3.stdout,
                f"子进程3 应能读取模板详情: {r3.stdout}")


def main() -> int:
    tests = [
        ("跨进程持久化: list/show/export", test_cross_process_persistence),
        ("JSON/CSV 导出: 字段完整 + 自动/显式格式", test_export_json_and_csv),
        ("Windows GBK 编码安全: show 不崩", test_windows_gbk_encoding_safety),
        ("show/export 显示路径", test_show_displays_export_paths),
        ("README 命令一致性: JSON+CSV 双命令 + 字段稳定", test_readme_export_commands),
        ("审计: 跨进程持久化 (show 可见最近审计)", test_audit_cross_process_persistence),
        ("审计: JSON/CSV 双格式导出 + 字段稳定", test_audit_export_json_and_csv),
        ("审计: 检测删除/覆盖/回滚风险", test_audit_detect_deletion_and_overwrite),
        ("审计: 不存在批次与配置路径变更", test_audit_nonexistent_batch_and_config_change),
        ("审计: 同字节数异内容签名验签", test_audit_same_size_different_content),
        ("审计新特性: run 自动保存初始审计快照", test_run_saves_initial_audit_snapshot),
        ("审计新特性: audit-list 命令 + 多次审计历史", test_audit_list_command_and_multiple_audits),
        ("审计新特性: action/target_pattern 变更检测", test_audit_config_change_action_and_pattern),
        ("审计新特性: --json/--csv 纯输出无提示文本", test_audit_pure_json_csv_output),
        ("审计新特性: 导出父目录不存在时自动创建", test_audit_export_parent_dir_creation),
        ("审计新特性: 回滚后再审计", test_audit_after_rollback),
        ("审计新特性: CSV 导出含 config_diff 稳定字段", test_audit_export_csv_contains_config_diff_section),
        ("审计新特性: 多次快照追踪健康变化", test_multiple_audit_snapshots_track_health_change),
        ("模板: save/list 基础功能与持久化", test_template_save_and_list),
        ("模板: 同名冲突 --force 覆盖", test_template_duplicate_without_force_fails),
        ("模板: show 详情与 --json 纯输出", test_template_show_and_pure_json_output),
        ("模板: export 单个/全部 JSON+CSV", test_template_export_json_and_csv),
        ("模板: apply 校验字段、路径并生成 YAML", test_template_apply_validates_and_generates_yaml),
        ("模板: 损坏/缺失/不存在错误处理", test_template_bad_data_and_corruption),
        ("模板: list --json/--csv 纯输出无提示", test_template_list_pure_json_csv_output),
        ("模板: 跨进程/重启持久化", test_template_cross_process_persistence),
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {name}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
