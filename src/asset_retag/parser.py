"""解析模块 - 配置解析和 CSV 映射解析"""
import csv
import logging
from pathlib import Path
from typing import List, Tuple, Dict, Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from .models import (
    AppConfig,
    AssetMapping,
    AssetType,
    OperationType,
)

logger = logging.getLogger(__name__)


class ConfigSchema(BaseModel):
    """配置验证 Schema"""
    source_root: str
    target_root: str
    archive_root: str | None = None
    operation: str = Field(default="copy", pattern="^(copy|move)$")
    photo_extensions: List[str] = Field(default_factory=lambda: ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "heic", "raw"])
    dir_pattern: str = Field(default="{asset_type}/{new_tag}")
    filename_pattern: str = Field(default="{new_tag}_{idx:04d}.{ext}")
    state_dir: str | None = None
    log_dir: str | None = None
    report_dir: str | None = None


class CSVMappingSchema(BaseModel):
    """CSV 映射验证 Schema"""
    old_id: str
    new_tag: str
    asset_type: str
    photo_dir: str


class ParseError(Exception):
    """解析错误"""
    pass


class ConfigParser:
    """配置解析器"""

    @staticmethod
    def parse(config_path: Path) -> AppConfig:
        """解析 YAML 配置文件"""
        if not config_path.exists():
            raise ParseError(f"配置文件不存在: {config_path}")

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw_config = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ParseError(f"YAML 解析失败: {e}") from e
        except UnicodeDecodeError:
            raise ParseError(f"配置文件编码错误，请使用 UTF-8 编码")

        if not isinstance(raw_config, dict):
            raise ParseError("配置文件格式错误：根节点必须是字典")

        try:
            validated = ConfigSchema(**raw_config)
        except ValidationError as e:
            errors = []
            for err in e.errors():
                field_name = ".".join(str(loc) for loc in err["loc"])
                errors.append(f"  - {field_name}: {err['msg']}")
            raise ParseError("配置字段错误:\n" + "\n".join(errors)) from e

        try:
            source_root = Path(validated.source_root).resolve()
            target_root = Path(validated.target_root).resolve()

            if not source_root.exists():
                raise ParseError(f"源根目录不存在: {source_root}")
            if not source_root.is_dir():
                raise ParseError(f"源根路径不是目录: {source_root}")

            config = AppConfig(
                source_root=source_root,
                target_root=target_root,
                archive_root=Path(validated.archive_root).resolve() if validated.archive_root else None,
                operation=OperationType(validated.operation),
                photo_extensions=[ext.lower().lstrip(".") for ext in validated.photo_extensions],
                dir_pattern=validated.dir_pattern,
                filename_pattern=validated.filename_pattern,
            )

            if validated.state_dir:
                config.state_dir = Path(validated.state_dir).resolve()
            if validated.log_dir:
                config.log_dir = Path(validated.log_dir).resolve()
            if validated.report_dir:
                config.report_dir = Path(validated.report_dir).resolve()

            if config.archive_root:
                if not config.archive_root.exists():
                    config.archive_root.mkdir(parents=True, exist_ok=True)

            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.log_dir.mkdir(parents=True, exist_ok=True)
            config.report_dir.mkdir(parents=True, exist_ok=True)

            return config

        except ParseError:
            raise
        except Exception as e:
            raise ParseError(f"配置处理失败: {e}") from e


