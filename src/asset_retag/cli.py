"""CLI 主入口"""
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .file_ops import FileOperator, FileOperationError
from .models import BatchStatus
from .parser import ConfigParser, CSVMappingParser, ParseError
from .planner import ExecutionPlanner
from .reporter import Reporter
from .state import StateError, StateManager


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


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（不指定则自动生成）")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def dry_run(config: str, mapping: str, batch_id: Optional[str], verbose: bool) -> None:
    """预演模式：生成报告，不修改任何资产文件"""
    try:
        config_path = Path(config).resolve()
        mapping_path = Path(mapping).resolve()

        app_config = ConfigParser.parse(config_path)

        if not batch_id:
            state_mgr = StateManager(app_config)
            batch_id = state_mgr.generate_batch_id()

        setup_logging(app_config.log_dir, batch_id, verbose)
        logger = logging.getLogger(__name__)
        logger.info(f"开始预演模式，批次 ID: {batch_id}")

        state_mgr = StateManager(app_config)
        config_dict = state_mgr.config_to_dict(app_config, mapping_path)
        state_mgr.create_batch(batch_id, config_dict)
        state_mgr.update_status(batch_id, BatchStatus.PLANNING, "开始预演计划")

        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        if parse_errors:
            click.echo(f"\n⚠️  CSV 解析发现 {len(parse_errors)} 个错误：")
            for err in parse_errors[:10]:
                click.echo(f"   - {err}")
            if len(parse_errors) > 10:
                click.echo(f"   ... 还有 {len(parse_errors) - 10} 个错误")
            if not click.confirm("\n是否继续处理有效条目？", default=True):
                state_mgr.update_status(batch_id, BatchStatus.FAILED, "用户取消")
                click.echo("已取消。")
                return

        if not mappings:
            raise click.ClickException("没有有效的映射条目，无法继续")

        planner = ExecutionPlanner(app_config)
        plan = planner.generate_plan(mappings, batch_id)

        state_mgr.save_plan(batch_id, plan)
        state_mgr.update_status(batch_id, BatchStatus.PLANNED, "预演计划完成")

        reporter = Reporter(app_config)
        reporter.print_plan_summary(plan)

        reports = reporter.generate_dry_run_report(plan, batch_id)

        click.echo("\n📋 预演报告已生成：")
        for name, path in reports.items():
            click.echo(f"   - {name}: {path}")

        has_issues = bool(plan.conflicts or plan.missing_evidence or plan.unregistered or plan.errors)
        if has_issues:
            click.echo("\n⚠️  检测到问题，请查看报告后决定是否继续执行")
        else:
            click.echo("\n✅ 预演完成，未检测到问题，可以执行实际操作")

        click.echo(f"\n批次 ID: {batch_id}")
        click.echo(f"执行命令: asset-retag run -c {config} -m {mapping} --batch-id {batch_id}")

    except ParseError as e:
        click.echo(f"\n❌ 解析错误: {e}", err=True)
        sys.exit(1)
    except StateError as e:
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 预演失败: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)


