"""文件操作模块 - 复制、归档、回滚"""
import logging
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .models import (
    AppConfig,
    AssetPlanItem,
    ExecutionPlan,
    OperationType,
    PhotoFile,
)

logger = logging.getLogger(__name__)


class FileOperationError(Exception):
    """文件操作错误"""
    pass


class FileLockedError(FileOperationError):
    """文件被锁定错误"""
    pass


class FileOperationResult:
    """文件操作结果"""

    def __init__(self, success: bool, message: str = "", source: Path = None, target: Path = None):
        self.success = success
        self.message = message
        self.source = source
        self.target = target
        self.timestamp = datetime.now()

    def __repr__(self) -> str:
        return f"FileOperationResult(success={self.success}, message='{self.message}')"


class FileOperator:
    """文件操作器"""

    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self._temp_files: List[Path] = []

    def __del__(self):
        """清理临时文件"""
        for temp_file in self._temp_files:
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except:
                pass

    @staticmethod
    def is_file_locked(path: Path) -> Tuple[bool, Optional[str]]:
        """检查文件是否被锁定

        通过尝试以独占模式打开文件来检测锁定
        """
        if not path.exists():
            return False, None

        try:
            if os.name == "nt":
                handle = os.open(str(path), os.O_RDONLY | os.O_EXCL)
                os.close(handle)
            else:
                import fcntl
                with open(path, "rb") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False, None
        except PermissionError as e:
            return True, f"文件被占用或无权限: {e}"
        except BlockingIOError as e:
            return True, f"文件被锁定: {e}"
        except OSError as e:
            if e.winerror == 32:  # ERROR_SHARING_VIOLATION
                return True, f"文件被其他进程占用 (Windows error 32)"
            return True, f"无法访问文件: {e}"
        except Exception as e:
            return True, f"检测文件锁定失败: {e}"

    def execute_plan(
        self,
        plan: ExecutionPlan,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        """执行计划

        Args:
            plan: 执行计划
            on_progress: 进度回调 (当前, 总数, 消息)

        Returns:
            (successful_ops, failed_ops)
        """
        successful_ops: List[Dict] = []
        failed_ops: List[Dict] = []

        executable_items = [item for item in plan.items if item.status == "planned" and item.photos]
        total = len(executable_items)

        logger.info(f"开始执行计划，共 {total} 个项目需要处理")

        for idx, item in enumerate(executable_items, start=1):
            mapping = item.mapping
            progress_msg = f"处理 {mapping.old_id} -> {mapping.new_tag}"

            if on_progress:
                on_progress(idx, total, progress_msg)

            logger.info(f"[{idx}/{total}] {progress_msg}")

            try:
                if not item.target_dir:
                    raise FileOperationError("目标目录未设置")

                item_ops, item_failures = self._execute_item(item)
                successful_ops.extend(item_ops)
                failed_ops.extend(item_failures)

                if item_failures:
                    logger.warning(f"项目 {mapping.new_tag} 有 {len(item_failures)} 个文件失败")

            except Exception as e:
                logger.exception(f"处理项目 {mapping.new_tag} 失败")
                failed_ops.append({
                    "old_id": mapping.old_id,
                    "new_tag": mapping.new_tag,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

        return successful_ops, failed_ops

    def _execute_item(self, item: AssetPlanItem) -> Tuple[List[Dict], List[Dict]]:
        """执行单个项目"""
        successful_ops: List[Dict] = []
        failed_ops: List[Dict] = []

        mapping = item.mapping
        target_dir = item.target_dir

        if not self.dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)

        for idx, photo in enumerate(item.photos, start=1):
            try:
                target_path = self._build_target_path(mapping, photo, idx)

                op_record = self._process_single_file(
                    source=photo.source_path,
                    target=target_path,
                    mapping=mapping,
                    photo_index=idx,
                )

                op_record["photo_index"] = idx
                op_record["old_id"] = mapping.old_id
                op_record["new_tag"] = mapping.new_tag
                op_record["asset_type"] = mapping.asset_type.value
                op_record["source_path"] = str(photo.source_path)
                op_record["target_path"] = str(target_path)
                op_record["file_size"] = photo.file_size

                successful_ops.append(op_record)

            except Exception as e:
                logger.warning(f"处理文件 {photo.source_path} 失败: {e}")
                failed_ops.append({
                    "old_id": mapping.old_id,
                    "new_tag": mapping.new_tag,
                    "photo_index": idx,
                    "source_path": str(photo.source_path),
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

        return successful_ops, failed_ops

    def _process_single_file(
        self,
        source: Path,
        target: Path,
        mapping,
        photo_index: int,
    ) -> Dict:
        """处理单个文件

        Returns:
            操作记录字典
        """
        if self.dry_run:
            return {
                "operation": self.config.operation.value,
                "dry_run": True,
                "timestamp": datetime.now().isoformat(),
            }

        if target.exists():
            locked, lock_reason = self.is_file_locked(target)
            if locked:
                raise FileOperationError(f"目标文件已存在且被占用: {lock_reason}")

        target.parent.mkdir(parents=True, exist_ok=True)

        archived_path = None
        if self.config.archive_root:
            archived_path = self._archive_source(source, mapping, photo_index)

        operation = self.config.operation
        if operation == OperationType.COPY:
            shutil.copy2(source, target)
        elif operation == OperationType.MOVE:
            shutil.move(str(source), str(target))
        else:
            raise FileOperationError(f"未知操作类型: {operation}")

        return {
            "operation": operation.value,
            "archived_path": str(archived_path) if archived_path else None,
            "timestamp": datetime.now().isoformat(),
        }

    def _archive_source(self, source: Path, mapping, photo_index: int) -> Path:
        """归档源文件"""
        if not self.config.archive_root:
            raise FileOperationError("未配置归档目录")

        archive_dir = self.config.archive_root / mapping.asset_type.value / mapping.old_id
        archive_dir.mkdir(parents=True, exist_ok=True)

        ext = source.suffix.lower().lstrip(".")
        archive_filename = f"{mapping.old_id}_{photo_index:04d}.{ext}"
        archive_path = archive_dir / archive_filename

        if archive_path.exists():
            raise FileOperationError(f"归档路径已存在: {archive_path}")

        shutil.copy2(source, archive_path)
        logger.info(f"已归档源文件到: {archive_path}")

        return archive_path

    def _build_target_path(self, mapping, photo: PhotoFile, idx: int) -> Path:
        """构建目标文件路径"""
        ext = photo.source_path.suffix.lower().lstrip(".")
        try:
            filename = self.config.filename_pattern.format(
                new_tag=mapping.new_tag,
                old_id=mapping.old_id,
                asset_type=mapping.asset_type.value,
                idx=idx,
                ext=ext,
            )
            dir_path = self.config.dir_pattern.format(
                asset_type=mapping.asset_type.value,
                new_tag=mapping.new_tag,
                old_id=mapping.old_id,
            )
            return (self.config.target_root / dir_path / filename).resolve()
        except KeyError as e:
            raise FileOperationError(f"模板包含未知变量: {e}")

    def rollback(
        self,
        operations: List[Dict],
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> Tuple[List[Dict], List[Dict]]:
        """回滚操作

        重要：回滚时如果目标文件已被占用，必须立即停止且不覆盖

        Args:
            operations: 要回滚的操作记录列表
            on_progress: 进度回调

        Returns:
            (rolled_back_ops, failed_ops)
        """
        rolled_back: List[Dict] = []
        failed_ops: List[Dict] = []

        reversed_ops = list(reversed(operations))
        total = len(reversed_ops)

        logger.info(f"开始回滚，共 {total} 个操作需要撤销")

        for idx, op in enumerate(reversed_ops, start=1):
            new_tag = op.get("new_tag", "unknown")
            photo_index = op.get("photo_index", idx)
            progress_msg = f"回滚 {new_tag} 照片 #{photo_index}"

            if on_progress:
                on_progress(idx, total, progress_msg)

            logger.info(f"[{idx}/{total}] {progress_msg}")

            try:
                source_path = Path(op["source_path"])
                target_path = Path(op["target_path"])
                archived_path = Path(op["archived_path"]) if op.get("archived_path") else None
                operation = op.get("operation", "copy")

                rollback_result = self._rollback_single_operation(
                    source_path=source_path,
                    target_path=target_path,
                    archived_path=archived_path,
                    operation=operation,
                )

                rollback_result.update({
                    "old_id": op.get("old_id"),
                    "new_tag": new_tag,
                    "photo_index": photo_index,
                    "target_path": str(target_path),
                    "source_path": str(source_path),
                })

                rolled_back.append(rollback_result)

            except FileLockedError as e:
                logger.error(f"回滚时检测到文件锁定，立即停止: {e}")
                failed_ops.append({
                    **op,
                    "error": f"文件被占用，回滚已停止: {e}",
                    "rollback_stopped": True,
                    "timestamp": datetime.now().isoformat(),
                })
                raise FileOperationError(f"回滚因文件锁定而中止: {e}") from e

            except Exception as e:
                logger.warning(f"回滚操作失败: {e}")
                failed_ops.append({
                    **op,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

        return rolled_back, failed_ops

    def _rollback_single_operation(
        self,
        source_path: Path,
        target_path: Path,
        archived_path: Optional[Path],
        operation: str,
    ) -> Dict:
        """回滚单个操作

        安全检查：
        1. 检查目标文件是否被锁定 - 如果锁定，立即抛出 FileLockedError
        2. 不覆盖任何已有文件
        """
        if self.dry_run:
            return {
                "rollback_operation": f"undo_{operation}",
                "dry_run": True,
                "timestamp": datetime.now().isoformat(),
            }

        locked, lock_reason = self.is_file_locked(target_path)
        if locked:
            raise FileLockedError(f"目标文件被占用，无法安全回滚: {lock_reason}")

        if operation == "move":
            if target_path.exists():
                if source_path.exists():
                    raise FileOperationError(
                        f"源路径已存在，无法移动回滚: {source_path}"
                    )
                source_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target_path), str(source_path))
                logger.info(f"已撤销移动: {target_path} -> {source_path}")
            else:
                logger.warning(f"目标文件不存在，跳过移动回滚: {target_path}")

        elif operation == "copy":
            if target_path.exists():
                target_path.unlink()
                logger.info(f"已删除复制的文件: {target_path}")
            else:
                logger.warning(f"目标文件不存在，跳过删除: {target_path}")

            if archived_path and archived_path.exists():
                if not source_path.exists():
                    archived_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(archived_path, source_path)
                    logger.info(f"已从归档恢复源文件: {archived_path} -> {source_path}")

        if archived_path and archived_path.exists():
            archived_path.unlink()
            logger.info(f"已清理归档文件: {archived_path}")

        return {
            "rollback_operation": f"undo_{operation}",
            "timestamp": datetime.now().isoformat(),
        }
