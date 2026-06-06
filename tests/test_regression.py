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
