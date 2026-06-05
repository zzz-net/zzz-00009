# 本地资产标签重贴批处理 CLI

一个本地离线的资产标签重贴批处理工具，用于将旧编号的资产照片按照新标签重新整理归档。

## ✨ 功能特性

- **预演模式 (Dry-run)**：先生成详细报告，不修改任何资产文件
- **冲突检测**：重复新标签、目标路径冲突、目标已存在、未登记目录自动检测
- **批量执行**：支持复制 (copy) 或移动 (move) 两种操作模式
- **归档保护**：移动操作前自动归档源文件
- **操作回滚**：支持完整回滚，遇到文件锁定立即停止不覆盖
- **幂等控制**：防止重复执行同一批次
- **状态持久化**：进程重启后可查看历史日志和恢复操作
- **多格式报告**：JSON 和 CSV 格式的批次结果导出

## 📦 安装

```bash
# 克隆或下载项目后，在项目根目录执行
pip install -e .
```

依赖：
- Python >= 3.9
- click >= 8.0
- pyyaml >= 6.0
- pydantic >= 2.0

## 🚀 快速开始

### 1. 查看样例数据

```bash
# 项目自带完整样例数据
ls examples/
├── config.yaml              # 配置文件样例
├── mapping.csv              # 正常映射表
├── mapping_with_errors.csv  # 含错误的映射表（用于测试）
├── config_invalid.yaml      # 无效配置（用于测试）
├── source/                  # 源照片目录
│   ├── OLD001_laptop/       # 各资产的照片
│   ├── OLD002_monitor/
│   └── ...
├── target/                  # 目标目录（处理结果）
├── archive/                 # 归档目录
└── reports/                 # 报告输出目录
```

### 2. 预演模式（推荐先执行）

```bash
# 生成执行计划和报告，不修改任何文件
asset-retag dry-run -c examples/config.yaml -m examples/mapping.csv
```

输出内容：
- 待处理列表 (`*_pending.csv`)
- 缺证据列表 (`*_missing_evidence.csv`)
- 未登记文件列表 (`*_unregistered.csv`)
- 冲突报告 (`*_conflicts.csv`)
- 完整摘要 (`*_summary.json`)

### 3. 实际执行

```bash
# 使用预演生成的批次 ID 执行（或自动生成新批次）
asset-retag run -c examples/config.yaml -m examples/mapping.csv --batch-id <BATCH_ID>

# 或者直接执行，自动生成批次 ID
asset-retag run -c examples/config.yaml -m examples/mapping.csv

# 跳过确认提示（适合脚本调用）
asset-retag run -c examples/config.yaml -m examples/mapping.csv --skip-confirm
```

### 4. 查看批次

```bash
# 列出所有批次
asset-retag list

# 按状态过滤
asset-retag list --status completed

# 查看批次详情
asset-retag show --batch-id <BATCH_ID>

# 查看批次日志
asset-retag logs --batch-id <BATCH_ID>

# 查看最后 20 行日志
asset-retag logs --batch-id <BATCH_ID> --tail 20
```

### 5. 回滚操作

```bash
# 预演回滚（不实际修改文件）
asset-retag rollback --batch-id <BATCH_ID> --config examples/config.yaml --dry-run

# 实际回滚（推荐显式指定配置）
asset-retag rollback --batch-id <BATCH_ID> --config examples/config.yaml

# 也可以自动发现配置（如果在项目目录下）
asset-retag rollback --batch-id <BATCH_ID>
```

> **重要**：回滚时如果检测到目标文件被其他进程占用，会立即停止回滚并报告，**不会强制覆盖**。

### 6. 批次快照导出/导入

```bash
# 导出批次快照到指定目录（目录不存在会自动创建）
asset-retag batch export --batch-id <BATCH_ID> --output-dir ./snapshots

# 导出时覆盖已存在的快照文件
asset-retag batch export --batch-id <BATCH_ID> --output-dir ./snapshots --overwrite

# 导入批次快照（默认不覆盖同名批次）
asset-retag batch import --snapshot ./snapshots/<BATCH_ID>_snapshot.json

# 导入时覆盖已存在的同名批次
asset-retag batch import --snapshot ./snapshots/<BATCH_ID>_snapshot.json --overwrite

# 跳过确认提示（适合脚本调用）
asset-retag batch import --snapshot ./snapshots/<BATCH_ID>_snapshot.json --skip-confirm

# 指定配置文件导入
asset-retag batch import --snapshot ./snapshots/<BATCH_ID>_snapshot.json --config examples/config.yaml
```

