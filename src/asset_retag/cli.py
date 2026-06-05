"""CLI 主入口"""
import logging
import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import click

import os
import sys

# Windows 终端编码适配
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
    sys.stderr.reconfigure(encoding="utf-8") if hasattr(sys.stderr, "reconfigure") else None
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except:
        pass

from . import __version__
from .file_ops import FileOperator, FileOperationError, FileOwnershipError
from .models import BatchStatus
from .parser import ConfigParser, CSVMappingParser, ParseError
from .planner import ExecutionPlanner, FatalPlanningError
from .reporter import Reporter
from .state import (
    StateError,
    StateManager,
    SnapshotError,
    SnapshotFormatError,
    SnapshotConflictError,
)
from .profiles import (
    ProfileError,
    ProfileFormatError,
    ProfileConflictError,
    ProfileNotFoundError,
    ProfileManager,
)
from .inventory import (
    InventoryError,
    InventoryFormatError,
    InventoryConflictError,
    InventoryNotFoundError,
    InventoryManager,
)


# 安全输出（替代 emoji，避免 Windows 编码问题）
_ICON_OK = "[OK]"
_ICON_WARN = "[WARN]"
_ICON_ERR = "[ERR]"
_ICON_INFO = "[INFO]"


def _safe_echo(message: str, *args, **kwargs) -> None:
    """安全输出，处理编码失败时降级"""
    try:
        click.echo(message, *args, **kwargs)
    except UnicodeEncodeError:
        ascii_msg = message.encode("ascii", errors="replace").decode("ascii")
        click.echo(ascii_msg, *args, **kwargs)


