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


def main() -> int:
    tests = [
        ("跨进程持久化: list/show/export", test_cross_process_persistence),
        ("JSON/CSV 导出: 字段完整 + 自动/显式格式", test_export_json_and_csv),
        ("Windows GBK 编码安全: show 不崩", test_windows_gbk_encoding_safety),
        ("show/export 显示路径", test_show_displays_export_paths),
        ("README 命令一致性: JSON+CSV 双命令 + 字段稳定", test_readme_export_commands),
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
