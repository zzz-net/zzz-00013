# 巡检照片归档校验 CLI

一个本地巡检照片归档校验的命令行工具。它读取配置和巡检 CSV，按设备编号、点位、日期把源目录照片生成归档计划；`dry-run` 预演、`run` 执行、`rollback` 按批次撤销，所有批次状态持久化，关闭终端后仍可查询。

## 功能特性

- **dry-run 预演**：输出待复制/移动、缺图、清单外文件、重复目标名、路径冲突
- **run 执行**：实际复制或移动文件，记录批次、每个文件动作和报告路径
- **rollback 回滚**：按批次撤销，检测目标是否被其他文件占用
- **持久化状态**：`list`/`show`/`export` 关闭终端后仍可查看历史批次、配置摘要和日志
- **严格错误处理**：源目录不存在、两行落到同一归档名、重复执行已完成批次、回滚目标被占用 → 必须失败并说明原因
- **配置体检 doctor**：`doctor` 在 dry-run/run 前检查 YAML 和 CSV，结果按 error/warn/info 分级，存在 error 时非 0 退出；历史持久化可查、可导出 JSON/CSV
- **归档交接包 handover**：巡检照片归档完成后，把某个批次整理成可交给外部人员核对的离线包（含 manifest.json/csv、批次报告副本和 README.txt）；历史持久化可查，支持校验包内文件哈希与源路径

## 安装

```bash
pip install -r requirements.txt
# 或
pip install -e .
```

安装后可使用 `patrol-archiver` 命令，也可以 `python -m patrol_archiver`。

## 目录结构示例

```
examples/
├── config.yaml              # 配置文件
├── patrol.csv               # 巡检清单（正常）
├── patrol_duplicate.csv     # 含重复目标行（错误演示）
├── config_bad_source.yaml   # 源目录不存在（错误演示）
└── source_photos/
    ├── IMG_0001.jpg
    ├── IMG_0002.jpg
    ├── ...
    └── EXTRA_orphan.jpg     # 清单外文件
```

## 配置文件 (config.yaml)

```yaml
source_dir: "./source_photos"   # 源照片目录
archive_dir: "./archive"        # 归档目标目录
csv_path: "./patrol.csv"        # 巡检 CSV
state_dir: "./.patrol_state"    # 批次状态存储目录（持久化）
action: "copy"                  # copy 或 move
target_pattern: "{device_id}/{point}/{date}/{filename}"
photo_extensions: [".jpg", ".jpeg", ".png", ".bmp", ".gif"]
csv_columns:
  device_id: "设备编号"
  point: "点位"
  date: "日期"
  photo_name: "照片名"
```

## 巡检 CSV

| 设备编号 | 点位   | 日期       | 照片名     |
|----------|--------|------------|------------|
| DEV-A01  | 配电室 | 2026-06-01 | IMG_0001.jpg |
| ...      | ...    | ...        | ...        |

---

## 完整复现步骤

### 0. 准备环境

```bash
cd examples
pip install -r ../requirements.txt
```

### 1. dry-run 预演

```bash
python -m patrol_archiver dry-run -c config.yaml
```

你会看到：
- 待处理的 8 个文件及其目标路径
- 缺图列表（如有）
- 清单外文件 `EXTRA_orphan.jpg`
- 无重复目标名和路径冲突

退出码 `0` 表示预演成功无致命错误。预演批次会被持久化到 `./.patrol_state`。

### 2. run 执行归档

```bash
python -m patrol_archiver run -c config.yaml
```

执行完成后：
- `archive/` 目录下会生成按 `设备编号/点位/日期/` 层级组织的照片
- `./.patrol_state/batches/` 保存批次 JSON
- `./.patrol_state/reports/` 生成文本报告

### 3. 查看批次

```bash
# 列出所有批次（关闭终端后依然可见）
python -m patrol_archiver list -c config.yaml

# 查看最近批次详情（含配置摘要和动作日志）
python -m patrol_archiver show -c config.yaml
```

### 4. 导出报告

按扩展名自动识别导出格式，JSON 和 CSV 两条命令都可以直接运行：

```bash
# 导出 JSON（包含完整 Batch 结构，含 plan_summary 所有字段）
python -m patrol_archiver export -c config.yaml -o batch_report.json

# 导出 CSV（Excel/Numbers 可直接打开，UTF-8-BOM）
python -m patrol_archiver export -c config.yaml -o batch_report.csv

# 也可以显式指定格式（覆盖扩展名推断）
python -m patrol_archiver export -c config.yaml -o batch.txt -f csv
python -m patrol_archiver export -c config.yaml -o batch.txt -f json
```

执行后 CLI 会提示实际格式和输出位置，例如：`已导出 [CSV] 到: .../batch_report.csv`。

**CSV 报告包含的内容**（用 `section` 字段分区，表头字段名稳定）：

