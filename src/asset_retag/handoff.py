"""交接包模块 - 资产重贴工作的可审计交接包管理"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import AppConfig, BatchState, BatchStatus
from .state import StateManager, StateError

logger = logging.getLogger(__name__)


class HandoffError(Exception):
    """交接包管理错误"""
    pass


class HandoffFormatError(HandoffError):
    """交接包格式错误"""
    pass


class HandoffConflictError(HandoffError):
    """交接包冲突错误"""
    pass


class HandoffNotFoundError(HandoffError):
    """交接包不存在错误"""
    pass


HANDOFF_VERSION = "1.0"


class Handoff:
    """交接包数据类"""

    def __init__(
        self,
        handoff_id: str,
        batch_id: str,
        created_at: datetime,
        updated_at: datetime,
        config_summary: Dict[str, Any],
        batch_status: str,
        report_index: List[Dict[str, Any]],
        recent_logs: List[str],
        operations_count: int = 0,
        errors_count: int = 0,
        note: str = "",
    ):
        self.handoff_id = handoff_id
        self.batch_id = batch_id
        self.created_at = created_at
        self.updated_at = updated_at
        self.config_summary = config_summary
        self.batch_status = batch_status
        self.report_index = report_index
        self.recent_logs = recent_logs
        self.operations_count = operations_count
        self.errors_count = errors_count
        self.note = note

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handoff_version": HANDOFF_VERSION,
            "handoff_id": self.handoff_id,
            "batch_id": self.batch_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "config_summary": self.config_summary,
            "batch_status": self.batch_status,
            "report_index": self.report_index,
            "recent_logs": self.recent_logs,
            "operations_count": self.operations_count,
            "errors_count": self.errors_count,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Handoff":
        return cls(
            handoff_id=data["handoff_id"],
            batch_id=data["batch_id"],
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            config_summary=data.get("config_summary", {}),
            batch_status=data.get("batch_status", ""),
            report_index=data.get("report_index", []),
            recent_logs=data.get("recent_logs", []),
            operations_count=data.get("operations_count", 0),
            errors_count=data.get("errors_count", 0),
            note=data.get("note", ""),
        )


class HandoffManager:
    """交接包管理器"""

    def __init__(self, config: Optional[AppConfig] = None):
        if config is None:
            self.state_dir = Path.home() / ".asset-retag" / "state"
            self.log_dir = Path.home() / ".asset-retag" / "logs"
            self.report_dir = Path.cwd() / "reports"
        else:
            self.state_dir = config.state_dir
            self.log_dir = config.log_dir
            self.report_dir = config.report_dir

        self.handoffs_dir = self.state_dir / "handoffs"
        self.operations_log = self.handoffs_dir / "handoff_operations.log"

        try:
            self.handoffs_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise HandoffError(
                f"无权限创建交接包目录 {self.handoffs_dir}: {e}"
            ) from e
        except Exception as e:
            raise HandoffError(f"创建交接包目录失败: {e}") from e

        self._ensure_operations_log()

    def _ensure_operations_log(self) -> None:
        """确保操作日志文件存在"""
        if not self.operations_log.exists():
            try:
                self.operations_log.touch()
            except PermissionError as e:
                raise HandoffError(
                    f"无权限创建操作日志文件 {self.operations_log}: {e}"
                ) from e

    def _get_handoff_file(self, handoff_id: str) -> Path:
        """获取交接包文件路径"""
        return self.handoffs_dir / f"{handoff_id}.json"

    def _generate_handoff_id(self) -> str:
        """生成唯一交接包 ID"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_uuid = uuid.uuid4().hex[:8]
        return f"handoff_{timestamp}_{short_uuid}"

    def _atomic_write_json(self, file_path: Path, data: Dict[str, Any]) -> None:
        """原子写入 JSON 文件"""
        temp_file = file_path.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(file_path)
        except PermissionError as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise HandoffError(
                f"无权限写入文件 {file_path}: {e}"
            ) from e
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise HandoffError(f"写入文件失败 {file_path}: {e}") from e

    def _log_operation(self, operation: str, details: Dict[str, Any]) -> None:
        """记录交接包操作日志"""
        timestamp = datetime.now().isoformat(timespec="seconds")
        log_entry = {
            "timestamp": timestamp,
            "operation": operation,
            "details": details,
        }
        try:
            with open(self.operations_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except PermissionError as e:
            raise HandoffError(
                f"无权限写入交接包操作日志: {e}"
            ) from e
        except Exception as e:
            raise HandoffError(f"写入交接包操作日志失败: {e}") from e

    def _collect_report_index(self, batch_id: str) -> List[Dict[str, Any]]:
        """收集批次相关的报告索引"""
        report_index = []
        if not self.report_dir.exists():
            return report_index

        for pattern in [f"{batch_id}_*", f"*{batch_id}*"]:
            for report_path in sorted(self.report_dir.glob(pattern)):
                if report_path.is_file():
                    try:
                        stat = report_path.stat()
                        report_index.append({
                            "name": report_path.name,
                            "path": str(report_path),
                            "size": stat.st_size,
                            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        })
                    except Exception:
                        report_index.append({
                            "name": report_path.name,
                            "path": str(report_path),
                            "size": 0,
                            "mtime": "",
                        })

        seen_paths = set()
        unique_reports = []
        for r in report_index:
            if r["path"] not in seen_paths:
                seen_paths.add(r["path"])
                unique_reports.append(r)
        return unique_reports

    def _collect_recent_logs(self, batch_id: str, tail: int = 100) -> List[str]:
        """收集批次最近日志"""
        log_file = self.log_dir / f"{batch_id}.log"
        if not log_file.exists():
            return []

        try:
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except PermissionError as e:
            raise HandoffError(
                f"无权限读取日志文件 {log_file}: {e}"
            ) from e
        except Exception as e:
            raise HandoffError(f"读取日志文件失败: {e}") from e

        if tail is not None and len(lines) > tail:
            lines = lines[-tail:]

        return [line.rstrip("\n") for line in lines]

    def create_from_batch(
        self,
        batch_id: str,
        state_manager: StateManager,
        note: str = "",
        handoff_id: Optional[str] = None,
    ) -> Handoff:
        """从批次创建交接包

        Args:
            batch_id: 批次 ID
            state_manager: StateManager 实例，用于读取批次状态
            note: 交接备注
            handoff_id: 指定交接包 ID（可选，自动生成）

        Returns:
            创建的 Handoff 对象

        Raises:
            StateError: 批次不存在
            HandoffConflictError: 同名交接包已存在
            HandoffError: 其他错误（权限不足等）
        """
        try:
            batch_state: BatchState = state_manager.get_batch(batch_id)
        except StateError:
            raise

        if handoff_id is None:
            handoff_id = self._generate_handoff_id()

        handoff_file = self._get_handoff_file(handoff_id)
        if handoff_file.exists():
            raise HandoffConflictError(
                f"交接包 '{handoff_id}' 已存在。"
            )

        config_summary = {
            "source_root": batch_state.config.get("source_root", ""),
            "target_root": batch_state.config.get("target_root", ""),
            "archive_root": batch_state.config.get("archive_root", ""),
            "operation": batch_state.config.get("operation", ""),
            "state_dir": batch_state.config.get("state_dir", str(self.state_dir)),
            "log_dir": batch_state.config.get("log_dir", str(self.log_dir)),
            "report_dir": batch_state.config.get("report_dir", str(self.report_dir)),
            "csv_path": batch_state.config.get("csv_path", ""),
        }

        report_index = self._collect_report_index(batch_id)
        recent_logs = self._collect_recent_logs(batch_id, tail=100)

        now = datetime.now()
        handoff = Handoff(
            handoff_id=handoff_id,
            batch_id=batch_id,
            created_at=now,
            updated_at=now,
            config_summary=config_summary,
            batch_status=batch_state.status.value,
            report_index=report_index,
            recent_logs=recent_logs,
            operations_count=len(batch_state.operations),
            errors_count=len(batch_state.errors),
            note=note,
        )

        self._atomic_write_json(handoff_file, handoff.to_dict())

        self._log_operation("create", {
            "handoff_id": handoff_id,
            "batch_id": batch_id,
            "batch_status": batch_state.status.value,
            "operations_count": len(batch_state.operations),
            "errors_count": len(batch_state.errors),
            "report_count": len(report_index),
            "note": note,
        })

        logger.info(f"已创建交接包 '{handoff_id}' 来自批次 '{batch_id}'")
        return handoff

    def list_handoffs(self) -> List[Handoff]:
        """列出所有交接包

        Returns:
            交接包列表，按创建时间降序
        """
        handoffs: List[Handoff] = []

        for handoff_file in sorted(self.handoffs_dir.glob("*.json")):
            if handoff_file.name.endswith("_operations.log"):
                continue
            try:
                with open(handoff_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                handoffs.append(Handoff.from_dict(data))
            except json.JSONDecodeError as e:
                logger.warning(f"交接包文件损坏 {handoff_file}: {e}")
            except (KeyError, ValueError) as e:
                logger.warning(f"无法解析交接包文件 {handoff_file}: {e}")
            except PermissionError as e:
                logger.warning(f"无权限读取交接包文件 {handoff_file}: {e}")
            except Exception as e:
                logger.warning(f"无法加载交接包 {handoff_file}: {e}")

        return sorted(handoffs, key=lambda h: h.created_at, reverse=True)

    def get_handoff(self, handoff_id: str) -> Handoff:
        """获取指定交接包

        Args:
            handoff_id: 交接包 ID

        Returns:
            Handoff 对象

        Raises:
            HandoffNotFoundError: 交接包不存在
            HandoffFormatError: 交接包格式损坏
            HandoffError: 其他错误（权限不足等）
        """
        handoff_file = self._get_handoff_file(handoff_id)
        if not handoff_file.exists():
            raise HandoffNotFoundError(f"交接包不存在: {handoff_id}")

        try:
            with open(handoff_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise HandoffFormatError(
                f"交接包文件 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise HandoffError(
                f"无权限读取交接包文件 {handoff_file}: {e}"
            ) from e
        except Exception as e:
            raise HandoffError(f"读取交接包文件失败: {e}") from e

        self._validate_handoff_data(data)

        try:
            return Handoff.from_dict(data)
        except (KeyError, ValueError) as e:
            raise HandoffFormatError(
                f"交接包数据格式错误: {e}"
            ) from e

    def _validate_handoff_data(self, data: Dict[str, Any]) -> None:
        """验证交接包数据格式"""
        required_fields = [
            "handoff_version", "handoff_id", "batch_id",
            "created_at", "updated_at",
        ]
        for field in required_fields:
            if field not in data:
                raise HandoffFormatError(f"交接包缺少必填字段: {field}")

        if data.get("handoff_version") != HANDOFF_VERSION:
            raise HandoffFormatError(
                f"不支持的交接包版本: {data.get('handoff_version')}。"
                f"当前支持版本: {HANDOFF_VERSION}"
            )

        try:
            datetime.fromisoformat(data["created_at"])
            datetime.fromisoformat(data["updated_at"])
        except ValueError as e:
            raise HandoffFormatError(f"交接包中的日期时间格式无效: {e}") from e

    def remove_handoff(self, handoff_id: str) -> None:
        """删除交接包

        Args:
            handoff_id: 交接包 ID

        Raises:
            HandoffNotFoundError: 交接包不存在
            HandoffError: 删除失败或权限不足
        """
        handoff = self.get_handoff(handoff_id)
        handoff_file = self._get_handoff_file(handoff_id)

        try:
            handoff_file.unlink()
        except PermissionError as e:
            raise HandoffError(
                f"无权限删除交接包文件 {handoff_file}: {e}"
            ) from e
        except Exception as e:
            raise HandoffError(f"删除交接包文件失败: {e}") from e

        self._log_operation("remove", {
            "handoff_id": handoff_id,
            "batch_id": handoff.batch_id,
        })

        logger.info(f"已删除交接包: {handoff_id}")

    def export_handoff(
        self,
        handoff_id: str,
        output_path: str | Path,
        overwrite: bool = False,
    ) -> Path:
        """导出交接包到 JSON 文件

        Args:
            handoff_id: 交接包 ID
            output_path: 输出文件或目录路径
            overwrite: 是否覆盖已存在的文件

        Returns:
            导出文件路径

        Raises:
            HandoffNotFoundError: 交接包不存在
            HandoffConflictError: 输出文件已存在且未指定 overwrite
            HandoffError: 其他错误（权限不足等）
        """
        handoff = self.get_handoff(handoff_id)
        output_path = Path(output_path).resolve()

        if output_path.exists() and output_path.is_dir():
            output_path = output_path / f"{handoff_id}_handoff.json"

        if output_path.exists() and not overwrite:
            raise HandoffConflictError(
                f"输出文件已存在: {output_path}。"
                f"如需覆盖，请使用 --overwrite 参数进行原子替换。"
            )

        export_data = handoff.to_dict()
        export_data["exported_at"] = datetime.now().isoformat()

        self._atomic_write_json(output_path, export_data)

        self._log_operation("export", {
            "handoff_id": handoff_id,
            "batch_id": handoff.batch_id,
            "output_path": str(output_path),
            "overwrite": overwrite,
        })

        logger.info(f"交接包 '{handoff_id}' 已导出到: {output_path}")
        return output_path

    def import_handoff(
        self,
        import_path: str | Path,
        overwrite: bool = False,
    ) -> Handoff:
        """从 JSON 文件导入交接包

        Args:
            import_path: 导入文件路径
            overwrite: 是否覆盖同名交接包（原子替换）

        Returns:
            导入的 Handoff 对象

        Raises:
            HandoffFormatError: 导入文件格式错误
            HandoffConflictError: 同名交接包已存在且未指定 overwrite
            HandoffError: 其他错误（权限不足等）
        """
        import_path = Path(import_path).resolve()
        if not import_path.exists():
            raise HandoffError(f"导入文件不存在: {import_path}")

        try:
            with open(import_path, "r", encoding="utf-8") as f:
                import_data = json.load(f)
        except json.JSONDecodeError as e:
            raise HandoffFormatError(
                f"交接包 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise HandoffError(
                f"无权限读取导入文件 {import_path}: {e}"
            ) from e
        except Exception as e:
            raise HandoffFormatError(
                f"读取导入文件失败: {e}"
            ) from e

        self._validate_handoff_data(import_data)

        try:
            handoff = Handoff.from_dict(import_data)
        except (KeyError, ValueError) as e:
            raise HandoffFormatError(
                f"交接包数据格式错误: {e}"
            ) from e

        handoff_id = handoff.handoff_id
        handoff_file = self._get_handoff_file(handoff_id)

        if handoff_file.exists() and not overwrite:
            raise HandoffConflictError(
                f"交接包 '{handoff_id}' 已存在。"
                f"如需覆盖，请使用 --overwrite 参数进行原子替换。"
            )

        handoff.updated_at = datetime.now()
        self._atomic_write_json(handoff_file, handoff.to_dict())

        self._log_operation("import", {
            "handoff_id": handoff_id,
            "batch_id": handoff.batch_id,
            "import_path": str(import_path),
            "overwrite": overwrite,
        })

        logger.info(f"已导入交接包 '{handoff_id}'")
        return handoff