> **快照内容**：每个快照 JSON 包含批次状态、完整操作记录、配置摘要、相关报告路径和最近 100 行日志。
>
> **导入规则（跨配置迁移友好）**：
> - 快照中的 `state_dir`/`log_dir`/`report_dir` 路径**仅作为来源说明**，不会强制校验与当前配置一致
> - 导入时数据会**真正写入当前配置解析出的 state/log/report 目录**，可以从另一套配置目录无缝迁移回来
> - 同名批次默认拒绝导入，需显式 `--overwrite` 进行**原子替换**（状态+日志同时替换，失败不留下半截文件）
> - 格式损坏的快照会给出明确错误提示（JSON 损坏、缺字段、非法状态等）
>
> **导入后可用性**：导入的批次可正常使用 `list`/`show`/`logs`/`rollback` 命令，进程重启后依然可用。

### 7. 配置档案 (Profile) 管理

```bash
# 添加配置档案（把 config.yaml 注册为命名档案）
asset-retag profile add --name production --config ./config.yaml --description "生产环境配置"
asset-retag profile add --name test --config ./examples/config.yaml --description "测试环境配置"

# 查看所有档案（默认档案带 [*] 标记）
asset-retag profile list

# 查看档案详情（含配置文件内容）
asset-retag profile show --name production

# 设置默认档案（跨进程重启保持）
asset-retag profile use --name production

# 撤销最近一次默认档案切换
asset-retag profile undo-use

# 删除档案（删除默认档案会同时清除默认设置）
asset-retag profile remove --name test --skip-confirm

# 导出档案到 JSON
asset-retag profile export --name production --output ./snapshots/
asset-retag profile export --name production --output ./snapshots/prod_profile.json --overwrite

# 从 JSON 导入档案（同名默认拒绝）
asset-retag profile import --file ./snapshots/production_profile.json

# 强制覆盖同名档案（原子替换）
asset-retag profile import --file ./snapshots/production_profile.json --overwrite
```

> **档案特性**：
> - 默认档案通过 `~/.asset-retag/profiles/profiles.json` 持久化，**跨进程重启保持**
> - `use` 和 `remove` 操作会写入 `profile_operations.log` 操作日志
> - `undo-use` 可以撤销最近一次 `use` 操作，恢复之前的默认档案
> - 导入导出使用 JSON 格式，包含档案名称、配置路径、描述、创建/更新时间
> - 同名导入默认拒绝，`--overwrite` 时原子替换
> - 导入时会校验：JSON 完整性、必填字段、配置文件存在性、目标目录写权限

#### 使用档案复用配置

所有接受 `--config` 的命令都可以用 `--profile` 复用已注册的档案：

```bash
# dry-run / run 使用档案
asset-retag dry-run --profile production -m ./mapping.csv
asset-retag run --profile production -m ./mapping.csv --skip-confirm

# list / show / logs 使用档案
asset-retag list --profile test
asset-retag show --batch-id batch_20240101_120000_abcd --profile test
asset-retag logs --batch-id batch_20240101_120000_abcd --profile test

# rollback 使用档案
asset-retag rollback --batch-id batch_20240101_120000_abcd --profile test --dry-run

# batch 快照导入导出使用档案
asset-retag batch export --batch-id batch_20240101_120000_abcd --profile production --output-dir ./snapshots
asset-retag batch import --snapshot ./snapshots/batch_xxx_snapshot.json --profile test --skip-confirm
```

> 配置优先级：`--config` > `--profile` > 自动发现 `./config.yaml` > 默认档案 > 内置默认值

### 8. 资产清单 (Inventory) 管理