| section 值 | 含义 | 主要字段 |
|------------|------|----------|
| `summary` | 批次摘要（1 行） | batch_id、created_at、status、mode、action、source_dir、archive_dir、csv_path、target_pattern、actions_total、actions_success、actions_failed、actions_pending、actions_rolled_back、missing_count、extra_files_count、duplicate_targets_count、path_conflicts_count、report_path、error |
| `action` | 每条文件动作 | idx、status、action、source、target、error |
| `missing` | 缺图：清单有但源目录没有 | idx、line_no、device_id、point、date、photo_name |
| `extra_file` | 清单外文件：源目录有但不在清单 | idx、source |
| `duplicate_target` | 重复目标名：两行 CSV 落到同一归档名 | idx、line_no、device_id、point、date、photo_name、source、target |
| `path_conflict` | 路径冲突：目标文件已存在或其他冲突 | idx、target、reason |

**JSON 报告**：完整 `Batch` 对象序列化，字段与 CLI 内部一致，含 `config_summary`、`actions`、`plan_summary` 等。

### 5. rollback 回滚

```bash
# 回滚最近一次批次
python -m patrol_archiver rollback -c config.yaml
```

回滚会把 `copy` 的目标删除，把 `move` 的文件移回源位置。完成后可再次用 `show` 查看状态变为 `rolled_back`。

---

## 错误场景演示

### 场景 A：源目录不存在

```bash
python -m patrol_archiver dry-run -c config_bad_source.yaml
# 输出: 错误: 配置校验失败: 源目录不存在: ...
# 退出码 1
```

### 场景 B：两行落到同一归档名

```bash
# 修改配置指向 patrol_duplicate.csv 或直接用下面的方法
python -c "
import yaml, shutil
shutil.copy('patrol_duplicate.csv', '_dup.csv')
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
cfg['csv_path'] = '_dup.csv'
yaml.dump(cfg, open('_dup.yaml', 'w', encoding='utf-8'), allow_unicode=True)
"
python -m patrol_archiver dry-run -c _dup.yaml
# 输出: !!! 存在致命错误，run 将被拒绝 !!!
#       - 归档目标名冲突: .../IMG_0001.jpg <- 第2行, 第3行
# run 子命令会直接失败，退出码 2
```

### 场景 C：重复执行已完成批次

```bash
# 先执行一次 run
python -m patrol_archiver run -c config.yaml
# 再执行一次
python -m patrol_archiver run -c config.yaml
# 输出: 错误: 检测到已完成的相同批次 batch_...，重复执行可能导致冲突。如需再次执行，请先 rollback。
# 退出码 3
```

### 场景 D：回滚目标被其他文件占用

```bash
# 1. 执行一次 run（用 move 更易演示）
python -c "
import yaml
cfg = yaml.safe_load(open('config.yaml', encoding='utf-8'))
cfg['action'] = 'move'
yaml.dump(cfg, open('_mv.yaml', 'w', encoding='utf-8'), allow_unicode=True)
"
# 先重置源目录
python -c "
from pathlib import Path
import shutil
base = Path('source_photos')
if not base.exists(): base.mkdir()
jpg_header = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9'
for p in ['IMG_0001.jpg','IMG_0002.jpg','IMG_0003.jpg','IMG_0004.jpg','IMG_0005.jpg','IMG_0006.jpg','IMG_0007.jpg','IMG_0008.jpg','EXTRA_orphan.jpg']:
    (base/p).write_bytes(jpg_header)
if Path('archive').exists(): shutil.rmtree('archive')
if Path('.patrol_state').exists(): shutil.rmtree('.patrol_state')
"
python -m patrol_archiver run -c _mv.yaml
# 2. 手动在某个原源位置放一个同名文件
python -c "
from pathlib import Path
Path('source_photos/IMG_0001.jpg').write_bytes(b'occupied')
"
# 3. rollback 将失败
python -m patrol_archiver rollback -c _mv.yaml
# 输出: 错误: 回滚前检测到冲突，无法执行: 移动动作的源文件已重新出现...
# 退出码 4
```

---

## 配置模板库复现步骤

### 0. 准备环境

```bash
cd examples
pip install -r ../requirements.txt
```

### 1. template-save 保存配置为命名模板

```bash
# 保存当前 config.yaml 为名为 "daily_patrol" 的模板，附描述
python -m patrol_archiver template-save -c config.yaml -n daily_patrol -d "日常巡检归档配置（默认 copy 模式）"
# 输出: 模板已保存: daily_patrol
#       存储位置: .../.patrol_state/templates
```

同名模板默认拒绝覆盖：

```bash
python -m patrol_archiver template-save -c config.yaml -n daily_patrol
# 输出: 错误: 模板 'daily_patrol' 已存在。使用 --force 强制覆盖。
# 退出码 6

# 带 --force 强制覆盖
python -m patrol_archiver template-save -c config.yaml -n daily_patrol --force
# 输出: 模板已覆盖保存: daily_patrol
```