def setup_logging(log_dir: Path, batch_id: str, verbose: bool = False) -> None:
    """设置日志"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{batch_id}.log"

    level = logging.DEBUG if verbose else logging.INFO

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING)
    console_formatter = logging.Formatter("%(levelname)s: %(message)s")
    console_handler.setFormatter(console_formatter)

    root_logger = logging.getLogger("asset_retag")
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


@click.group()
@click.version_option(__version__)
def main() -> None:
    """本地资产标签重贴批处理 CLI"""
    pass


def _classify_parse_errors(errors: List[str]) -> Tuple[List[str], List[str]]:
    """分类解析错误：(致命错误, 非致命错误)

    致命错误：照片目录不存在、重复新标签、重复旧编号 - 必须立即中止，不创建任何状态
    非致命错误：空字段、无效类型等 - 可询问用户是否继续
    """
    fatal = []
    non_fatal = []
    for err in errors:
        if ("照片目录不存在" in err
            or ("photo_dir" in err.lower() and "not exist" in err.lower())
            or "重复的新标签" in err
            or "重复的旧编号" in err
            or "duplicate new_tag" in err.lower()
            or "duplicate old_id" in err.lower()):
            fatal.append(err)
        else:
            non_fatal.append(err)
    return fatal, non_fatal


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--profile", "-p", help="配置档案名称")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（不指定则自动生成）")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def dry_run(config: Optional[str], profile: Optional[str], mapping: str, batch_id: Optional[str], skip_confirm: bool, verbose: bool) -> None:
    """预演模式：生成报告，不修改任何资产文件"""
    app_config = None
    state_mgr = None
    batch_created = False

    try:
        mapping_path = Path(mapping).resolve()

        app_config, resolved_config_path = _resolve_config_required(config, profile)
        state_mgr = StateManager(app_config)

        # === 第一步：先解析，不创建批次 ===
        _safe_echo(f"{_ICON_INFO} 正在解析 CSV 映射文件...")
        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        # 检查致命解析错误（照片目录不存在、重复新标签、重复旧编号）
        fatal_parse_errors, non_fatal_parse_errors = _classify_parse_errors(parse_errors)
        if fatal_parse_errors:
            _safe_echo(f"\n{_ICON_ERR} CSV 解析发现 {len(fatal_parse_errors)} 个致命错误：", err=True)
            for err in fatal_parse_errors:
                _safe_echo(f"   - {err}", err=True)
            _safe_echo(f"\n{_ICON_ERR} 致命错误必须修复后才能继续。预演已中止，未创建任何批次状态或报告。", err=True)
            sys.exit(1)

        # 非致命解析错误可询问用户
        if non_fatal_parse_errors:
            _safe_echo(f"\n{_ICON_WARN} CSV 解析发现 {len(non_fatal_parse_errors)} 个问题：")
            for err in non_fatal_parse_errors[:10]:
                _safe_echo(f"   - {err}")
            if len(non_fatal_parse_errors) > 10:
                _safe_echo(f"   ... 还有 {len(non_fatal_parse_errors) - 10} 个问题")
            if not skip_confirm and not click.confirm("\n是否继续处理有效条目？", default=True):
                _safe_echo("已取消。")
                sys.exit(0)

        if not mappings:
            _safe_echo(f"{_ICON_ERR} 没有有效的映射条目，无法继续", err=True)
            sys.exit(1)

        # === 第二步：解析成功后再创建批次 ===
        if not batch_id:
            batch_id = state_mgr.generate_batch_id()

        setup_logging(app_config.log_dir, batch_id, verbose)
        logger = logging.getLogger(__name__)
        logger.info(f"开始预演模式，批次 ID: {batch_id}")

        config_dict = state_mgr.config_to_dict(app_config, mapping_path)
        state_mgr.create_batch(batch_id, config_dict)
        batch_created = True
        state_mgr.update_status(batch_id, BatchStatus.PLANNING, "开始预演计划")

        planner = ExecutionPlanner(app_config)
        plan = planner.generate_plan(mappings, batch_id)

        state_mgr.save_plan(batch_id, plan)
        state_mgr.update_status(batch_id, BatchStatus.PLANNED, "预演计划完成")

        reporter = Reporter(app_config)
        reporter.print_plan_summary(plan)

        reports = reporter.generate_dry_run_report(plan, batch_id)

        _safe_echo(f"\n{_ICON_OK} 预演报告已生成：")
        for name, path in reports.items():
            _safe_echo(f"   - {name}: {path}")

        has_issues = bool(plan.missing_evidence or plan.unregistered or plan.errors)
        if has_issues:
            _safe_echo(f"\n{_ICON_WARN} 检测到问题，请查看报告后决定是否继续执行")
        else:
            _safe_echo(f"\n{_ICON_OK} 预演完成，未检测到致命问题，可以执行实际操作")

        _safe_echo(f"\n批次 ID: {batch_id}")
        if profile:
            _safe_echo(f"执行命令: asset-retag run --profile {profile} -m {mapping} --batch-id {batch_id}")
        else:
            _safe_echo(f"执行命令: asset-retag run -c {resolved_config_path} -m {mapping} --batch-id {batch_id}")

    except FatalPlanningError as e:
        _safe_echo(f"\n{_ICON_ERR} 检测到致命冲突，预演已中止：", err=True)
        _safe_echo(str(e), err=True)
        # 致命冲突也需要清理批次
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
            _safe_echo(f"\n{_ICON_INFO} 已清理半成品批次状态和日志", err=True)
        sys.exit(1)
    except ParseError as e:
        _safe_echo(f"\n{_ICON_ERR} 解析错误: {e}", err=True)
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)
    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 预演失败: {e}", err=True)
        if verbose:
            traceback.print_exc()
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)


@main.command()
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--profile", "-p", help="配置档案名称")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（如之前 dry-run 过）")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def run(config: Optional[str], profile: Optional[str], mapping: str, batch_id: Optional[str], skip_confirm: bool, verbose: bool) -> None:
    """执行资产标签重贴批处理"""
    app_config = None
    state_mgr = None
    batch_created = False

    try:
        mapping_path = Path(mapping).resolve()

        app_config, _ = _resolve_config_required(config, profile)
        state_mgr = StateManager(app_config)

        # === 第一步：先解析，不创建批次 ===
        _safe_echo(f"{_ICON_INFO} 正在解析 CSV 映射文件...")
        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        # 检查致命解析错误（照片目录不存在、重复新标签、重复旧编号）
        fatal_parse_errors, non_fatal_parse_errors = _classify_parse_errors(parse_errors)
        if fatal_parse_errors:
            _safe_echo(f"\n{_ICON_ERR} CSV 解析发现 {len(fatal_parse_errors)} 个致命错误：", err=True)
            for err in fatal_parse_errors:
                _safe_echo(f"   - {err}", err=True)
            _safe_echo(f"\n{_ICON_ERR} 致命错误必须修复后才能继续。已中止，未创建任何批次状态或报告。", err=True)
            sys.exit(1)

        # 非致命解析错误可询问用户
        if non_fatal_parse_errors:
            _safe_echo(f"\n{_ICON_WARN} CSV 解析发现 {len(non_fatal_parse_errors)} 个问题：")
            for err in non_fatal_parse_errors[:10]:
                _safe_echo(f"   - {err}")
            if len(non_fatal_parse_errors) > 10:
                _safe_echo(f"   ... 还有 {len(non_fatal_parse_errors) - 10} 个问题")
            if not skip_confirm and not click.confirm("\n是否继续处理有效条目？", default=True):
                _safe_echo("已取消。")
                sys.exit(0)

        if not mappings:
            _safe_echo(f"{_ICON_ERR} 没有有效的映射条目，无法继续", err=True)
            sys.exit(1)

        # === 第二步：解析成功后再创建或复用批次 ===
        is_new_batch = False
        batch_exists = False
        if not batch_id:
            batch_id = state_mgr.generate_batch_id()
            is_new_batch = True
        else:
            # 检查批次是否已存在
            try:
                state_mgr.get_batch(batch_id)
                batch_exists = True
            except StateError:
                batch_exists = False
                is_new_batch = True

            if batch_exists and not state_mgr.can_execute(batch_id):
                batch_state = state_mgr.get_batch(batch_id)
                _safe_echo(
                    f"{_ICON_ERR} 批次 {batch_id} 状态为 '{batch_state.status}'，无法执行。"
                    f"请先回滚或使用新的批次 ID。",
                    err=True
                )
                sys.exit(1)

        setup_logging(app_config.log_dir, batch_id, verbose)
        logger = logging.getLogger(__name__)
        logger.info(f"开始执行批次，ID: {batch_id}")

        if is_new_batch:
            config_dict = state_mgr.config_to_dict(app_config, mapping_path)
            state_mgr.create_batch(batch_id, config_dict)
            batch_created = True
            state_mgr.update_status(batch_id, BatchStatus.PLANNING, "开始执行计划")

        planner = ExecutionPlanner(app_config)
        plan = planner.generate_plan(mappings, batch_id)

        state_mgr.save_plan(batch_id, plan)

        reporter = Reporter(app_config)
        reporter.print_plan_summary(plan)

        executable_items = [item for item in plan.items if item.status == "planned" and item.photos]
        if not executable_items:
            _safe_echo(f"\n{_ICON_WARN} 没有可执行的项目（所有映射都没有照片或有冲突）")
            # 彻底清理批次，不留下 FAILED 状态
            if state_mgr and batch_created and batch_id:
                state_mgr.delete_batch(batch_id)
                _safe_echo(f"{_ICON_INFO} 已清理批次状态和日志")
            sys.exit(2)

        if not skip_confirm:
            _safe_echo(f"\n将处理 {len(executable_items)} 个映射，共 {sum(len(i.photos) for i in executable_items)} 个文件")
            if not click.confirm("确认执行？此操作将修改文件系统", default=False):
                # 用户取消，彻底清理批次，不留下 FAILED 状态（无论批次是否是本次创建的）
                if state_mgr and batch_id:
                    try:
                        state_mgr.delete_batch(batch_id)
                    except StateError:
                        pass
                _safe_echo("已取消，未留下任何批次状态。")
                sys.exit(0)

        state_mgr.update_status(batch_id, BatchStatus.EXECUTING, "开始执行文件操作")

        operator = FileOperator(app_config, dry_run=False)

        def progress_callback(current: int, total: int, message: str) -> None:
            _safe_echo(f"  [{current}/{total}] {message}")

        successful_ops, failed_ops = operator.execute_plan(plan, on_progress=progress_callback)

        state_mgr.add_operations(batch_id, successful_ops)

        for failure in failed_ops:
            state_mgr.add_error(batch_id, failure.get("error", "未知错误"))

        total_ops = len(successful_ops) + len(failed_ops)
        if total_ops == 0:
            final_status = BatchStatus.FAILED
            state_mgr.update_status(batch_id, final_status, "没有执行任何操作")
        elif len(failed_ops) == 0:
            final_status = BatchStatus.COMPLETED
            state_mgr.update_status(batch_id, final_status, f"成功完成 {len(successful_ops)} 个操作")
        elif len(successful_ops) == 0:
            final_status = BatchStatus.FAILED
            state_mgr.update_status(batch_id, final_status, f"全部失败，共 {len(failed_ops)} 个错误")
        else:
            final_status = BatchStatus.PARTIAL
            state_mgr.update_status(
                batch_id, final_status,
                f"部分完成：成功 {len(successful_ops)} 个，失败 {len(failed_ops)} 个"
            )

        reporter.print_execution_summary(successful_ops, failed_ops)

        reports = reporter.generate_execution_report(
            plan, batch_id, successful_ops, failed_ops, final_status
        )

        _safe_echo(f"\n{_ICON_OK} 执行报告已生成：")
        for name, path in reports.items():
            _safe_echo(f"   - {name}: {path}")

        _safe_echo(f"\n批次 ID: {batch_id}")
        if final_status != BatchStatus.COMPLETED:
            _safe_echo(f"如需回滚: asset-retag rollback --batch-id {batch_id}")

        if final_status in (BatchStatus.FAILED, BatchStatus.PARTIAL):
            sys.exit(2)

    except FatalPlanningError as e:
        _safe_echo(f"\n{_ICON_ERR} 检测到致命冲突，执行已中止：", err=True)
        _safe_echo(str(e), err=True)
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
            _safe_echo(f"\n{_ICON_INFO} 已清理半成品批次状态和日志", err=True)
        sys.exit(1)
    except ParseError as e:
        _safe_echo(f"\n{_ICON_ERR} 解析错误: {e}", err=True)
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)
    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)
    except FileOperationError as e:
        _safe_echo(f"\n{_ICON_ERR} 文件操作错误: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 执行失败: {e}", err=True)
        if verbose:
            traceback.print_exc()
        if state_mgr and batch_created and batch_id:
            state_mgr.delete_batch(batch_id)
        sys.exit(1)


@main.command()
@click.option("--batch-id", "-b", required=True, help="要回滚的批次 ID")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
@click.option("--dry-run", is_flag=True, help="预演回滚，不实际修改文件")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def rollback(batch_id: str, config: Optional[str], profile: Optional[str], dry_run: bool, skip_confirm: bool, verbose: bool) -> None:
    """回滚指定批次的操作"""
    try:
        app_config, _ = _resolve_config(config, profile)
        setup_logging(app_config.log_dir, batch_id, verbose)
        logger = logging.getLogger(__name__)

        state_mgr = StateManager(app_config)

        if not state_mgr.can_rollback(batch_id):
            batch_state = state_mgr.get_batch(batch_id)
            raise click.ClickException(
                f"批次 {batch_id} 状态为 '{batch_state.status}'，无法回滚。"
                f"该批次没有可回滚的操作记录。"
            )

        batch_state = state_mgr.get_batch(batch_id)
        operations = batch_state.operations

        if not operations:
            raise click.ClickException("该批次没有操作记录，无需回滚")

        logger.info(f"开始回滚批次 {batch_id}，共 {len(operations)} 个操作")

        _safe_echo(f"\n批次 ID: {batch_id}")
        _safe_echo(f"当前状态: {batch_state.status.value}")
        _safe_echo(f"操作记录数: {len(operations)}")
        _safe_echo(f"模式: {'预演' if dry_run else '实际执行'}")

        if not skip_confirm:
            if not click.confirm("\n确认回滚？此操作将撤销之前的文件操作", default=False):
                _safe_echo("已取消。")
                return

        state_mgr.update_status(batch_id, BatchStatus.ROLLING_BACK, "开始回滚")

        operator = FileOperator(app_config, dry_run=dry_run)

        def progress_callback(current: int, total: int, message: str) -> None:
            _safe_echo(f"  [{current}/{total}] {message}")

        try:
            rolled_back_ops, failed_ops = operator.rollback(
                operations, on_progress=progress_callback
            )
        except FileOwnershipError as e:
            _safe_echo(f"\n{_ICON_ERR} 回滚安全校验失败：", err=True)
            _safe_echo(str(e), err=True)
            state_mgr.add_error(batch_id, f"回滚安全校验失败: {e}")
            state_mgr.update_status(batch_id, BatchStatus.ROLLBACK_FAILED, "回滚因文件所有权校验失败而中止")
            final_status = BatchStatus.ROLLBACK_FAILED
            rolled_back_ops = []
            failed_ops = [{"error": str(e), "rollback_stopped": True}]
            reporter = Reporter(app_config)
            reporter.print_rollback_summary(rolled_back_ops, failed_ops)
            if not dry_run:
                reports = reporter.generate_rollback_report(batch_id, rolled_back_ops, failed_ops)
                _safe_echo(f"\n{_ICON_INFO} 回滚报告已生成：")
                for name, path in reports.items():
                    _safe_echo(f"   - {name}: {path}")
            sys.exit(1)
        except FileOperationError as e:
            if "回滚因文件锁定而中止" in str(e):
                _safe_echo(f"\n{_ICON_WARN}  {e}", err=True)
                rolled_back_ops = []
                failed_ops = [{"error": str(e), "rollback_stopped": True}]
            else:
                raise

        for failure in failed_ops:
            if not failure.get("rollback_stopped"):
                state_mgr.add_error(batch_id, f"回滚失败: {failure.get('error', '未知错误')}")

        stopped = any(f.get("rollback_stopped") for f in failed_ops)
        if stopped:
            final_status = BatchStatus.ROLLBACK_FAILED
            state_mgr.update_status(batch_id, final_status, "回滚因文件锁定而中止")
        elif len(failed_ops) == 0:
            final_status = BatchStatus.ROLLED_BACK
            state_mgr.update_status(batch_id, final_status, f"成功回滚 {len(rolled_back_ops)} 个操作")
        elif len(rolled_back_ops) == 0:
            final_status = BatchStatus.ROLLBACK_FAILED
            state_mgr.update_status(batch_id, final_status, f"回滚全部失败，共 {len(failed_ops)} 个错误")
        else:
            final_status = BatchStatus.ROLLBACK_FAILED
            state_mgr.update_status(
                batch_id, final_status,
                f"回滚部分完成：成功 {len(rolled_back_ops)} 个，失败 {len(failed_ops)} 个"
            )

        reporter = Reporter(app_config)
        reporter.print_rollback_summary(rolled_back_ops, failed_ops)

        if not dry_run:
            reports = reporter.generate_rollback_report(batch_id, rolled_back_ops, failed_ops)
            _safe_echo(f"\n{_ICON_OK} 回滚报告已生成：")
            for name, path in reports.items():
                _safe_echo(f"   - {name}: {path}")

    except ParseError as e:
        _safe_echo(f"\n{_ICON_ERR} 解析错误: {e}", err=True)
        sys.exit(1)
    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        sys.exit(1)
    except FileOperationError as e:
        _safe_echo(f"\n{_ICON_ERR} 文件操作错误: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 回滚失败: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)


@main.command("list")
@click.option("--status", "-s", help="按状态过滤 (pending/planned/executing/completed/partial/failed/rolled_back)")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
def list_batches(status: Optional[str], config: Optional[str], profile: Optional[str]) -> None:
    """列出所有批次"""
    try:
        app_config, _ = _resolve_config(config, profile)
        state_mgr = StateManager(app_config)

        status_filter = None
        if status:
            try:
                status_filter = BatchStatus(status.lower())
            except ValueError:
                valid_statuses = ", ".join(s.value for s in BatchStatus)
                raise click.ClickException(f"无效的状态 '{status}'。有效值: {valid_statuses}")

        batches = state_mgr.list_batches(status_filter=status_filter)

        reporter = Reporter(app_config)
        reporter.print_batch_list(batches)

        for batch in batches:
            state_mgr.get_batch(batch.batch_id)

    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 查询失败: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--batch-id", "-b", required=True, help="批次 ID")
@click.option("--logs", "-l", is_flag=True, help="显示最近日志")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
def show(batch_id: str, logs: bool, config: Optional[str], profile: Optional[str]) -> None:
    """显示批次详情"""
    try:
        app_config, _ = _resolve_config(config, profile)
        state_mgr = StateManager(app_config)
        batch = state_mgr.get_batch(batch_id)

        reporter = Reporter(app_config)
        reporter.print_batch_detail(batch, show_logs=logs)

    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 查询失败: {e}", err=True)
        sys.exit(1)


@main.command()
@click.option("--batch-id", "-b", required=True, help="批次 ID")
@click.option("--tail", "-n", type=int, help="显示最后 N 行")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
def logs(batch_id: str, tail: Optional[int], config: Optional[str], profile: Optional[str]) -> None:
    """查看批次日志"""
    try:
        app_config, _ = _resolve_config(config, profile)
        state_mgr = StateManager(app_config)

        log_lines = state_mgr.get_logs(batch_id, tail=tail)

        _safe_echo(f"\n批次 {batch_id} 日志：")
        _safe_echo("-" * 80)
        for line in log_lines:
            _safe_echo(line)
        if not log_lines:
            _safe_echo("(无日志)")
        _safe_echo("-" * 80 + "\n")

    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 查询失败: {e}", err=True)
        sys.exit(1)


@main.group()
def batch() -> None:
    """批次管理（快照导出/导入）"""
    pass


@batch.command("export")
@click.option("--batch-id", "-b", required=True, help="要导出的批次 ID")
@click.option("--output-dir", "-o", required=True, type=click.Path(file_okay=False), help="快照输出目录")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的快照文件")
def batch_export(batch_id: str, output_dir: str, config: Optional[str], profile: Optional[str], overwrite: bool) -> None:
    """导出批次快照"""
    try:
        app_config, _ = _resolve_config(config, profile)
        state_mgr = StateManager(app_config)

        output_path = Path(output_dir).resolve()

        _safe_echo(f"{_ICON_INFO} 正在导出批次 {batch_id} 快照...")

        snapshot_file = state_mgr.export_snapshot(batch_id, output_path, overwrite=overwrite)

        _safe_echo(f"\n{_ICON_OK} 快照已导出: {snapshot_file}")
        _safe_echo(f"   包含: 状态、操作记录({state_mgr.get_batch(batch_id).operations.__len__()}条)、配置摘要、报告路径、最近日志")

    except StateError as e:
        _safe_echo(f"\n{_ICON_ERR} 状态错误: {e}", err=True)
        sys.exit(1)
    except SnapshotConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 快照冲突: {e}", err=True)
        sys.exit(1)
    except SnapshotError as e:
        _safe_echo(f"\n{_ICON_ERR} 快照错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导出失败: {e}", err=True)
        sys.exit(1)


@batch.command("import")
@click.option("--snapshot", "-s", required=True, type=click.Path(exists=True, dir_okay=False), help="快照文件路径")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径（可选）")
@click.option("--profile", "-p", help="配置档案名称")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的同名批次")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
def batch_import(snapshot: str, config: Optional[str], profile: Optional[str], overwrite: bool, skip_confirm: bool) -> None:
    """导入批次快照"""
    try:
        app_config, _ = _resolve_config(config, profile)
        state_mgr = StateManager(app_config)

        snapshot_file = Path(snapshot).resolve()

        _safe_echo(f"{_ICON_INFO} 正在导入快照: {snapshot_file}")

        if not skip_confirm:
            _safe_echo(f"\n将导入到当前配置目录:")
            _safe_echo(f"  状态目录: {app_config.state_dir}")
            _safe_echo(f"  日志目录: {app_config.log_dir}")
            _safe_echo(f"  报告目录: {app_config.report_dir}")
            if not click.confirm("\n确认导入？", default=False):
                _safe_echo("已取消。")
                return

        imported_state = state_mgr.import_snapshot(snapshot_file, overwrite=overwrite)

        _safe_echo(f"\n{_ICON_OK} 批次 {imported_state.batch_id} 已成功导入")
        _safe_echo(f"   状态: {imported_state.status.value}")
        _safe_echo(f"   操作记录: {len(imported_state.operations)} 条")
        _safe_echo(f"\n可用命令验证:")
        _safe_echo(f"   asset-retag show --batch-id {imported_state.batch_id}")
        _safe_echo(f"   asset-retag logs --batch-id {imported_state.batch_id}")
        if state_mgr.can_rollback(imported_state.batch_id):
            _safe_echo(f"   asset-retag rollback --batch-id {imported_state.batch_id}")

    except SnapshotFormatError as e:
        _safe_echo(f"\n{_ICON_ERR} 快照格式错误: {e}", err=True)
        sys.exit(1)
    except SnapshotConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 快照冲突: {e}", err=True)
        sys.exit(1)
    except SnapshotError as e:
        _safe_echo(f"\n{_ICON_ERR} 快照错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导入失败: {e}", err=True)
        sys.exit(1)


@main.group()
def profile() -> None:
    """配置档案管理"""
    pass


@profile.command("add")
@click.option("--name", "-n", required=True, help="档案名称")
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--description", "-d", default="", help="档案描述")
def profile_add(name: str, config: str, description: str) -> None:
    """添加配置档案"""
    try:
        profile_mgr = ProfileManager()
        created = profile_mgr.add_profile(name, config, description)
        _safe_echo(f"{_ICON_OK} 已添加档案 '{name}'")
        _safe_echo(f"   配置文件: {created.config_path}")
        if description:
            _safe_echo(f"   描述: {description}")
    except ProfileConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案冲突: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 添加档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("list")
def profile_list() -> None:
    """列出所有配置档案"""
    try:
        profile_mgr = ProfileManager()
        profiles = profile_mgr.list_profiles()
        default = profile_mgr.get_default_profile()
        default_name = default.name if default else None

        if not profiles:
            _safe_echo(f"{_ICON_INFO} 暂无配置档案，使用 profile add 添加")
            return

        _safe_echo(f"\n共有 {len(profiles)} 个配置档案：")
        _safe_echo("-" * 80)
        for p in profiles:
            marker = " [*]" if p.name == default_name else ""
            _safe_echo(f"  {p.name}{marker}")
            _safe_echo(f"    配置文件: {p.config_path}")
            if p.description:
                _safe_echo(f"    描述: {p.description}")
            _safe_echo(f"    创建时间: {p.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        _safe_echo("-" * 80)
        if default_name:
            _safe_echo(f"\n默认档案: {default_name} ([*] 标记)")
            _safe_echo(f"修改默认: asset-retag profile use <name>")
        else:
            _safe_echo(f"\n未设置默认档案")
            _safe_echo(f"设置默认: asset-retag profile use <name>")
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 列出档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("show")
@click.option("--name", "-n", required=True, help="档案名称")
def profile_show(name: str) -> None:
    """显示配置档案详情"""
    try:
        profile_mgr = ProfileManager()
        p = profile_mgr.get_profile(name)
        default = profile_mgr.get_default_profile()
        is_default = default and default.name == name

        _safe_echo(f"\n档案名称: {p.name}" + ("  [默认]" if is_default else ""))
        _safe_echo(f"配置文件: {p.config_path}")
        if p.description:
            _safe_echo(f"描述: {p.description}")
        _safe_echo(f"创建时间: {p.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        _safe_echo(f"更新时间: {p.updated_at.strftime('%Y-%m-%d %H:%M:%S')}")

        if p.config_path.exists():
            content = p.config_path.read_text(encoding="utf-8")
            _safe_echo(f"\n配置文件内容:")
            _safe_echo("-" * 80)
            for line in content.splitlines()[:50]:
                _safe_echo(line)
            if len(content.splitlines()) > 50:
                _safe_echo(f"... (还有 {len(content.splitlines()) - 50} 行)")
            _safe_echo("-" * 80)
        else:
            _safe_echo(f"\n{_ICON_WARN} 配置文件不存在: {p.config_path}")
    except ProfileNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案不存在: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 显示档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("use")
@click.option("--name", "-n", required=True, help="档案名称")
def profile_use(name: str) -> None:
    """设置默认配置档案"""
    try:
        profile_mgr = ProfileManager()
        old_default = profile_mgr.get_default_profile()
        old_name = old_default.name if old_default else None

        set_p = profile_mgr.use_profile(name)

        _safe_echo(f"{_ICON_OK} 已设置默认档案: {name}")
        _safe_echo(f"   配置文件: {set_p.config_path}")
        if old_name and old_name != name:
            _safe_echo(f"   之前默认: {old_name}")
            _safe_echo(f"\n撤销切换: asset-retag profile undo-use")
    except ProfileNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案不存在: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 设置默认档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("undo-use")
def profile_undo_use() -> None:
    """撤销最近一次默认档案切换"""
    try:
        profile_mgr = ProfileManager()
        result = profile_mgr.undo_use()

        if result is None:
            _safe_echo(f"{_ICON_INFO} 没有可撤销的 use 操作")
            return

        restored = result["before"]["default_profile"]
        previous = result["after"]["default_profile"]

        _safe_echo(f"{_ICON_OK} 已撤销默认档案切换")
        _safe_echo(f"   恢复默认: {restored or '(无)'}")
        if previous:
            _safe_echo(f"   之前设置: {previous}")
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 撤销失败: {e}", err=True)
        sys.exit(1)


@profile.command("remove")
@click.option("--name", "-n", required=True, help="档案名称")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
def profile_remove(name: str, skip_confirm: bool) -> None:
    """删除配置档案"""
    try:
        profile_mgr = ProfileManager()
        p = profile_mgr.get_profile(name)
        default = profile_mgr.get_default_profile()
        is_default = default and default.name == name

        _safe_echo(f"档案名称: {name}" + ("  [默认]" if is_default else ""))
        _safe_echo(f"配置文件: {p.config_path}")

        if not skip_confirm:
            if is_default:
                if not click.confirm(f"\n确认删除默认档案 '{name}'？删除后默认档案将被清除。", default=False):
                    _safe_echo("已取消。")
                    return
            else:
                if not click.confirm(f"\n确认删除档案 '{name}'？", default=False):
                    _safe_echo("已取消。")
                    return

        profile_mgr.remove_profile(name)
        _safe_echo(f"{_ICON_OK} 已删除档案: {name}")
    except ProfileNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案不存在: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 删除档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("export")
@click.option("--name", "-n", required=True, help="档案名称")
@click.option("--output", "-o", required=True, type=click.Path(file_okay=True), help="输出文件路径（或目录）")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的文件")
def profile_export(name: str, output: str, overwrite: bool) -> None:
    """导出配置档案到 JSON"""
    try:
        profile_mgr = ProfileManager()
        output_path = Path(output).resolve()

        if output_path.exists() and output_path.is_dir():
            output_path = output_path / f"{name}_profile.json"

        if output_path.exists() and not overwrite:
            raise ProfileConflictError(
                f"输出文件已存在: {output_path}。如需覆盖，请使用 --overwrite 参数。"
            )

        exported = profile_mgr.export_profile(name, output_path)
        _safe_echo(f"{_ICON_OK} 档案已导出: {exported}")
    except ProfileNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案不存在: {e}", err=True)
        sys.exit(1)
    except ProfileConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案冲突: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导出档案失败: {e}", err=True)
        sys.exit(1)


@profile.command("import")
@click.option("--file", "-f", "import_file", required=True, type=click.Path(exists=True, dir_okay=False), help="导入的 JSON 文件路径")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的同名档案（原子替换）")
def profile_import_cmd(import_file: str, overwrite: bool) -> None:
    """从 JSON 导入配置档案"""
    try:
        profile_mgr = ProfileManager()
        imported = profile_mgr.import_profile(import_file, overwrite=overwrite)

        _safe_echo(f"{_ICON_OK} 已导入档案: {imported.name}")
        _safe_echo(f"   配置文件: {imported.config_path}")
        if imported.description:
            _safe_echo(f"   描述: {imported.description}")
        if overwrite:
            _safe_echo(f"   (已覆盖同名档案)")
    except ProfileFormatError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案格式错误: {e}", err=True)
        sys.exit(1)
    except ProfileConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案冲突: {e}", err=True)
        sys.exit(1)
    except ProfileError as e:
        _safe_echo(f"\n{_ICON_ERR} 档案错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导入档案失败: {e}", err=True)
        sys.exit(1)


@main.group()
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--profile", "-p", help="配置档案名称")
@click.pass_context
def inventory(ctx: click.Context, config: Optional[str], profile: Optional[str]) -> None:
    """资产清单管理"""
    app_config, _ = _resolve_config(config, profile)
    ctx.ensure_object(dict)
    ctx.obj["app_config"] = app_config


@inventory.command("scan")
@click.option("--name", "-n", required=True, help="清单名称")
@click.option("--description", "-d", default="", help="清单描述")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的同名清单")
@click.pass_context
def inventory_scan(ctx: click.Context, name: str, description: str, overwrite: bool) -> None:
    """扫描 source_root 生成资产清单"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        inv = inv_mgr.scan(name, description=description, overwrite=overwrite)

        _safe_echo(f"{_ICON_OK} 已扫描清单 '{name}'")
        _safe_echo(f"   源目录: {inv.source_root}")
        _safe_echo(f"   文件数: {inv.file_count}")
        _safe_echo(f"   总大小: {inv.total_size} 字节")
        old_ids = inv.get_old_ids()
        if old_ids:
            _safe_echo(f"   旧编号: {', '.join(old_ids[:10])}" + (" ..." if len(old_ids) > 10 else ""))
        if description:
            _safe_echo(f"   描述: {description}")
        if overwrite:
            _safe_echo(f"   (已覆盖同名清单)")
    except InventoryConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单冲突: {e}", err=True)
        sys.exit(1)
    except InventoryFormatError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单格式错误: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 扫描失败: {e}", err=True)
        traceback.print_exc()
        sys.exit(1)