```bash
# 扫描 source_root 生成资产清单（记录相对路径、大小、mtime、扩展名、所属旧编号）
asset-retag inventory -c ./config.yaml scan --name baseline_2024 --description "2024年基线清单"

# 覆盖同名清单
asset-retag inventory -c ./config.yaml scan --name baseline_2024 --overwrite

# 查看所有清单（跨进程重启可见）
asset-retag inventory -c ./config.yaml list
asset-retag inventory --profile production list

# 查看清单详情（含文件列表、旧编号统计）
asset-retag inventory -c ./config.yaml show --name baseline_2024

# 将清单与当前 source_root 目录比对（新增/删除/变更文件）
asset-retag inventory -c ./config.yaml diff --name baseline_2024

# 删除清单
asset-retag inventory -c ./config.yaml remove --name baseline_2024 --skip-confirm

# 导出清单到 JSON
asset-retag inventory -c ./config.yaml export --name baseline_2024 --output ./snapshots/
asset-retag inventory -c ./config.yaml export --name baseline_2024 --output ./snapshots/baseline.json

# 从 JSON 导入清单（同名默认拒绝）
asset-retag inventory -c ./config.yaml import --file ./snapshots/baseline_2024_inventory.json

# 强制覆盖同名清单（原子替换）
asset-retag inventory -c ./config.yaml import --file ./snapshots/baseline_2024_inventory.json --overwrite
```

> **清单特性**：
> - 清单数据存储在 `state_dir/inventories/`，**跨进程重启保持**
> - 每个清单条目记录：相对路径、文件大小、修改时间(mtime)、扩展名、所属旧编号
> - 旧编号自动从目录名提取（如 `OLD001_laptop/` -> `OLD001`）
> - 扫描、导入导出、删除操作均写入操作日志
> - 导入导出使用 JSON 格式，支持版本校验和必填字段检查
> - 同名导入默认拒绝，`--overwrite` 时原子替换（先写临时文件再 replace，不留半成品）
> - 错误场景清晰报错：配置不存在、空目录、损坏 JSON、缺字段、无写权限、同名冲突

#### diff 结果解读

`inventory diff` 输出三类变化：
- **新增 [+]**：当前目录有但清单中没有的文件
- **删除 [-]**：清单中有但当前目录没有的文件
- **变更 [~]**：两边都存在但文件大小或修改时间不同的文件

### 9. 交接包 (Handoff) 管理

```bash
# 从批次创建交接包（打包配置摘要、批次状态、报告索引、最近日志）
asset-retag handoff -c ./config.yaml create --batch-id <BATCH_ID>

# 指定交接包 ID 和备注
asset-retag handoff -c ./config.yaml create --batch-id <BATCH_ID> \
  --handoff-id handoff_2024_shift_01 --note "日班交接，完成3个批次"

# 列出所有交接包（跨进程重启可见）
asset-retag handoff -c ./config.yaml list
asset-retag handoff --profile production list

# 查看交接包详情
asset-retag handoff -c ./config.yaml show --handoff-id <HANDOFF_ID>

# 查看详情并附带最近日志
asset-retag handoff -c ./config.yaml show --handoff-id <HANDOFF_ID> --logs

# 导出交接包到 JSON
asset-retag handoff -c ./config.yaml export --handoff-id <HANDOFF_ID> --output ./handoffs/
asset-retag handoff -c ./config.yaml export --handoff-id <HANDOFF_ID> --output ./handoffs/my_handoff.json

# 强制覆盖导出
asset-retag handoff -c ./config.yaml export --handoff-id <HANDOFF_ID> \
  --output ./handoffs/my_handoff.json --overwrite

# 从 JSON 导入交接包（同名默认拒绝）
asset-retag handoff -c ./config.yaml import --file ./handoffs/<HANDOFF_ID>_handoff.json

# 强制覆盖同名交接包（原子替换）
asset-retag handoff -c ./config.yaml import --file ./handoffs/<HANDOFF_ID>_handoff.json --overwrite

# 删除交接包（需要确认）
asset-retag handoff -c ./config.yaml remove --handoff-id <HANDOFF_ID>

# 跳过确认直接删除
asset-retag handoff -c ./config.yaml remove --handoff-id <HANDOFF_ID> --skip-confirm
```

