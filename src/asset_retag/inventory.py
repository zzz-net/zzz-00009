"""资产清单管理模块"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import AppConfig, Inventory, InventoryDiff, InventoryItem

logger = logging.getLogger(__name__)


class InventoryError(Exception):
    """清单管理错误"""
    pass


class InventoryFormatError(InventoryError):
    """清单格式错误"""
    pass


class InventoryConflictError(InventoryError):
    """清单冲突错误"""
    pass


class InventoryNotFoundError(InventoryError):
    """清单不存在错误"""
    pass


INVENTORY_VERSION = "1.0"
OLD_ID_PATTERN = re.compile(r"^([A-Za-z0-9_-]+?)[_-]")


class InventoryManager:
    """资产清单管理器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.state_dir = config.state_dir
        self.log_dir = config.log_dir
        self.inventory_dir = self.state_dir / "inventories"
        self.inventory_index_file = self.inventory_dir / "index.json"
        self.operations_log = self.inventory_dir / "inventory_operations.log"

        self.inventory_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_storage()

    def _ensure_storage(self) -> None:
        """确保存储文件存在"""
        if not self.inventory_index_file.exists():
            initial_data = {
                "version": INVENTORY_VERSION,
                "inventories": {},
            }
            self._atomic_write_json(self.inventory_index_file, initial_data)

        if not self.operations_log.exists():
            try:
                self.operations_log.touch()
            except PermissionError:
                pass

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
            raise InventoryError(f"无权限写入文件 {file_path}: {e}") from e
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise InventoryError(f"写入文件失败 {file_path}: {e}") from e

    def _read_index(self) -> Dict[str, Any]:
        """读取清单索引"""
        try:
            with open(self.inventory_index_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise InventoryFormatError(
                f"清单索引文件 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise InventoryError(
                f"无权限读取清单索引文件: {e}"
            ) from e

        version = data.get("version")
        if version != INVENTORY_VERSION:
            raise InventoryFormatError(
                f"不支持的清单索引版本: {version}。当前支持版本: {INVENTORY_VERSION}"
            )

        return data

    def _write_index(self, data: Dict[str, Any]) -> None:
        """写入清单索引"""
        self._atomic_write_json(self.inventory_index_file, data)

    def _get_inventory_file(self, name: str) -> Path:
        """获取清单数据文件路径"""
        return self.inventory_dir / f"{name}.json"

    def _log_operation(self, operation: str, details: Dict[str, Any]) -> None:
        """记录清单操作日志"""
        timestamp = datetime.now().isoformat(timespec="seconds")
        log_entry = {
            "timestamp": timestamp,
            "operation": operation,
            "details": details,
        }
        try:
            with open(self.operations_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except PermissionError:
            pass
        except Exception:
            pass

        inv_log_file = self.log_dir / f"inventory_{operation}.log"
        try:
            with open(inv_log_file, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {operation}: {json.dumps(details, ensure_ascii=False)}\n")
        except PermissionError:
            pass
        except Exception:
            pass

    def _extract_old_id(self, relative_path: str) -> str:
        """从相对路径中提取旧编号（目录名的前缀）"""
        parts = Path(relative_path).parts
        if len(parts) >= 1:
            dir_name = parts[0]
            match = OLD_ID_PATTERN.match(dir_name)
            if match:
                return match.group(1)
            if "_" not in dir_name and "-" not in dir_name:
                return dir_name
        return ""

    def scan(
        self,
        name: str,
        description: str = "",
        overwrite: bool = False,
    ) -> Inventory:
        """扫描 source_root 生成资产清单

        Args:
            name: 清单名称
            description: 清单描述
            overwrite: 是否覆盖同名清单

        Returns:
            生成的 Inventory 对象

        Raises:
            InventoryError: 配置的 source_root 不存在或为空目录、无写权限
            InventoryConflictError: 同名清单已存在且未指定 overwrite
        """
        source_root = self.config.source_root.resolve()

        if not source_root.exists():
            raise InventoryError(
                f"配置的 source_root 不存在: {source_root}"
            )

        if not source_root.is_dir():
            raise InventoryError(
                f"source_root 不是目录: {source_root}"
            )

        all_files = list(source_root.rglob("*"))
        file_entries = [f for f in all_files if f.is_file()]

        if not file_entries:
            raise InventoryError(
                f"source_root 目录为空，没有文件可扫描: {source_root}"
            )

        index_data = self._read_index()
        inventories_idx: Dict[str, Any] = index_data.get("inventories", {})

        if name in inventories_idx and not overwrite:
            raise InventoryConflictError(
                f"清单 '{name}' 已存在。如需覆盖，请使用 --overwrite 参数。"
            )

        items: List[InventoryItem] = []
        for file_path in file_entries:
            try:
                rel_path = str(file_path.relative_to(source_root))
                stat = file_path.stat()
                ext = file_path.suffix.lower().lstrip(".")
                old_id = self._extract_old_id(rel_path)

                items.append(InventoryItem(
                    relative_path=rel_path,
                    file_size=stat.st_size,
                    mtime=stat.st_mtime,
                    extension=ext,
                    old_id=old_id,
                ))
            except Exception as e:
                logger.warning(f"跳过文件 {file_path}: {e}")

        items.sort(key=lambda x: x.relative_path)

        now = datetime.now()
        if name in inventories_idx:
            existing_file = self._get_inventory_file(name)
            if existing_file.exists():
                try:
                    old_data = json.loads(existing_file.read_text(encoding="utf-8"))
                    created_at = datetime.fromisoformat(old_data.get("created_at", now.isoformat()))
                except Exception:
                    created_at = now
            else:
                created_at = now
        else:
            created_at = now

        inventory = Inventory(
            name=name,
            source_root=source_root,
            created_at=created_at,
            updated_at=now,
            items=items,
            description=description,
        )

        inv_file = self._get_inventory_file(name)
        self._atomic_write_json(inv_file, inventory.to_dict())

        inventories_idx[name] = {
            "name": name,
            "file": str(inv_file.name),
            "source_root": str(source_root),
            "file_count": inventory.file_count,
            "total_size": inventory.total_size,
            "created_at": inventory.created_at.isoformat(),
            "updated_at": inventory.updated_at.isoformat(),
            "description": description,
        }
        index_data["inventories"] = inventories_idx
        self._write_index(index_data)

        self._log_operation("scan", {
            "name": name,
            "source_root": str(source_root),
            "file_count": inventory.file_count,
            "total_size": inventory.total_size,
            "overwrite": overwrite,
        })

        logger.info(f"已扫描清单 '{name}': {inventory.file_count} 个文件, {inventory.total_size} 字节")
        return inventory

    def list_inventories(self) -> List[Dict[str, Any]]:
        """列出所有清单

        Returns:
            清单摘要列表，按更新时间降序
        """
        index_data = self._read_index()
        inventories_idx: Dict[str, Any] = index_data.get("inventories", {})
        result = list(inventories_idx.values())
        result.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return result

    def get_inventory(self, name: str) -> Inventory:
        """获取指定清单

        Args:
            name: 清单名称

        Returns:
            Inventory 对象

        Raises:
            InventoryNotFoundError: 清单不存在
            InventoryFormatError: 清单文件格式损坏
        """
        index_data = self._read_index()
        inventories_idx: Dict[str, Any] = index_data.get("inventories", {})

        if name not in inventories_idx:
            raise InventoryNotFoundError(f"清单不存在: {name}")

        inv_file = self._get_inventory_file(name)
        if not inv_file.exists():
            raise InventoryNotFoundError(f"清单文件不存在: {inv_file}")

        try:
            with open(inv_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise InventoryFormatError(
                f"清单文件 '{name}' JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise InventoryError(
                f"无权限读取清单文件 '{name}': {e}"
            ) from e

        return Inventory.from_dict(data)

    def remove_inventory(self, name: str) -> None:
        """删除清单

        Args:
            name: 清单名称

        Raises:
            InventoryNotFoundError: 清单不存在
        """
        index_data = self._read_index()
        inventories_idx: Dict[str, Any] = index_data.get("inventories", {})

        if name not in inventories_idx:
            raise InventoryNotFoundError(f"清单不存在: {name}")

        inv_file = self._get_inventory_file(name)
        deleted = False
        if inv_file.exists():
            try:
                inv_file.unlink()
                deleted = True
            except PermissionError as e:
                raise InventoryError(f"无权限删除清单文件: {e}") from e
            except Exception as e:
                raise InventoryError(f"删除清单文件失败: {e}") from e

        del inventories_idx[name]
        index_data["inventories"] = inventories_idx
        self._write_index(index_data)

        self._log_operation("remove", {
            "name": name,
            "file_deleted": deleted,
        })

        logger.info(f"已删除清单: {name}")

    def export_inventory(self, name: str, output_path: str | Path) -> Path:
        """导出清单到 JSON 文件

        Args:
            name: 清单名称
            output_path: 输出文件路径（如果是目录则自动生成文件名）

        Returns:
            导出文件路径

        Raises:
            InventoryNotFoundError: 清单不存在
            InventoryError: 写入失败或权限不足
        """
        inventory = self.get_inventory(name)
        output_path = Path(output_path).resolve()

        if output_path.exists() and output_path.is_dir():
            output_path = output_path / f"{name}_inventory.json"

        export_data = {
            "inventory_version": INVENTORY_VERSION,
            "exported_at": datetime.now().isoformat(),
            "inventory": inventory.to_dict(),
        }

        self._atomic_write_json(output_path, export_data)

        self._log_operation("export", {
            "name": name,
            "output_path": str(output_path),
        })

        logger.info(f"清单 '{name}' 已导出到: {output_path}")
        return output_path

    def _validate_import_data(self, import_data: Dict[str, Any]) -> None:
        """验证导入数据格式"""
        required_fields = ["inventory_version", "inventory"]
        for field in required_fields:
            if field not in import_data:
                raise InventoryFormatError(f"导入数据缺少必填字段: {field}")

        if import_data["inventory_version"] != INVENTORY_VERSION:
            raise InventoryFormatError(
                f"不支持的清单版本: {import_data['inventory_version']}。"
                f"当前支持版本: {INVENTORY_VERSION}"
            )

        inv_data = import_data["inventory"]
        inv_required = ["name", "source_root", "created_at", "updated_at", "items"]
        for field in inv_required:
            if field not in inv_data:
                raise InventoryFormatError(f"导入清单数据缺少必填字段: inventory.{field}")

        for i, item in enumerate(inv_data.get("items", [])):
            item_required = ["relative_path", "file_size", "mtime", "extension"]
            for field in item_required:
                if field not in item:
                    raise InventoryFormatError(
                        f"导入清单条目 {i} 缺少必填字段: items[{i}].{field}"
                    )

    def import_inventory(
        self,
        import_path: str | Path,
        overwrite: bool = False,
    ) -> Inventory:
        """从 JSON 文件导入清单

        Args:
            import_path: 导入文件路径
            overwrite: 是否覆盖同名清单

        Returns:
            导入的 Inventory 对象

        Raises:
            InventoryFormatError: 导入文件格式错误
            InventoryConflictError: 同名清单已存在且未指定 overwrite
            InventoryError: 无写权限
        """
        import_path = Path(import_path).resolve()
        if not import_path.exists():
            raise InventoryError(f"导入文件不存在: {import_path}")

        try:
            with open(import_path, "r", encoding="utf-8") as f:
                import_data = json.load(f)
        except json.JSONDecodeError as e:
            raise InventoryFormatError(
                f"导入文件 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise InventoryError(
                f"无权限读取导入文件 {import_path}: {e}"
            ) from e

        self._validate_import_data(import_data)

        inventory = Inventory.from_dict(import_data["inventory"])
        name = inventory.name

        index_data = self._read_index()
        inventories_idx: Dict[str, Any] = index_data.get("inventories", {})

        if name in inventories_idx and not overwrite:
            raise InventoryConflictError(
                f"清单 '{name}' 已存在。如需覆盖，请使用 --overwrite 参数进行原子替换。"
            )

        inv_file = self._get_inventory_file(name)
        self._atomic_write_json(inv_file, inventory.to_dict())

        inventories_idx[name] = {
            "name": name,
            "file": str(inv_file.name),
            "source_root": str(inventory.source_root),
            "file_count": inventory.file_count,
            "total_size": inventory.total_size,
            "created_at": inventory.created_at.isoformat(),
            "updated_at": inventory.updated_at.isoformat(),
            "description": inventory.description,
        }
        index_data["inventories"] = inventories_idx
        self._write_index(index_data)

        self._log_operation("import", {
            "name": name,
            "source": str(import_path),
            "file_count": inventory.file_count,
            "overwrite": overwrite,
        })

        logger.info(f"已导入清单 '{name}': {inventory.file_count} 个文件")
        return inventory

    def diff_inventory(
        self,
        name: str,
    ) -> InventoryDiff:
        """将指定清单与当前 source_root 目录进行比对

        Args:
            name: 清单名称

        Returns:
            InventoryDiff 比对结果，包含 added、removed、modified

        Raises:
            InventoryNotFoundError: 清单不存在
            InventoryError: source_root 不存在
        """
        inventory = self.get_inventory(name)
        source_root = self.config.source_root.resolve()

        if not source_root.exists():
            raise InventoryError(
                f"配置的 source_root 不存在: {source_root}"
            )

        inv_items: Dict[str, InventoryItem] = {
            item.relative_path: item for item in inventory.items
        }

        current_items: Dict[str, InventoryItem] = {}
        if source_root.is_dir():
            for file_path in source_root.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    rel_path = str(file_path.relative_to(source_root))
                    stat = file_path.stat()
                    ext = file_path.suffix.lower().lstrip(".")
                    old_id = self._extract_old_id(rel_path)
                    current_items[rel_path] = InventoryItem(
                        relative_path=rel_path,
                        file_size=stat.st_size,
                        mtime=stat.st_mtime,
                        extension=ext,
                        old_id=old_id,
                    )
                except Exception as e:
                    logger.warning(f"跳过文件 {file_path}: {e}")

        diff = InventoryDiff()

        for path, item in current_items.items():
            if path not in inv_items:
                diff.added.append(item)
            else:
                old_item = inv_items[path]
                if old_item.file_size != item.file_size or abs(old_item.mtime - item.mtime) > 0.001:
                    diff.modified.append({
                        "path": path,
                        "old": old_item,
                        "new": item,
                    })

        for path, item in inv_items.items():
            if path not in current_items:
                diff.removed.append(item)

        diff.added.sort(key=lambda x: x.relative_path)
        diff.removed.sort(key=lambda x: x.relative_path)
        diff.modified.sort(key=lambda x: x["path"])

        self._log_operation("diff", {
            "name": name,
            "added_count": len(diff.added),
            "removed_count": len(diff.removed),
            "modified_count": len(diff.modified),
        })

        return diff