class CSVMappingParser:
    """CSV 映射解析器"""

    REQUIRED_COLUMNS = {"old_id", "new_tag", "asset_type", "photo_dir"}

    @classmethod
    def parse(cls, csv_path: Path, source_root: Path) -> Tuple[List[AssetMapping], List[str]]:
        """解析 CSV 映射文件

        Returns:
            (mappings, errors) - 成功解析的映射列表和错误信息列表
        """
        if not csv_path.exists():
            raise ParseError(f"CSV 映射文件不存在: {csv_path}")

        mappings: List[AssetMapping] = []
        errors: List[str] = []

        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)

                if reader.fieldnames is None:
                    raise ParseError("CSV 文件为空或格式错误")

                header_set = {h.strip().lower() for h in reader.fieldnames}
                missing_columns = cls.REQUIRED_COLUMNS - header_set
                if missing_columns:
                    raise ParseError(
                        f"CSV 缺少必要列: {', '.join(sorted(missing_columns))}. "
                        f"必要列包括: {', '.join(sorted(cls.REQUIRED_COLUMNS))}"
                    )

                header_map = {h.lower().strip(): h for h in reader.fieldnames}

                seen_old_ids: Dict[str, int] = {}
                seen_new_tags: Dict[str, int] = {}

                for line_num, row in enumerate(reader, start=2):
                    try:
                        raw_data = {
                            "old_id": str(row[header_map["old_id"]]).strip(),
                            "new_tag": str(row[header_map["new_tag"]]).strip(),
                            "asset_type": str(row[header_map["asset_type"]]).strip().lower(),
                            "photo_dir": str(row[header_map["photo_dir"]]).strip(),
                        }

                        validated = CSVMappingSchema(**raw_data)

                        asset_type = cls._parse_asset_type(validated.asset_type, line_num)
                        photo_dir = cls._resolve_photo_dir(validated.photo_dir, source_root, line_num)

                        mapping = AssetMapping(
                            old_id=validated.old_id,
                            new_tag=validated.new_tag,
                            asset_type=asset_type,
                            photo_dir=photo_dir,
                            raw=raw_data,
                        )

                        if not mapping.old_id:
                            errors.append(f"行 {line_num}: old_id 不能为空")
                            continue
                        if not mapping.new_tag:
                            errors.append(f"行 {line_num}: new_tag 不能为空")
                            continue

                        if mapping.old_id in seen_old_ids:
                            errors.append(
                                f"行 {line_num}: 重复的旧编号 '{mapping.old_id}'，首次出现在行 {seen_old_ids[mapping.old_id]}"
                            )
                        else:
                            seen_old_ids[mapping.old_id] = line_num

                        if mapping.new_tag in seen_new_tags:
                            errors.append(
                                f"行 {line_num}: 重复的新标签 '{mapping.new_tag}'，首次出现在行 {seen_new_tags[mapping.new_tag]}"
                            )
                        else:
                            seen_new_tags[mapping.new_tag] = line_num

                        mappings.append(mapping)

                    except ValidationError as e:
                        for err in e.errors():
                            field_name = ".".join(str(loc) for loc in err["loc"])
                            errors.append(f"行 {line_num}: {field_name}: {err['msg']}")
                    except ParseError as e:
                        errors.append(f"行 {line_num}: {e}")
                    except Exception as e:
                        errors.append(f"行 {line_num}: 未知错误 - {e}")

        except ParseError:
            raise
        except UnicodeDecodeError:
            raise ParseError("CSV 文件编码错误，请使用 UTF-8 编码（可带 BOM）")
        except csv.Error as e:
            raise ParseError(f"CSV 解析失败: {e}") from e

        return mappings, errors

    @staticmethod
    def _parse_asset_type(value: str, line_num: int) -> AssetType:
        """解析资产类型"""
        try:
            return AssetType(value)
        except ValueError:
            valid_types = ", ".join(t.value for t in AssetType)
            raise ParseError(
                f"无效的资产类型 '{value}'，有效值为: {valid_types}"
            )

    @staticmethod
    def _resolve_photo_dir(photo_dir: str, source_root: Path, line_num: int) -> Path:
        """解析并验证照片目录"""
        p = Path(photo_dir)
        if p.is_absolute():
            resolved = p.resolve()
        else:
            resolved = (source_root / p).resolve()

        if not resolved.exists():
            raise ParseError(f"照片目录不存在: {resolved}")
        if not resolved.is_dir():
            raise ParseError(f"照片路径不是目录: {resolved}")

        return resolved
