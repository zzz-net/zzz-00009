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


@dataclass
class Profile:
    """配置档案"""
    name: str
    config_path: Path
    created_at: datetime
    updated_at: datetime
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "config_path": str(self.config_path),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Profile":
        return cls(
            name=data["name"],
            config_path=Path(data["config_path"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            description=data.get("description", ""),
        )


@dataclass
class InventoryItem:
    """资产清单条目 - 单个文件信息"""
    relative_path: str
    file_size: int
    mtime: float
    extension: str
    old_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "file_size": self.file_size,
            "mtime": self.mtime,
            "extension": self.extension,
            "old_id": self.old_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InventoryItem":
        return cls(
            relative_path=data["relative_path"],
            file_size=data["file_size"],
            mtime=data["mtime"],
            extension=data["extension"],
            old_id=data.get("old_id", ""),
        )


@dataclass
class InventoryDiff:
    """清单比对结果"""
    added: List[InventoryItem] = field(default_factory=list)
    removed: List[InventoryItem] = field(default_factory=list)
    modified: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return len(self.added) > 0 or len(self.removed) > 0 or len(self.modified) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "added": [item.to_dict() for item in self.added],
            "removed": [item.to_dict() for item in self.removed],
            "modified": [
                {
                    "path": m["path"],
                    "old": m["old"].to_dict() if isinstance(m.get("old"), InventoryItem) else m.get("old"),
                    "new": m["new"].to_dict() if isinstance(m.get("new"), InventoryItem) else m.get("new"),
                }
                for m in self.modified
            ],
        }


@dataclass
class Inventory:
    """资产清单"""
    name: str
    source_root: Path
    created_at: datetime
    updated_at: datetime
    items: List[InventoryItem] = field(default_factory=list)
    description: str = ""

    @property
    def file_count(self) -> int:
        return len(self.items)

    @property
    def total_size(self) -> int:
        return sum(item.file_size for item in self.items)

    def get_old_ids(self) -> List[str]:
        """获取所有唯一旧编号"""
        return sorted(set(item.old_id for item in self.items if item.old_id))

    def get_items_by_old_id(self, old_id: str) -> List[InventoryItem]:
        """按旧编号筛选条目"""
        return [item for item in self.items if item.old_id == old_id]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "source_root": str(self.source_root),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "items": [item.to_dict() for item in self.items],
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Inventory":
        return cls(
            name=data["name"],
            source_root=Path(data["source_root"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            items=[InventoryItem.from_dict(item) for item in data.get("items", [])],
            description=data.get("description", ""),
        )
