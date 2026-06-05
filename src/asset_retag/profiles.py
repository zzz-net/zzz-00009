"""配置档案管理模块"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Profile

logger = logging.getLogger(__name__)


class ProfileError(Exception):
    """档案管理错误"""
    pass


class ProfileFormatError(ProfileError):
    """档案格式错误"""
    pass


class ProfileConflictError(ProfileError):
    """档案冲突错误"""
    pass


class ProfileNotFoundError(ProfileError):
    """档案不存在错误"""
    pass


class ProfileOperationError(ProfileError):
    """档案操作错误"""
    pass


PROFILES_VERSION = "1.0"


class ProfileManager:
    """配置档案管理器"""

    def __init__(self, base_dir: Optional[Path] = None):
        if base_dir is None:
            base_dir = Path.home() / ".asset-retag"
        self.base_dir = Path(base_dir).resolve()
        self.profiles_dir = self.base_dir / "profiles"
        self.profiles_file = self.profiles_dir / "profiles.json"
        self.operations_log = self.profiles_dir / "profile_operations.log"
        self.undo_stack_file = self.profiles_dir / "undo_stack.json"

        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        self._ensure_storage()

    def _ensure_storage(self) -> None:
        """确保存储文件存在"""
        if not self.profiles_file.exists():
            initial_data = {
                "version": PROFILES_VERSION,
                "default_profile": None,
                "profiles": {},
            }
            self._atomic_write_json(self.profiles_file, initial_data)

        if not self.undo_stack_file.exists():
            self._atomic_write_json(self.undo_stack_file, {"stack": []})

        if not self.operations_log.exists():
            self.operations_log.touch()

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
            raise ProfileError(f"无权限写入文件 {file_path}: {e}") from e
        except Exception as e:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except Exception:
                    pass
            raise ProfileError(f"写入文件失败 {file_path}: {e}") from e

    def _read_profiles_data(self) -> Dict[str, Any]:
        """读取档案数据"""
        try:
            with open(self.profiles_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ProfileFormatError(
                f"档案数据文件 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise ProfileError(
                f"无权限读取档案数据文件: {e}"
            ) from e

        version = data.get("version")
        if version != PROFILES_VERSION:
            raise ProfileFormatError(
                f"不支持的档案数据版本: {version}。当前支持版本: {PROFILES_VERSION}"
            )

        return data

    def _write_profiles_data(self, data: Dict[str, Any]) -> None:
        """写入档案数据"""
        self._atomic_write_json(self.profiles_file, data)

    def _log_operation(self, operation: str, details: Dict[str, Any]) -> None:
        """记录档案操作日志"""
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
            raise ProfileError(
                f"无权限写入档案操作日志: {e}"
            ) from e
        except Exception as e:
            raise ProfileError(f"写入档案操作日志失败: {e}") from e

    def _read_undo_stack(self) -> List[Dict[str, Any]]:
        """读取撤销栈"""
        try:
            with open(self.undo_stack_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"撤销栈文件损坏，将重置: {e}")
            return []
        except Exception:
            return []
        return data.get("stack", [])

    def _write_undo_stack(self, stack: List[Dict[str, Any]]) -> None:
        """写入撤销栈"""
        self._atomic_write_json(self.undo_stack_file, {"stack": stack})

    def _push_undo(self, operation: str, before: Dict[str, Any], after: Dict[str, Any]) -> None:
        """推入撤销记录"""
        stack = self._read_undo_stack()
        stack.append({
            "operation": operation,
            "before": before,
            "after": after,
            "timestamp": datetime.now().isoformat(),
        })
        if len(stack) > 50:
            stack = stack[-50:]
        self._write_undo_stack(stack)

    def _validate_config_path(self, config_path: Path) -> None:
        """验证配置文件路径"""
        config_path = Path(config_path).resolve()
        if not config_path.exists():
            raise ProfileError(f"配置文件不存在: {config_path}")
        if not config_path.is_file():
            raise ProfileError(f"配置路径不是文件: {config_path}")

    def add_profile(
        self,
        name: str,
        config_path: str | Path,
        description: str = "",
    ) -> Profile:
        """添加新档案

        Args:
            name: 档案名称
            config_path: 配置文件路径
            description: 档案描述

        Returns:
            创建的 Profile 对象

        Raises:
            ProfileConflictError: 同名档案已存在
            ProfileError: 配置文件不存在或权限不足
        """
        config_path = Path(config_path).resolve()
        self._validate_config_path(config_path)

        data = self._read_profiles_data()
        profiles: Dict[str, Any] = data.get("profiles", {})

        if name in profiles:
            raise ProfileConflictError(
                f"档案 '{name}' 已存在。如需覆盖，请使用 --overwrite 参数。"
            )

        now = datetime.now()
        profile = Profile(
            name=name,
            config_path=config_path,
            created_at=now,
            updated_at=now,
            description=description,
        )

        profiles[name] = profile.to_dict()
        data["profiles"] = profiles
        self._write_profiles_data(data)

        self._log_operation("add", {
            "name": name,
            "config_path": str(config_path),
            "description": description,
        })

        logger.info(f"已添加档案 '{name}': {config_path}")
        return profile

    def list_profiles(self) -> List[Profile]:
        """列出所有档案

        Returns:
            所有档案列表，按创建时间降序
        """
        data = self._read_profiles_data()
        profiles_data: Dict[str, Any] = data.get("profiles", {})

        profiles = []
        for name, pd in profiles_data.items():
            try:
                profiles.append(Profile.from_dict(pd))
            except Exception as e:
                logger.warning(f"无法加载档案 '{name}': {e}")

        return sorted(profiles, key=lambda p: p.created_at, reverse=True)

    def get_profile(self, name: str) -> Profile:
        """获取指定档案

        Args:
            name: 档案名称

        Returns:
            Profile 对象

        Raises:
            ProfileNotFoundError: 档案不存在
        """
        data = self._read_profiles_data()
        profiles_data: Dict[str, Any] = data.get("profiles", {})

        if name not in profiles_data:
            raise ProfileNotFoundError(f"档案不存在: {name}")

        return Profile.from_dict(profiles_data[name])

    def get_default_profile(self) -> Optional[Profile]:
        """获取当前默认档案

        Returns:
            默认 Profile 对象，若无则返回 None
        """
        data = self._read_profiles_data()
        default_name = data.get("default_profile")
        if not default_name:
            return None
        try:
            return self.get_profile(default_name)
        except ProfileNotFoundError:
            return None

    def use_profile(self, name: str) -> Profile:
        """设置默认档案

        Args:
            name: 档案名称

        Returns:
            设置后的默认 Profile 对象

        Raises:
            ProfileNotFoundError: 档案不存在
        """
        profile = self.get_profile(name)

        data = self._read_profiles_data()
        old_default = data.get("default_profile")

        before = {"default_profile": old_default}
        after = {"default_profile": name}

        data["default_profile"] = name
        self._write_profiles_data(data)

        self._push_undo("use", before, after)

        self._log_operation("use", {
            "name": name,
            "previous_default": old_default,
        })

        logger.info(f"已设置默认档案: {name}")
        return profile

    def remove_profile(self, name: str) -> None:
        """删除档案

        Args:
            name: 档案名称

        Raises:
            ProfileNotFoundError: 档案不存在
        """
        profile = self.get_profile(name)

        data = self._read_profiles_data()
        profiles_data: Dict[str, Any] = data.get("profiles", {})
        old_default = data.get("default_profile")

        before = {
            "profiles": dict(profiles_data),
            "default_profile": old_default,
        }

        del profiles_data[name]

        if old_default == name:
            data["default_profile"] = None

        data["profiles"] = profiles_data
        self._write_profiles_data(data)

        after = {
            "profiles": dict(profiles_data),
            "default_profile": data.get("default_profile"),
        }

        self._push_undo("remove", before, after)

        self._log_operation("remove", {
            "name": name,
            "config_path": str(profile.config_path),
            "was_default": old_default == name,
        })

        logger.info(f"已删除档案: {name}")

    def export_profile(self, name: str, output_path: str | Path) -> Path:
        """导出档案到 JSON 文件

        Args:
            name: 档案名称
            output_path: 输出文件路径

        Returns:
            导出文件路径

        Raises:
            ProfileNotFoundError: 档案不存在
            ProfileError: 写入失败或权限不足
        """
        profile = self.get_profile(name)
        output_path = Path(output_path).resolve()

        if output_path.exists() and output_path.is_dir():
            output_path = output_path / f"{name}_profile.json"

        export_data = {
            "profile_version": PROFILES_VERSION,
            "exported_at": datetime.now().isoformat(),
            "profile": profile.to_dict(),
        }

        self._atomic_write_json(output_path, export_data)

        self._log_operation("export", {
            "name": name,
            "output_path": str(output_path),
        })

        logger.info(f"档案 '{name}' 已导出到: {output_path}")
        return output_path

    def _validate_import_data(self, import_data: Dict[str, Any]) -> None:
        """验证导入数据格式"""
        required_fields = ["profile_version", "profile"]
        for field in required_fields:
            if field not in import_data:
                raise ProfileFormatError(f"导入数据缺少必填字段: {field}")

        if import_data["profile_version"] != PROFILES_VERSION:
            raise ProfileFormatError(
                f"不支持的档案版本: {import_data['profile_version']}。"
                f"当前支持版本: {PROFILES_VERSION}"
            )

        profile_data = import_data["profile"]
        profile_required = ["name", "config_path", "created_at", "updated_at"]
        for field in profile_required:
            if field not in profile_data:
                raise ProfileFormatError(f"导入档案数据缺少必填字段: profile.{field}")

    def import_profile(
        self,
        import_path: str | Path,
        overwrite: bool = False,
    ) -> Profile:
        """从 JSON 文件导入档案

        Args:
            import_path: 导入文件路径
            overwrite: 是否覆盖同名档案

        Returns:
            导入的 Profile 对象

        Raises:
            ProfileFormatError: 导入文件格式错误
            ProfileConflictError: 同名档案已存在且未指定 overwrite
            ProfileError: 配置文件不存在或权限不足
        """
        import_path = Path(import_path).resolve()
        if not import_path.exists():
            raise ProfileError(f"导入文件不存在: {import_path}")

        try:
            with open(import_path, "r", encoding="utf-8") as f:
                import_data = json.load(f)
        except json.JSONDecodeError as e:
            raise ProfileFormatError(
                f"导入文件 JSON 解析失败，文件可能已损坏: {e}"
            ) from e
        except PermissionError as e:
            raise ProfileError(
                f"无权限读取导入文件 {import_path}: {e}"
            ) from e

        self._validate_import_data(import_data)

        profile_data = import_data["profile"]
        name = profile_data["name"]
        config_path = Path(profile_data["config_path"]).resolve()

        self._validate_config_path(config_path)

        data = self._read_profiles_data()
        profiles_data: Dict[str, Any] = data.get("profiles", {})

        if name in profiles_data and not overwrite:
            raise ProfileConflictError(
                f"档案 '{name}' 已存在。如需覆盖，请使用 --overwrite 参数进行原子替换。"
            )

        now = datetime.now()
        profile = Profile.from_dict(profile_data)
        profile.config_path = config_path
        if name in profiles_data:
            profile.updated_at = now
        else:
            profile.created_at = now
            profile.updated_at = now

        profiles_data[name] = profile.to_dict()
        data["profiles"] = profiles_data
        self._write_profiles_data(data)

        self._log_operation("import", {
            "name": name,
            "config_path": str(config_path),
            "overwrite": overwrite,
        })

        logger.info(f"已导入档案 '{name}': {config_path}")
        return profile

    def undo_use(self) -> Optional[Dict[str, Any]]:
        """撤销最近一次 use 操作

        Returns:
            撤销操作的详情字典，包含 operation、before、after
            如果没有可撤销的操作则返回 None

        Raises:
            ProfileError: 撤销失败
        """
        stack = self._read_undo_stack()

        use_indices = [i for i, op in enumerate(stack) if op["operation"] == "use"]
        if not use_indices:
            return None

        last_idx = use_indices[-1]
        last_use = stack[last_idx]

        data = self._read_profiles_data()
        old_default = data.get("default_profile")

        data["default_profile"] = last_use["before"]["default_profile"]
        self._write_profiles_data(data)

        new_stack = [op for i, op in enumerate(stack) if i != last_idx]
        self._write_undo_stack(new_stack)

        self._log_operation("undo-use", {
            "previous_default": old_default,
            "restored_default": last_use["before"]["default_profile"],
        })

        logger.info(
            f"已撤销 use 操作，恢复默认档案为: "
            f"{last_use['before']['default_profile']}"
        )

        return last_use
