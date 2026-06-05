"""状态记录模块 - 批次状态、日志、幂等控制"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    AppConfig,
    BatchState,
    BatchStatus,
    ExecutionPlan,
)

logger = logging.getLogger(__name__)


class StateError(Exception):
    """状态管理错误"""
    pass


class StateManager:
    """状态管理器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.state_dir = config.state_dir
        self.log_dir = config.log_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _get_state_file(self, batch_id: str) -> Path:
        """获取批次状态文件路径"""
        return self.state_dir / f"{batch_id}.json"

    def _get_log_file(self, batch_id: str) -> Path:
        """获取批次日志文件路径"""
        return self.log_dir / f"{batch_id}.log"

    def generate_batch_id(self) -> str:
        """生成唯一批次 ID"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"batch_{timestamp}_{short_uuid}"

    def create_batch(self, batch_id: str, config_dict: Dict[str, Any]) -> BatchState:
        """创建新批次

        幂等控制：如果批次已存在且不是失败状态，抛出异常
        """
        state_file = self._get_state_file(batch_id)

        if state_file.exists():
            existing = self._load_state(state_file)
            if existing.status not in (BatchStatus.FAILED, BatchStatus.ROLLBACK_FAILED):
                raise StateError(
                    f"批次 {batch_id} 已存在，状态为 '{existing.status}'。"
                    f"如需重新执行，请先回滚或使用新的批次 ID。"
                )
            logger.warning(f"批次 {batch_id} 已存在但状态为失败，将覆盖原有状态")

        now = datetime.now()
        state = BatchState(
            batch_id=batch_id,
            status=BatchStatus.PENDING,
            created_at=now,
            updated_at=now,
            config=config_dict,
        )

        self._save_state(state)
        self._log(batch_id, f"批次已创建，状态: {state.status}")

        return state

    def update_status(self, batch_id: str, status: BatchStatus, message: str = "") -> BatchState:
        """更新批次状态"""
        state = self.get_batch(batch_id)
        state.status = status
        state.updated_at = datetime.now()

        if message:
            self._log(batch_id, f"状态变更为 {status.value}: {message}")
        else:
            self._log(batch_id, f"状态变更为 {status.value}")

        self._save_state(state)
        return state

    def save_plan(self, batch_id: str, plan: ExecutionPlan) -> None:
        """保存执行计划到批次状态"""
        state = self.get_batch(batch_id)

        plan_dict = {
            "batch_id": plan.batch_id,
            "created_at": plan.created_at.isoformat(),
            "items": [
                {
                    "old_id": item.mapping.old_id,
                    "new_tag": item.mapping.new_tag,
                    "asset_type": item.mapping.asset_type.value,
                    "photo_dir": str(item.mapping.photo_dir),
                    "target_dir": str(item.target_dir) if item.target_dir else None,
                    "photo_count": len(item.photos),
                    "photos": [
                        {
                            "source_path": str(p.source_path),
                            "file_name": p.file_name,
                            "file_size": p.file_size,
                        }
                        for p in item.photos
                    ],
                    "status": item.status,
                }
                for item in plan.items
            ],
            "conflicts": plan.conflicts,
            "missing_evidence": plan.missing_evidence,
            "unregistered": plan.unregistered,
            "errors": plan.errors,
        }

        state.plan = plan_dict
        state.updated_at = datetime.now()
        self._save_state(state)
        self._log(batch_id, f"执行计划已保存，共 {len(plan.items)} 个项目")

    def add_operation(self, batch_id: str, operation: Dict[str, Any]) -> None:
        """添加操作记录"""
        state = self.get_batch(batch_id)
        state.operations.append(operation)
        state.updated_at = datetime.now()
        self._save_state(state)

    def add_operations(self, batch_id: str, operations: List[Dict[str, Any]]) -> None:
        """批量添加操作记录"""
        if not operations:
            return
        state = self.get_batch(batch_id)
        state.operations.extend(operations)
        state.updated_at = datetime.now()
        self._save_state(state)
        self._log(batch_id, f"已记录 {len(operations)} 个操作")

    def add_error(self, batch_id: str, error: str) -> None:
        """添加错误记录"""
        state = self.get_batch(batch_id)
        state.errors.append(error)
        state.updated_at = datetime.now()
        self._save_state(state)
        self._log(batch_id, f"错误: {error}")

    def get_batch(self, batch_id: str) -> BatchState:
        """获取批次状态"""
        state_file = self._get_state_file(batch_id)
        if not state_file.exists():
            raise StateError(f"批次不存在: {batch_id}")
        return self._load_state(state_file)

    def list_batches(self, status_filter: Optional[BatchStatus] = None) -> List[BatchState]:
        """列出所有批次

        Args:
            status_filter: 可选的状态过滤器
        """
        batches: List[BatchState] = []

        for state_file in sorted(self.state_dir.glob("*.json")):
            try:
                state = self._load_state(state_file)
                if status_filter is None or state.status == status_filter:
                    batches.append(state)
            except Exception as e:
                logger.warning(f"无法加载状态文件 {state_file}: {e}")

        return sorted(batches, key=lambda b: b.created_at, reverse=True)

    def can_execute(self, batch_id: str) -> bool:
        """检查批次是否可以执行"""
        try:
            state = self.get_batch(batch_id)
            return state.status in (
                BatchStatus.PENDING,
                BatchStatus.PLANNED,
                BatchStatus.FAILED,
                BatchStatus.PARTIAL,
            )
        except StateError:
            return True

    def can_rollback(self, batch_id: str) -> bool:
        """检查批次是否可以回滚"""
        try:
            state = self.get_batch(batch_id)
            return state.status in (
                BatchStatus.COMPLETED,
                BatchStatus.PARTIAL,
                BatchStatus.FAILED,
                BatchStatus.ROLLBACK_FAILED,
            ) and len(state.operations) > 0
        except StateError:
            return False

    def get_logs(self, batch_id: str, tail: Optional[int] = None) -> List[str]:
        """获取批次日志"""
        log_file = self._get_log_file(batch_id)
        if not log_file.exists():
            raise StateError(f"批次日志不存在: {batch_id}")

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if tail is not None:
            lines = lines[-tail:]

        return [line.rstrip("\n") for line in lines]

    def config_to_dict(self, config: AppConfig, csv_path: Path, csv_hash: str = "") -> Dict[str, Any]:
        """将配置转换为可序列化的字典"""
        return {
            "source_root": str(config.source_root),
            "target_root": str(config.target_root),
            "archive_root": str(config.archive_root) if config.archive_root else None,
            "operation": config.operation.value,
            "photo_extensions": config.photo_extensions,
            "dir_pattern": config.dir_pattern,
            "filename_pattern": config.filename_pattern,
            "state_dir": str(config.state_dir),
            "log_dir": str(config.log_dir),
            "report_dir": str(config.report_dir),
            "csv_path": str(csv_path),
            "csv_hash": csv_hash,
        }

    def _load_state(self, state_file: Path) -> BatchState:
        """从文件加载状态"""
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            return BatchState(
                batch_id=data["batch_id"],
                status=BatchStatus(data["status"]),
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                config=data.get("config", {}),
                plan=data.get("plan"),
                operations=data.get("operations", []),
                errors=data.get("errors", []),
            )
        except (KeyError, ValueError) as e:
            raise StateError(f"状态文件格式错误: {e}") from e
        except json.JSONDecodeError as e:
            raise StateError(f"状态文件 JSON 解析失败: {e}") from e

    def _save_state(self, state: BatchState) -> None:
        """保存状态到文件"""
        state_file = self._get_state_file(state.batch_id)
        temp_file = state_file.with_suffix(".tmp")

        data = {
            "batch_id": state.batch_id,
            "status": state.status.value,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "config": state.config,
            "plan": state.plan,
            "operations": state.operations,
            "errors": state.errors,
        }

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            temp_file.replace(state_file)
        except Exception as e:
            if temp_file.exists():
                temp_file.unlink()
            raise StateError(f"保存状态失败: {e}") from e

    def delete_batch(self, batch_id: str) -> None:
        """删除批次及其相关文件（用于清理失败的半成品批次）"""
        import logging
        for handler in logging.root.handlers[:]:
            try:
                handler.close()
                logging.root.removeHandler(handler)
            except:
                pass

        state_file = self._get_state_file(batch_id)
        log_file = self._get_log_file(batch_id)

        deleted = []
        failed = []

        if state_file.exists():
            try:
                state_file.unlink()
                deleted.append(str(state_file))
            except Exception as e:
                failed.append(f"{state_file}: {e}")

        if log_file.exists():
            try:
                log_file.unlink()
                deleted.append(str(log_file))
            except Exception as e:
                failed.append(f"{log_file}: {e}")

        if deleted:
            logger.info(f"已清理批次 {batch_id} 的文件: {', '.join(deleted)}")
        if failed:
            logger.warning(f"未能清理以下文件（可能被占用）: {', '.join(failed)}")

    def _log(self, batch_id: str, message: str) -> None:
        """写入日志"""
        log_file = self._get_log_file(batch_id)
        timestamp = datetime.now().isoformat(timespec="seconds")

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
