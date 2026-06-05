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
asset-retag rollback --batch-id <BATCH_ID> --dry-run

# 实际回滚
asset-retag rollback --batch-id <BATCH_ID>
```

> **重要**：回滚时如果检测到目标文件被其他进程占用，会立即停止回滚并报告，**不会强制覆盖**。

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