> **交接包特性**：
> - 存储在 `state_dir/handoffs/`，**跨进程重启保持**
> - 每个交接包包含：配置摘要、批次状态、操作记录数、错误数、报告索引（文件名/路径/大小）、最近 100 行日志、自定义备注
> - 创建、导入导出、删除操作均写入 `handoff_operations.log` 操作日志
> - 导入导出使用 JSON 格式，支持版本校验（`handoff_version: 1.0`）和必填字段检查
> - 同名导入/导出默认拒绝，`--overwrite` 时原子替换（先写临时文件再 replace，不留半成品）
> - 所有错误场景清晰报错，**不输出 traceback**：批次不存在、交接包不存在、损坏 JSON、缺字段、同名冲突、无写权限

#### Handoff 错误提示示例

```bash
# 批次不存在
asset-retag handoff -c ./config.yaml create --batch-id nonexistent_batch
# 输出：[ERR] 批次错误: 批次不存在: nonexistent_batch

# 交接包不存在
asset-retag handoff -c ./config.yaml show --handoff-id nonexistent_handoff
# 输出：[ERR] 交接包不存在: 交接包不存在: nonexistent_handoff

# 同名交接包冲突（默认拒绝）
asset-retag handoff -c ./config.yaml import --file ./handoffs/existing_handoff.json
# 输出：[ERR] 交接包冲突: 交接包 'xxx' 已存在。如需覆盖，请使用 --overwrite 参数进行原子替换。

# 导入损坏 JSON
asset-retag handoff -c ./config.yaml import --file ./handoffs/broken.json
# 输出：[ERR] 交接包格式错误: 交接包 JSON 解析失败，文件可能已损坏: ...

# 导入缺少必填字段
asset-retag handoff -c ./config.yaml import --file ./handoffs/incomplete.json
# 输出：[ERR] 交接包格式错误: 交接包缺少必填字段: handoff_id
```

## 📋 配置说明 (`config.yaml`)

```yaml
# 源根目录：包含所有待处理的照片目录
source_root: ./examples/source

# 目标根目录：处理后的文件将按分类存放
target_root: ./examples/target

# 归档目录（可选）：move 操作时源文件先归档到此处
archive_root: ./examples/archive

# 操作类型：copy 或 move
# copy: 复制文件到目标目录，源文件保留
# move: 移动文件到目标目录，源文件先归档再删除
operation: copy

# 支持的照片文件扩展名
photo_extensions:
  - jpg
  - jpeg
  - png
  - gif
  - bmp
  - tiff
  - heic
  - raw

# 目标目录命名模板
# 可用变量: {asset_type}, {new_tag}, {old_id}
dir_pattern: "{asset_type}/{new_tag}"

# 目标文件命名模板
# 可用变量: {asset_type}, {new_tag}, {old_id}, {idx}, {ext}
filename_pattern: "{new_tag}_{idx:04d}.{ext}"

# 状态目录（可选，默认 ~/.asset-retag/state）
state_dir: ./examples/state

# 日志目录（可选，默认 ~/.asset-retag/logs）
log_dir: ./examples/logs

# 报告目录（可选，默认 ./reports）
report_dir: ./examples/reports
```

## 📊 CSV 映射格式

| 字段 | 说明 | 示例 |
|------|------|------|
| `old_id` | 资产旧编号 | `OLD001` |
| `new_tag` | 资产新标签 | `AST-2024-001` |
| `asset_type` | 资产类型 | `hardware` / `software` / `document` / `other` |
| `photo_dir` | 照片目录（相对 source_root 或绝对路径） | `OLD001_laptop` |

示例：
```csv
old_id,new_tag,asset_type,photo_dir
OLD001,AST-2024-001,hardware,OLD001_laptop
OLD002,AST-2024-002,hardware,OLD002_monitor
```

## ❌ 失败场景处理

### 1. 重复新标签
同一 `new_tag` 被多个 `old_id` 使用时，会在预演阶段报告冲突。

### 2. 照片目录不存在
CSV 中指定的 `photo_dir` 不存在时，会在解析阶段报错。

### 3. 配置字段错误
- 缺少必填字段（`source_root`, `target_root`）
- `operation` 不是 `copy` 或 `move`
- 路径不存在

### 4. 重复执行同一批次
批次完成后再次执行会被拒绝，需要先回滚或使用新的批次 ID。

### 5. 目标文件已存在且被占用
执行或回滚时检测到文件锁定，会立即停止操作，不覆盖。

## 🔄 回滚机制

回滚操作按**反向顺序**撤销之前的操作：