### 2. template-list 列出所有模板

```bash
# 人类可读格式
python -m patrol_archiver template-list -c config.yaml

# --json 纯输出（可管道到 jq 等工具）
python -m patrol_archiver template-list -c config.yaml --json

# --csv 纯输出
python -m patrol_archiver template-list -c config.yaml --csv
```

注意：`--json` 和 `--csv` 输出是纯净的，不夹杂"状态目录"等提示文本。

### 3. template-show 查看单个模板详情

```bash
# 人类可读格式
python -m patrol_archiver template-show -c config.yaml -n daily_patrol

# --json 纯输出
python -m patrol_archiver template-show -c config.yaml -n daily_patrol --json
```

不存在或损坏的模板会给出清晰错误：

```bash
python -m patrol_archiver template-show -c config.yaml -n no_such_template
# 输出: 错误: 模板不存在: no_such_template
# 退出码 7
```

### 4. template-export 导出模板（JSON / CSV）

```bash
# 导出单个模板为 JSON（按扩展名自动识别格式）
python -m patrol_archiver template-export -c config.yaml -n daily_patrol -o daily_patrol.json

# 导出单个模板为 CSV
python -m patrol_archiver template-export -c config.yaml -n daily_patrol -o daily_patrol.csv

# 不指定 -n 则导出全部模板
python -m patrol_archiver template-export -c config.yaml -o all_templates.json
python -m patrol_archiver template-export -c config.yaml -o all_templates.csv

# 用 -f 显式指定格式（覆盖扩展名推断）
python -m patrol_archiver template-export -c config.yaml -n daily_patrol -o daily_patrol.txt -f json
```

### 5. template-apply 从模板生成 YAML 配置

```bash
# 从 daily_patrol 模板生成新配置文件
python -m patrol_archiver template-apply -s .patrol_state -n daily_patrol -c from_template.yaml

# 输出文件已存在时默认拒绝覆盖
python -m patrol_archiver template-apply -s .patrol_state -n daily_patrol -c from_template.yaml
# 输出: 错误: 输出文件已存在: .../from_template.yaml。使用 --force 强制覆盖。
# 退出码 6

# 带 --force 强制覆盖
python -m patrol_archiver template-apply -s .patrol_state -n daily_patrol -c from_template.yaml --force

# 验证生成的配置能正常 dry-run
python -m patrol_archiver dry-run -c from_template.yaml
```

`template-apply` 会在校验时：
- 校验模板必填字段（`source_dir` / `archive_dir` / `csv_path` / `state_dir` / `action` / `target_pattern`）是否完整
- 校验 `action` 是否为 `copy` 或 `move`
- 校验 `target_pattern` 是否包含必要占位符 `{device_id}`/`{point}`/`{date}`/`{filename}`
- 校验 `source_dir` 是否存在、`csv_path` 是否存在
- 校验 `archive_dir` 父目录、输出目录、`state_dir` 是否可写
- 路径问题仅以警告形式输出到 stderr，YAML 仍会生成（方便用户后续手动修正）
- 字段缺失或非法则直接失败（退出码 8）

---

---

## 归档交接包复现步骤

归档交接包用于把已完成的批次整理成可交给外部人员核对的离线包。每个交接包包含：
- `manifest.json` - JSON 格式的完整文件清单（含 SHA-256 哈希、源路径、归档路径）
- `manifest.csv` - CSV 格式的文件清单（Excel 可直接打开，UTF-8-BOM）
- `batch_report_*.txt` - 原始批次报告副本
- `files/` - 归档照片副本（按原始归档层级组织）
- `README.txt` - 交接说明文档（含目录结构、统计信息、核对方法、配置摘要）

### 0. 准备环境

```bash
cd examples
pip install -r ../requirements.txt
```

### 1. 先执行 run 完成归档

```bash
# 如果是首次，先 dry-run 预演
python -m patrol_archiver dry-run -c config.yaml

# 确认无误后实际执行归档
python -m patrol_archiver run -c config.yaml
```

### 2. handover-create 生成交接包

```bash
# 使用最近一次 run 批次，输出到 handover_pkg 目录
python -m patrol_archiver handover-create -c config.yaml -o handover_pkg

# 或指定批次 ID
python -m patrol_archiver handover-create -c config.yaml -b batch_20240101_001 -o handover_pkg

# 若 handover_pkg 已存在，会自动改用 handover_pkg_1、handover_pkg_2...
```

生成的目录结构：

```
handover_pkg/
├── manifest.json           # 完整清单：handover_id、批次信息、摘要、文件列表
├── manifest.csv            # Excel 可直接打开的 CSV 清单
├── batch_report_batch_*.txt  # 批次报告副本
├── README.txt              # 交接说明（含核对方法）
└── files/                  # 归档照片（按原归档层级复制）
    └── ...
```

