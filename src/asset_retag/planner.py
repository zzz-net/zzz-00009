"""计划模块 - 生成执行计划、冲突检测、dry-run"""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

from .models import (
    AppConfig,
    AssetMapping,
    AssetPlanItem,
    ExecutionPlan,
    PhotoFile,
)

logger = logging.getLogger(__name__)


class PlanningError(Exception):
    """计划错误"""
    pass


class FatalPlanningError(PlanningError):
    """致命计划错误 - 必须中止执行"""

    def __init__(self, message: str, conflicts: List[Dict]):
        super().__init__(message)
        self.conflicts = conflicts


class ExecutionPlanner:
    """执行计划生成器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.extensions = set(config.photo_extensions)

    def generate_plan(
        self,
        mappings: List[AssetMapping],
        batch_id: str,
    ) -> ExecutionPlan:
        """生成执行计划

        此方法为纯函数，不修改任何资产文件
        """
        plan = ExecutionPlan(
            batch_id=batch_id,
            created_at=datetime.now(),
        )

        logger.info(f"开始生成执行计划，批次 ID: {batch_id}")

        all_photo_dirs: Set[Path] = set()
        new_tag_counts: Dict[str, int] = {}
        target_path_map: Dict[Path, List[AssetMapping]] = {}

        for mapping in mappings:
            all_photo_dirs.add(mapping.photo_dir)

            if mapping.new_tag in new_tag_counts:
                new_tag_counts[mapping.new_tag] += 1
            else:
                new_tag_counts[mapping.new_tag] = 1

        plan.conflicts.extend(self._check_duplicate_new_tags(new_tag_counts, mappings))

        for mapping in mappings:
            try:
                photos = self._scan_photos(mapping.photo_dir)

                target_dir = self._build_target_dir(mapping)
                target_paths = self._build_target_paths(mapping, photos)

                for tp in target_paths:
                    if tp in target_path_map:
                        target_path_map[tp].append(mapping)
                    else:
                        target_path_map[tp] = [mapping]

                if not photos:
                    plan.missing_evidence.append({
                        "old_id": mapping.old_id,
                        "new_tag": mapping.new_tag,
                        "photo_dir": str(mapping.photo_dir),
                        "reason": "照片目录中没有找到匹配扩展名的文件",
                    })

                item = AssetPlanItem(
                    mapping=mapping,
                    photos=photos,
                    target_dir=target_dir,
                    status="planned" if photos else "no_photos",
                )
                plan.items.append(item)

            except Exception as e:
                plan.errors.append(
                    f"处理映射 {mapping.old_id} -> {mapping.new_tag} 时出错: {e}"
                )
                logger.exception(f"处理映射失败: {mapping.old_id}")

        plan.conflicts.extend(self._check_target_path_conflicts(target_path_map))
        plan.conflicts.extend(self._check_existing_targets(plan.items))
        plan.unregistered.extend(self._find_unregistered_dirs(all_photo_dirs))

        self._summarize_plan(plan)

        fatal_conflicts = [
            c for c in plan.conflicts
            if c.get("type") in ("duplicate_new_tag", "target_path_conflict")
        ]
        if fatal_conflicts:
            error_msg = (
                f"检测到 {len(fatal_conflicts)} 个致命冲突，必须修复后才能继续：\n"
                + "\n".join(f"  - {c.get('message', '')}" for c in fatal_conflicts)
            )
            raise FatalPlanningError(error_msg, fatal_conflicts)

        return plan

    def _scan_photos(self, photo_dir: Path) -> List[PhotoFile]:
        """扫描照片目录中的照片文件"""
        photos: List[PhotoFile] = []

        if not photo_dir.exists():
            return photos

        try:
            entries = sorted(photo_dir.iterdir())
        except PermissionError as e:
            logger.warning(f"无法访问目录 {photo_dir}: {e}")
            return photos

        for entry in entries:
            if entry.is_file():
                ext = entry.suffix.lower().lstrip(".")
                if ext in self.extensions:
                    try:
                        stat = entry.stat()
                        photos.append(PhotoFile(
                            source_path=entry,
                            file_name=entry.name,
                            file_size=stat.st_size,
                        ))
                    except OSError as e:
                        logger.warning(f"无法读取文件 {entry}: {e}")

        return photos

    def _build_target_dir(self, mapping: AssetMapping) -> Path:
        """构建目标目录路径"""
        try:
            dir_path = self.config.dir_pattern.format(
                asset_type=mapping.asset_type.value,
                new_tag=mapping.new_tag,
                old_id=mapping.old_id,
            )
            return (self.config.target_root / dir_path).resolve()
        except KeyError as e:
            raise PlanningError(f"目录模板包含未知变量: {e}")
        except Exception as e:
            raise PlanningError(f"构建目标目录失败: {e}")

    def _build_target_paths(self, mapping: AssetMapping, photos: List[PhotoFile]) -> List[Path]:
        """构建所有目标文件路径"""
        paths: List[Path] = []
        target_dir = self._build_target_dir(mapping)

        for idx, photo in enumerate(photos, start=1):
            ext = photo.source_path.suffix.lower().lstrip(".")
            try:
                filename = self.config.filename_pattern.format(
                    new_tag=mapping.new_tag,
                    old_id=mapping.old_id,
                    asset_type=mapping.asset_type.value,
                    idx=idx,
                    ext=ext,
                )
                paths.append((target_dir / filename).resolve())
            except KeyError as e:
                raise PlanningError(f"文件名模板包含未知变量: {e}")

        return paths

    def _check_duplicate_new_tags(
        self,
        tag_counts: Dict[str, int],
        mappings: List[AssetMapping],
    ) -> List[Dict]:
        """检查重复的新标签"""
        conflicts: List[Dict] = []

        for tag, count in tag_counts.items():
            if count > 1:
                old_ids = [m.old_id for m in mappings if m.new_tag == tag]
                conflicts.append({
                    "type": "duplicate_new_tag",
                    "new_tag": tag,
                    "old_ids": old_ids,
                    "message": f"新标签 '{tag}' 被 {count} 个旧编号使用: {', '.join(old_ids)}",
                })

        return conflicts

    def _check_target_path_conflicts(
        self,
        target_path_map: Dict[Path, List[AssetMapping]],
    ) -> List[Dict]:
        """检查目标路径冲突（多个映射生成相同目标路径）"""
        conflicts: List[Dict] = []

        for path, mappings in target_path_map.items():
            if len(mappings) > 1:
                conflicts.append({
                    "type": "target_path_conflict",
                    "target_path": str(path),
                    "mappings": [
                        {"old_id": m.old_id, "new_tag": m.new_tag}
                        for m in mappings
                    ],
                    "message": f"目标路径 '{path}' 被 {len(mappings)} 个映射共享",
                })

        return conflicts

    def _check_existing_targets(self, items: List[AssetPlanItem]) -> List[Dict]:
        """检查目标路径已存在的文件"""
        conflicts: List[Dict] = []

        for item in items:
            if not item.target_dir or not item.photos:
                continue

            mapping = item.mapping
            target_paths = self._build_target_paths(mapping, item.photos)

            for idx, target_path in enumerate(target_paths):
                if target_path.exists():
                    existing_size = target_path.stat().st_size
                    incoming_size = item.photos[idx].file_size

                    conflicts.append({
                        "type": "target_exists",
                        "old_id": mapping.old_id,
                        "new_tag": mapping.new_tag,
                        "target_path": str(target_path),
                        "source_path": str(item.photos[idx].source_path),
                        "existing_size": existing_size,
                        "incoming_size": incoming_size,
                        "message": f"目标文件已存在: {target_path}",
                    })

        return conflicts

    def _find_unregistered_dirs(self, registered_dirs: Set[Path]) -> List[Dict]:
        """查找未登记的目录（源根目录下的子目录不在映射中）"""
        unregistered: List[Dict] = []
        source_root = self.config.source_root

        try:
            for entry in sorted(source_root.iterdir()):
                if entry.is_dir():
                    resolved = entry.resolve()
                    if resolved not in registered_dirs:
                        file_count = sum(
                            1 for f in entry.iterdir()
                            if f.is_file() and f.suffix.lower().lstrip(".") in self.extensions
                        )
                        if file_count > 0:
                            unregistered.append({
                                "directory": str(resolved),
                                "photo_count": file_count,
                                "message": f"目录包含 {file_count} 个照片文件但未在 CSV 中登记",
                            })
        except PermissionError as e:
            logger.warning(f"无法扫描源根目录 {source_root}: {e}")

        return unregistered

    @staticmethod
    def _summarize_plan(plan: ExecutionPlan) -> None:
        """生成计划摘要"""
        total_items = len(plan.items)
        items_with_photos = sum(1 for item in plan.items if item.photos)
        items_no_photos = total_items - items_with_photos
        total_photos = sum(len(item.photos) for item in plan.items)

        logger.info(f"计划生成完成:")
        logger.info(f"  - 总映射数: {total_items}")
        logger.info(f"  - 有照片的映射: {items_with_photos}")
        logger.info(f"  - 无照片的映射: {items_no_photos}")
        logger.info(f"  - 总照片数: {total_photos}")
        logger.info(f"  - 冲突数: {len(plan.conflicts)}")
        logger.info(f"  - 缺证据数: {len(plan.missing_evidence)}")
        logger.info(f"  - 未登记目录数: {len(plan.unregistered)}")
        logger.info(f"  - 错误数: {len(plan.errors)}")
