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
from .state import StateError, StateManager


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

    致命错误：照片目录不存在 - 必须中止
    非致命错误：重复标签（已在 planner 中处理）、空字段、无效类型等 - 可询问用户
    """
    fatal = []
    non_fatal = []
    for err in errors:
        if "照片目录不存在" in err or "photo_dir" in err.lower() and "not exist" in err.lower():
            fatal.append(err)
        else:
            non_fatal.append(err)
    return fatal, non_fatal


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（不指定则自动生成）")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def dry_run(config: str, mapping: str, batch_id: Optional[str], skip_confirm: bool, verbose: bool) -> None:
    """预演模式：生成报告，不修改任何资产文件"""
    app_config = None
    state_mgr = None
    batch_created = False

    try:
        config_path = Path(config).resolve()
        mapping_path = Path(mapping).resolve()

        app_config = ConfigParser.parse(config_path)
        state_mgr = StateManager(app_config)

        # === 第一步：先解析，不创建批次 ===
        _safe_echo(f"{_ICON_INFO} 正在解析 CSV 映射文件...")
        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        # 检查致命解析错误（照片目录不存在）
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
        _safe_echo(f"执行命令: asset-retag run -c {config} -m {mapping} --batch-id {batch_id}")

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
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（如之前 dry-run 过）")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def run(config: str, mapping: str, batch_id: Optional[str], skip_confirm: bool, verbose: bool) -> None:
    """执行资产标签重贴批处理"""
    app_config = None
    state_mgr = None
    batch_created = False

    try:
        config_path = Path(config).resolve()
        mapping_path = Path(mapping).resolve()

        app_config = ConfigParser.parse(config_path)
        state_mgr = StateManager(app_config)

        # === 第一步：先解析，不创建批次 ===
        _safe_echo(f"{_ICON_INFO} 正在解析 CSV 映射文件...")
        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        # 检查致命解析错误（照片目录不存在）
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
            state_mgr.update_status(batch_id, BatchStatus.FAILED, "无可执行项目")
            sys.exit(2)

        if not skip_confirm:
            _safe_echo(f"\n将处理 {len(executable_items)} 个映射，共 {sum(len(i.photos) for i in executable_items)} 个文件")
            if not click.confirm("确认执行？此操作将修改文件系统", default=False):
                state_mgr.update_status(batch_id, BatchStatus.FAILED, "用户取消")
                _safe_echo("已取消。")
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
@click.option("--dry-run", is_flag=True, help="预演回滚，不实际修改文件")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def rollback(batch_id: str, dry_run: bool, skip_confirm: bool, verbose: bool) -> None:
    """回滚指定批次的操作"""
    try:
        temp_config_path = _discover_config_path()
        if not temp_config_path or not temp_config_path.exists():
            raise click.ClickException(
                "无法自动发现配置文件。请在项目目录下运行，或确保 config.yaml 存在。"
            )

        app_config = ConfigParser.parse(temp_config_path)
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
def list_batches(status: Optional[str], config: Optional[str]) -> None:
    """列出所有批次"""
    try:
        app_config = _load_config_or_default(config)
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
def show(batch_id: str, logs: bool, config: Optional[str]) -> None:
    """显示批次详情"""
    try:
        app_config = _load_config_or_default(config)
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
def logs(batch_id: str, tail: Optional[int], config: Optional[str]) -> None:
    """查看批次日志"""
    try:
        app_config = _load_config_or_default(config)
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


def _load_config_or_default(config_path: Optional[str]) -> object:
    """加载配置，或使用默认配置（仅用于查看历史批次）"""
    if config_path:
        return ConfigParser.parse(Path(config_path).resolve())

    discovered = _discover_config_path()
    if discovered:
        return ConfigParser.parse(discovered)

    from .models import AppConfig, OperationType
    return AppConfig(
        source_root=Path.cwd(),
        target_root=Path.cwd() / "target",
        operation=OperationType.COPY,
    )


if __name__ == "__main__":
    main()