### 3. handover-list 列出历史交接包

```bash
# 列出最近 20 条（默认）
python -m patrol_archiver handover-list -c config.yaml

# 列出最近 50 条
python -m patrol_archiver handover-list -c config.yaml -n 50
```

### 4. handover-show 查看交接包详情

```bash
# 查看最近一次交接包
python -m patrol_archiver handover-show -c config.yaml

# 查看指定交接包
python -m patrol_archiver handover-show -c config.yaml -d handover_20240101_000001
```

### 5. handover-verify 校验交接包完整性

```bash
# 校验最近一次交接包
python -m patrol_archiver handover-verify -c config.yaml

# 校验指定交接包
python -m patrol_archiver handover-verify -c config.yaml -d handover_20240101_000001

# 若发现文件缺失或哈希不匹配，会以退出码 12 非 0 退出
echo "Exit code: $?"
```

校验内容包括：
- 交接包目录是否还存在
- `manifest.json` 是否可解析
- `files/` 下每个文件是否存在
- 每个文件的 SHA-256 哈希是否与清单一致
- 原始归档路径是否还存在
- 原始源文件路径是否还存在

### 6. 交给外部人员核对的方式

把整个交接包目录（如 `handover_pkg/`）打包发给外部人员：

```bash
# 打包（Windows PowerShell）
Compress-Archive -Path handover_pkg -DestinationPath handover_pkg.zip

# 或 Linux / macOS
tar -czvf handover_pkg.tar.gz handover_pkg/
```

外部人员收到后，可用以下方式核对（任选其一）：

```bash
# 方式一：使用本工具校验（推荐，前提是接收方也安装了 patrol_archiver）
python -m patrol_archiver handover-verify -c config.yaml -d handover_20240101_000001

# 方式二：手动用 sha256sum 核对（接收方只需标准工具）
cd handover_pkg
python -c "
import json, hashlib, sys
m = json.load(open('manifest.json', encoding='utf-8'))
for f in m['files']:
    h = hashlib.sha256()
    with open(f['relative_path'], 'rb') as fh:
        for chunk in iter(lambda: fh.read(1048576), b''):
            h.update(chunk)
    ok = h.hexdigest() == f['sha256']
    print(f"{'OK' if ok else 'FAIL'} {f['relative_path']}")
    if not ok: sys.exit(1)
"

# 方式三：用 manifest.csv 在 Excel 中人工核对
# manifest.csv 含 idx/source/archive_path/relative_path/file_size/sha256/action/source_signature
```


## CLI 命令总览

| 命令 | 说明 |
|------|------|
| `dry-run -c CONFIG` | 预演，生成计划并保存批次（DRY-RUN） |
| `run -c CONFIG` | 实际执行归档，生成报告 |
| `rollback -c CONFIG [-b BATCH_ID]` | 按批次回滚 |
| `list -c CONFIG [-n N]` | 列出批次历史（持久化） |
| `show -c CONFIG [-b BATCH_ID]` | 显示批次详情、配置摘要、动作日志 |
| `export -c CONFIG [-b BATCH_ID] -o OUTPUT [-f json\|csv\|auto]` | 导出批次报告（JSON 或 CSV，默认按扩展名识别） |
| `doctor -c CONFIG [--json\|--csv]` | 配置体检：检查 YAML 和巡检 CSV 是否可用（dry-run/run 前推荐执行） |
| `doctor-history -c CONFIG [-n N]` | 列出体检历史记录（跨重启可查） |
| `doctor-export -c CONFIG [-d DOCTOR_ID] [-o OUTPUT] [-f json\|csv\|auto] [--json\|--csv]` | 导出体检记录为 JSON 或 CSV |
| `handover-create -c CONFIG [-b BATCH] -o DIR` | 生成归档交接包（manifest.json/csv + 报告副本 + README.txt），默认取最近批次，同名目录自动加序号 |
| `handover-list -c CONFIG [-n N]` | 列出历史交接包（跨重启可查） |
| `handover-show -c CONFIG [-d HANDOVER_ID]` | 查看交接包详情和包内文件列表 |
| `handover-verify -c CONFIG [-d HANDOVER_ID]` | 校验交接包文件存在性、SHA-256 哈希和源/归档路径，异常非 0 退出 |

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功 |
| 1 | 配置/CSV 等一般性错误 |
| 2 | 计划含致命错误（重复目标/路径冲突），run 被拒绝 |
| 3 | 检测到重复执行已完成批次 |
| 4 | 回滚冲突或失败 |
| 10 | doctor 体检发现 error 级别问题 |
| 11 | 交接包生成错误（HandoverError） |
| 12 | 交接包校验发现 error 级别问题 |