@inventory.command("list")
@click.pass_context
def inventory_list(ctx: click.Context) -> None:
    """列出所有资产清单"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        inventories = inv_mgr.list_inventories()

        if not inventories:
            _safe_echo(f"{_ICON_INFO} 暂无资产清单，使用 inventory scan 创建")
            return

        _safe_echo(f"\n共有 {len(inventories)} 个资产清单：")
        _safe_echo("-" * 90)
        for inv in inventories:
            _safe_echo(f"  {inv['name']}")
            _safe_echo(f"    源目录: {inv.get('source_root', '-')}")
            _safe_echo(f"    文件数: {inv.get('file_count', 0)}, 总大小: {inv.get('total_size', 0)} 字节")
            if inv.get("description"):
                _safe_echo(f"    描述: {inv['description']}")
            _safe_echo(f"    更新时间: {inv.get('updated_at', '-')}")
        _safe_echo("-" * 90)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 列出清单失败: {e}", err=True)
        sys.exit(1)


@inventory.command("show")
@click.option("--name", "-n", required=True, help="清单名称")
@click.pass_context
def inventory_show(ctx: click.Context, name: str) -> None:
    """显示资产清单详情"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        inv = inv_mgr.get_inventory(name)

        _safe_echo(f"\n清单名称: {inv.name}")
        _safe_echo(f"源目录: {inv.source_root}")
        _safe_echo(f"文件数: {inv.file_count}")
        _safe_echo(f"总大小: {inv.total_size} 字节")
        _safe_echo(f"创建时间: {inv.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        _safe_echo(f"更新时间: {inv.updated_at.strftime('%Y-%m-%d %H:%M:%S')}")
        if inv.description:
            _safe_echo(f"描述: {inv.description}")

        old_ids = inv.get_old_ids()
        if old_ids:
            _safe_echo(f"\n包含 {len(old_ids)} 个旧编号：")
            for oid in old_ids[:20]:
                items = inv.get_items_by_old_id(oid)
                _safe_echo(f"  {oid}: {len(items)} 个文件")
            if len(old_ids) > 20:
                _safe_echo(f"  ... 还有 {len(old_ids) - 20} 个旧编号")

        _safe_echo(f"\n文件列表 (最多显示前 30 个)：")
        for i, item in enumerate(inv.items[:30]):
            old_suffix = f" [{item.old_id}]" if item.old_id else ""
            _safe_echo(f"  {i + 1:>3}. {item.relative_path}  ({item.file_size} 字节, {item.extension}){old_suffix}")
        if len(inv.items) > 30:
            _safe_echo(f"  ... 还有 {len(inv.items) - 30} 个文件")
    except InventoryNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单不存在: {e}", err=True)
        sys.exit(1)
    except InventoryFormatError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单格式错误: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 显示清单失败: {e}", err=True)
        sys.exit(1)


@inventory.command("remove")
@click.option("--name", "-n", required=True, help="清单名称")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.pass_context
def inventory_remove(ctx: click.Context, name: str, skip_confirm: bool) -> None:
    """删除资产清单"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)

        if not skip_confirm:
            click.echo(f"确定要删除清单 '{name}' 吗？此操作不可撤销。")
            if not click.confirm("继续删除？", default=False):
                _safe_echo(f"{_ICON_INFO} 已取消删除")
                return

        inv_mgr.remove_inventory(name)
        _safe_echo(f"{_ICON_OK} 已删除清单: {name}")
    except InventoryNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单不存在: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 删除清单失败: {e}", err=True)
        sys.exit(1)


@inventory.command("export")
@click.option("--name", "-n", required=True, help="清单名称")
@click.option("--output", "-o", "output_path", required=True, type=click.Path(), help="输出文件或目录路径")
@click.pass_context
def inventory_export(ctx: click.Context, name: str, output_path: str) -> None:
    """导出资产清单到 JSON"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        exported_path = inv_mgr.export_inventory(name, output_path)
        _safe_echo(f"{_ICON_OK} 清单已导出: {name}")
        _safe_echo(f"   输出文件: {exported_path}")
    except InventoryNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单不存在: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导出清单失败: {e}", err=True)
        sys.exit(1)


@inventory.command("import")
@click.option("--file", "-f", "import_file", required=True, type=click.Path(exists=True, dir_okay=False), help="导入的 JSON 文件路径")
@click.option("--overwrite", is_flag=True, help="覆盖已存在的同名清单（原子替换）")
@click.pass_context
def inventory_import_cmd(ctx: click.Context, import_file: str, overwrite: bool) -> None:
    """从 JSON 导入资产清单"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        imported = inv_mgr.import_inventory(import_file, overwrite=overwrite)

        _safe_echo(f"{_ICON_OK} 已导入清单: {imported.name}")
        _safe_echo(f"   文件数: {imported.file_count}")
        _safe_echo(f"   总大小: {imported.total_size} 字节")
        if imported.description:
            _safe_echo(f"   描述: {imported.description}")
        if overwrite:
            _safe_echo(f"   (已覆盖同名清单)")
    except InventoryFormatError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单格式错误: {e}", err=True)
        sys.exit(1)
    except InventoryConflictError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单冲突: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 导入清单失败: {e}", err=True)
        sys.exit(1)


@inventory.command("diff")
@click.option("--name", "-n", required=True, help="清单名称")
@click.pass_context
def inventory_diff(ctx: click.Context, name: str) -> None:
    """将清单与当前 source_root 目录比对"""
    try:
        app_config = ctx.obj["app_config"]
        inv_mgr = InventoryManager(app_config)
        diff = inv_mgr.diff_inventory(name)

        _safe_echo(f"\n清单 '{name}' 与当前目录比对结果：")
        _safe_echo(f"  源目录: {app_config.source_root}")

        if not diff.has_changes:
            _safe_echo(f"\n{_ICON_OK} 目录内容与清单一致，没有变化")
            return

        _safe_echo(f"  新增: {len(diff.added)} 个文件")
        _safe_echo(f"  删除: {len(diff.removed)} 个文件")
        _safe_echo(f"  变更: {len(diff.modified)} 个文件")

        if diff.added:
            _safe_echo(f"\n[新增文件]")
            for item in diff.added[:20]:
                _safe_echo(f"  + {item.relative_path}  ({item.file_size} 字节)")
            if len(diff.added) > 20:
                _safe_echo(f"  ... 还有 {len(diff.added) - 20} 个新增文件")

        if diff.removed:
            _safe_echo(f"\n[删除文件]")
            for item in diff.removed[:20]:
                _safe_echo(f"  - {item.relative_path}  ({item.file_size} 字节)")
            if len(diff.removed) > 20:
                _safe_echo(f"  ... 还有 {len(diff.removed) - 20} 个删除文件")

        if diff.modified:
            _safe_echo(f"\n[变更文件]")
            for m in diff.modified[:20]:
                old = m["old"]
                new = m["new"]
                size_delta = new.file_size - old.file_size
                delta_str = f"+{size_delta}" if size_delta > 0 else str(size_delta)
                _safe_echo(f"  ~ {m['path']}  ({old.file_size} -> {new.file_size} 字节, {delta_str})")
            if len(diff.modified) > 20:
                _safe_echo(f"  ... 还有 {len(diff.modified) - 20} 个变更文件")

    except InventoryNotFoundError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单不存在: {e}", err=True)
        sys.exit(1)
    except InventoryError as e:
        _safe_echo(f"\n{_ICON_ERR} 清单错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        _safe_echo(f"\n{_ICON_ERR} 比对失败: {e}", err=True)
        sys.exit(1)


def _discover_config_path() -> Optional[Path]:
    """自动发现配置文件路径"""
    candidates = [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config.yml",
        Path.cwd() / "conf" / "config.yaml",
        Path.cwd() / "conf" / "config.yml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_config(
    config_path: Optional[str],
    profile_name: Optional[str],
) -> Tuple[object, Optional[Path]]:
    """解析配置来源：--config 优先，其次 --profile，再次自动发现，最后默认值

    Args:
        config_path: --config 参数值
        profile_name: --profile 参数值

    Returns:
        (AppConfig 对象, 实际配置文件路径) 元组。
        如果使用内置默认值时配置文件路径为 None。
    """
    if config_path:
        resolved = Path(config_path).resolve()
        return ConfigParser.parse(resolved), resolved

    if profile_name:
        profile_mgr = ProfileManager()
        try:
            profile = profile_mgr.get_profile(profile_name)
        except ProfileNotFoundError as e:
            raise click.ClickException(str(e))
        return ConfigParser.parse(profile.config_path), profile.config_path

    profile_mgr = ProfileManager()
    default_profile = profile_mgr.get_default_profile()
    if default_profile:
        return ConfigParser.parse(default_profile.config_path), default_profile.config_path

    discovered = _discover_config_path()
    if discovered:
        return ConfigParser.parse(discovered), discovered

    from .models import AppConfig, OperationType
    return AppConfig(
        source_root=Path.cwd(),
        target_root=Path.cwd() / "target",
        operation=OperationType.COPY,
    ), None


def _resolve_config_required(
    config_path: Optional[str],
    profile_name: Optional[str],
) -> Tuple[object, Path]:
    """解析配置（必须有来源，不能使用内置默认值）

    用于 dry-run/run 等必须有配置文件的命令。

    Returns:
        (AppConfig 对象, 配置文件路径) 元组
    """
    app_config, resolved_path = _resolve_config(config_path, profile_name)
    if resolved_path is None:
        raise click.ClickException(
            "未指定配置文件。请使用 --config 指定配置文件路径，"
            "或使用 --profile 指定档案名称，"
            "或在当前目录放置 config.yaml，"
            "或使用 profile use 设置默认档案。"
        )
    return app_config, resolved_path


def _load_config_or_default(config_path: Optional[str]) -> object:
    """加载配置，或使用默认配置（仅用于查看历史批次）"""
    app_config, _ = _resolve_config(config_path, None)
    return app_config


if __name__ == "__main__":
    main()