- **Copy 操作**：删除目标文件，从归档恢复源文件
- **Move 操作**：将目标文件移回源位置
- **文件锁定检测**：回滚前检查目标文件是否被占用，如果被占用则立即停止，不覆盖任何文件

## 📁 项目结构

```
src/asset_retag/
├── __init__.py        # 包入口
├── models.py          # 数据模型定义
├── parser.py          # 解析模块（配置、CSV）
├── planner.py         # 计划模块（dry-run、冲突检测）
├── file_ops.py        # 文件操作模块（复制、归档、回滚）
├── state.py           # 状态记录模块（批次、日志、幂等）
├── reporter.py        # 报告模块（JSON/CSV 导出）
├── profiles.py        # 配置档案管理模块
├── inventory.py       # 资产清单管理模块
└── cli.py             # CLI 主入口
```

## 🛠️ 完整命令示例

```bash
# ========== 基础流程 ==========

# 1. 预演
asset-retag dry-run -c examples/config.yaml -m examples/mapping.csv

# 2. 查看报告后执行
asset-retag run -c examples/config.yaml -m examples/mapping.csv --batch-id <BATCH_ID>

# 3. 查看结果
asset-retag show --batch-id <BATCH_ID> --logs

# ========== 错误处理测试 ==========

# 测试无效配置
asset-retag dry-run -c examples/config_invalid.yaml -m examples/mapping.csv

# 测试含错误的 CSV
asset-retag dry-run -c examples/config.yaml -m examples/mapping_with_errors.csv

# ========== 回滚测试 ==========

# 先执行一个批次
asset-retag run -c examples/config.yaml -m examples/mapping.csv --skip-confirm

# 查看批次 ID
asset-retag list

# 回滚
asset-retag rollback --batch-id <BATCH_ID>

# ========== 进程重启后的一致性测试 ==========

# 执行中途模拟进程退出后，仍可查看历史
asset-retag list
asset-retag logs --batch-id <BATCH_ID>

# ========== 批次快照导出/导入 ==========

# 导出批次快照
asset-retag batch export --batch-id test_normal_run_001 --output-dir ./snapshots

# 查看导出的快照文件
ls ./snapshots/test_normal_run_001_snapshot.json

# 清理本地批次后导入
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --skip-confirm

# 导入后验证可用性
asset-retag list
asset-retag show --batch-id test_normal_run_001
asset-retag logs --batch-id test_normal_run_001
asset-retag rollback --batch-id test_normal_run_001 --dry-run

# 同名批次冲突测试（默认拒绝）
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --skip-confirm

# 强制覆盖导入
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --overwrite --skip-confirm

# 跨配置目录导入测试（state/log 目录不一致时仍能成功）
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --config other_config.yaml --skip-confirm

# ========== 跨配置目录迁移场景 ==========

# 场景：在机器 A 使用了 ~/old_workspace/config.yaml 执行了批次，现在要迁移到机器 B 的 ~/new_workspace/

# 1. 在机器 A 导出批次快照
asset-retag batch export --batch-id batch_20240101_120000_abcd1234 --output-dir ./snapshots --config ~/old_workspace/config.yaml

# 2. 把快照文件拷贝到机器 B
#    scp ./snapshots/batch_20240101_120000_abcd1234_snapshot.json user@machineB:~/

# 3. 在机器 B 使用新配置导入（state/log/report 路径自动适配新配置）
asset-retag batch import \
  --snapshot ~/batch_20240101_120000_abcd1234_snapshot.json \
  --config ~/new_workspace/config.yaml \
  --skip-confirm

# 4. 验证迁移结果
asset-retag list --config ~/new_workspace/config.yaml
asset-retag show --batch-id batch_20240101_120000_abcd1234 --config ~/new_workspace/config.yaml
asset-retag logs --batch-id batch_20240101_120000_abcd1234 --config ~/new_workspace/config.yaml
asset-retag rollback --batch-id batch_20240101_120000_abcd1234 --config ~/new_workspace/config.yaml --dry-run

# ========== 错误提示示例 ==========

# 同名批次冲突（默认拒绝，提示使用 --overwrite）
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --skip-confirm
# 输出：[ERR] 快照冲突: 批次 test_normal_run_001 已存在（状态文件: ...）。如需覆盖，请使用 --overwrite 参数进行原子替换。

# 快照文件损坏
asset-retag batch import --snapshot ./snapshots/broken.json --skip-confirm
# 输出：[ERR] 快照格式错误: 快照 JSON 解析失败，文件可能已损坏: ...

# 快照缺少必填字段
asset-retag batch import --snapshot ./snapshots/incomplete.json --skip-confirm
# 输出：[ERR] 快照格式错误: 快照缺少必填字段: state

# 快照含非法状态值
asset-retag batch import --snapshot ./snapshots/bad_status.json --skip-confirm
# 输出：[ERR] 快照格式错误: 无效的批次状态: invalid_status_123

# 目标目录无权限
asset-retag batch import --snapshot ./snapshots/test_normal_run_001_snapshot.json --config /root/restricted_config.yaml --skip-confirm
# 输出：[ERR] 快照错误: 目标目录权限不足，无法创建 state/log 目录: ...

# ========== 配置档案 (Profile) 管理 ==========

# 1. 添加档案
asset-retag profile add --name prod --config ./config.yaml --description "生产环境"
asset-retag profile add --name test --config ./examples/config.yaml --description "测试环境"

# 2. 查看所有档案
asset-retag profile list

# 3. 查看档案详情
asset-retag profile show --name prod

# 4. 设置默认档案（跨进程重启保持）
asset-retag profile use --name prod

# 5. 撤销默认档案切换
asset-retag profile undo-use

# 6. 删除档案
asset-retag profile remove --name test --skip-confirm

# 7. 导出档案
asset-retag profile export --name prod --output ./snapshots/
asset-retag profile export --name prod --output ./snapshots/prod_profile.json --overwrite

# 8. 导入档案（同名默认拒绝）
asset-retag profile import --file ./snapshots/prod_profile.json

# 9. 覆盖导入（原子替换）
asset-retag profile import --file ./snapshots/prod_profile.json --overwrite

# 10. 使用档案执行命令
asset-retag dry-run --profile prod -m ./mapping.csv
asset-retag run --profile prod -m ./mapping.csv --skip-confirm
asset-retag list --profile prod
asset-retag show --batch-id batch_xxx --profile prod
asset-retag logs --batch-id batch_xxx --profile prod
asset-retag rollback --batch-id batch_xxx --profile prod --dry-run
asset-retag batch export --batch-id batch_xxx --profile prod --output-dir ./snapshots
asset-retag batch import --snapshot ./snapshots/batch_xxx_snapshot.json --profile prod --skip-confirm

# ========== Profile 错误提示示例 ==========

# 添加同名档案（默认拒绝）
asset-retag profile add --name prod --config ./other.yaml
# 输出：[ERR] 档案冲突: 档案 'prod' 已存在。如需覆盖，请使用 --overwrite 参数。

# 档案不存在
asset-retag profile show --name nonexistent
# 输出：[ERR] 档案不存在: 档案不存在: nonexistent

# 使用不存在的档案
asset-retag dry-run --profile nonexistent -m mapping.csv
# 输出：档案不存在: nonexistent

# 导入损坏 JSON
asset-retag profile import --file ./broken.json
# 输出：[ERR] 档案格式错误: 导入文件 JSON 解析失败，文件可能已损坏: ...

# 导入缺少字段
asset-retag profile import --file ./incomplete.json
# 输出：[ERR] 档案格式错误: 导入数据缺少必填字段: profile_version

# 配置文件不存在时添加档案
asset-retag profile add --name bad --config ./nonexistent.yaml
# 输出：[ERR] 档案错误: 配置文件不存在: .../nonexistent.yaml
```

## ⚠️ 注意事项

1. **预演模式不修改任何文件**：强烈建议每次执行前先运行 `dry-run`
2. **回滚安全性**：回滚时检测到文件锁定会立即停止，不会强制覆盖
3. **幂等控制**：已完成的批次不能重复执行，必须先回滚
4. **归档重要性**：使用 `move` 操作时建议配置 `archive_root`，以便回滚
5. **文件锁定**：Windows 下文件被打开时会被锁定，回滚会停止

## 📝 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 配置/解析/状态等致命错误 |
| 2 | 部分成功或执行失败 |

## 📄 License

本工具为本地离线工具，不连接任何云端资产系统。
