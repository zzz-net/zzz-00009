"""数据模型定义"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Dict, Any


class BatchStatus(str, Enum):
    """批次状态"""
    PENDING = "pending"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


class OperationType(str, Enum):
    """操作类型"""
    COPY = "copy"
    MOVE = "move"


class AssetType(str, Enum):
    """资产类型"""
    HARDWARE = "hardware"
    SOFTWARE = "software"
    DOCUMENT = "document"
    OTHER = "other"


@dataclass
class AssetMapping:
    """资产映射条目"""
    old_id: str
    new_tag: str
    asset_type: AssetType
    photo_dir: Path
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PhotoFile:
    """照片文件信息"""
    source_path: Path
    file_name: str
    file_size: int
    file_hash: Optional[str] = None


@dataclass
class AssetPlanItem:
    """计划项"""
    mapping: AssetMapping
    photos: List[PhotoFile] = field(default_factory=list)
    target_dir: Optional[Path] = None
    status: str = "pending"
    error: Optional[str] = None


@dataclass
class AppConfig:
    """应用配置"""
    source_root: Path
    target_root: Path
    archive_root: Optional[Path] = None
    operation: OperationType = OperationType.COPY
    photo_extensions: List[str] = field(default_factory=lambda: ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "heic", "raw"])
    dir_pattern: str = "{asset_type}/{new_tag}"
    filename_pattern: str = "{new_tag}_{idx:04d}.{ext}"
    state_dir: Path = field(default_factory=lambda: Path.home() / ".asset-retag" / "state")
    log_dir: Path = field(default_factory=lambda: Path.home() / ".asset-retag" / "logs")
    report_dir: Path = field(default_factory=lambda: Path.cwd() / "reports")


@dataclass
class ExecutionPlan:
    """执行计划"""
    batch_id: str
    created_at: datetime
    items: List[AssetPlanItem] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    missing_evidence: List[Dict[str, Any]] = field(default_factory=list)
    unregistered: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class BatchState:
    """批次状态记录"""
    batch_id: str
    status: BatchStatus
    created_at: datetime
    updated_at: datetime
    config: Dict[str, Any] = field(default_factory=dict)
    plan: Optional[Dict[str, Any]] = None
    operations: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