@main.command()
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False), help="配置文件路径")
@click.option("--mapping", "-m", required=True, type=click.Path(exists=True, dir_okay=False), help="CSV 映射文件路径")
@click.option("--batch-id", "-b", help="指定批次 ID（如之前 dry-run 过）")
@click.option("--skip-confirm", is_flag=True, help="跳过确认提示")
@click.option("--verbose", "-v", is_flag=True, help="显示详细日志")
def run(config: str, mapping: str, batch_id: Optional[str], skip_confirm: bool, verbose: bool) -> None:
    """执行资产标签重贴批处理"""
    try:
        config_path = Path(config).resolve()
        mapping_path = Path(mapping).resolve()

        app_config = ConfigParser.parse(config_path)

        state_mgr = StateManager(app_config)

        if not batch_id:
            batch_id = state_mgr.generate_batch_id()
            is_new_batch = True
        else:
            is_new_batch = False
            if not state_mgr.can_execute(batch_id):
                batch_state = state_mgr.get_batch(batch_id)
                raise click.ClickException(
                    f"批次 {batch_id} 状态为 '{batch_state.status}'，无法执行。"
                    f"请先回滚或使用新的批次 ID。"
                )

        setup_logging(app_config.log_dir, batch_id, verbose)
        logger = logging.getLogger(__name__)
        logger.info(f"开始执行批次，ID: {batch_id}")

        if is_new_batch:
            config_dict = state_mgr.config_to_dict(app_config, mapping_path)
            state_mgr.create_batch(batch_id, config_dict)
            state_mgr.update_status(batch_id, BatchStatus.PLANNING, "开始执行计划")

        mappings, parse_errors = CSVMappingParser.parse(mapping_path, app_config.source_root)

        if parse_errors:
            click.echo(f"\n⚠️  CSV 解析发现 {len(parse_errors)} 个错误：")
            for err in parse_errors[:10]:
                click.echo(f"   - {err}")
            if len(parse_errors) > 10:
                click.echo(f"   ... 还有 {len(parse_errors) - 10} 个错误")
            if not skip_confirm and not click.confirm("\n是否继续处理有效条目？", default=True):
                state_mgr.update_status(batch_id, BatchStatus.FAILED, "用户取消")
                click.echo("已取消。")
                return

        if not mappings:
            raise click.ClickException("没有有效的映射条目，无法继续")

        planner = ExecutionPlanner(app_config)
        plan = planner.generate_plan(mappings, batch_id)

        state_mgr.save_plan(batch_id, plan)

        reporter = Reporter(app_config)
        reporter.print_plan_summary(plan)

        executable_items = [item for item in plan.items if item.status == "planned" and item.photos]
        if not executable_items:
            click.echo("\n⚠️  没有可执行的项目（所有映射都没有照片或有冲突）")
            state_mgr.update_status(batch_id, BatchStatus.FAILED, "无可执行项目")
            return

        if not skip_confirm:
            click.echo(f"\n将处理 {len(executable_items)} 个映射，共 {sum(len(i.photos) for i in executable_items)} 个文件")
            if not click.confirm("确认执行？此操作将修改文件系统", default=False):
                state_mgr.update_status(batch_id, BatchStatus.FAILED, "用户取消")
                click.echo("已取消。")
                return

        state_mgr.update_status(batch_id, BatchStatus.EXECUTING, "开始执行文件操作")

        operator = FileOperator(app_config, dry_run=False)

        def progress_callback(current: int, total: int, message: str) -> None:
            click.echo(f"  [{current}/{total}] {message}")

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

        click.echo("\n📋 执行报告已生成：")
        for name, path in reports.items():
            click.echo(f"   - {name}: {path}")

        click.echo(f"\n批次 ID: {batch_id}")
        if final_status != BatchStatus.COMPLETED:
            click.echo(f"如需回滚: asset-retag rollback --batch-id {batch_id}")

        if final_status in (BatchStatus.FAILED, BatchStatus.PARTIAL):
            sys.exit(2)

    except ParseError as e:
        click.echo(f"\n❌ 解析错误: {e}", err=True)
        sys.exit(1)
    except StateError as e:
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except FileOperationError as e:
        click.echo(f"\n❌ 文件操作错误: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 执行失败: {e}", err=True)
        if verbose:
            traceback.print_exc()
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

        click.echo(f"\n批次 ID: {batch_id}")
        click.echo(f"当前状态: {batch_state.status.value}")
        click.echo(f"操作记录数: {len(operations)}")
        click.echo(f"模式: {'预演' if dry_run else '实际执行'}")

        if not skip_confirm:
            if not click.confirm("\n确认回滚？此操作将撤销之前的文件操作", default=False):
                click.echo("已取消。")
                return

        state_mgr.update_status(batch_id, BatchStatus.ROLLING_BACK, "开始回滚")

        operator = FileOperator(app_config, dry_run=dry_run)

        def progress_callback(current: int, total: int, message: str) -> None:
            click.echo(f"  [{current}/{total}] {message}")

        try:
            rolled_back_ops, failed_ops = operator.rollback(
                operations, on_progress=progress_callback
            )
        except FileOperationError as e:
            if "回滚因文件锁定而中止" in str(e):
                click.echo(f"\n⚠️  {e}", err=True)
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
            click.echo("\n📋 回滚报告已生成：")
            for name, path in reports.items():
                click.echo(f"   - {name}: {path}")

    except ParseError as e:
        click.echo(f"\n❌ 解析错误: {e}", err=True)
        sys.exit(1)
    except StateError as e:
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except FileOperationError as e:
        click.echo(f"\n❌ 文件操作错误: {e}", err=True)
        if verbose:
            traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 回滚失败: {e}", err=True)
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
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 查询失败: {e}", err=True)
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
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 查询失败: {e}", err=True)
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

        click.echo(f"\n批次 {batch_id} 日志：")
        click.echo("-" * 80)
        for line in log_lines:
            click.echo(line)
        if not log_lines:
            click.echo("(无日志)")
        click.echo("-" * 80 + "\n")

    except StateError as e:
        click.echo(f"\n❌ 状态错误: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"\n❌ 查询失败: {e}", err=True)
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
