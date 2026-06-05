"""报告模块 - JSON/CSV 导出、冲突报告"""
import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
import os

# Windows 终端编码适配
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
    sys.stderr.reconfigure(encoding="utf-8") if hasattr(sys.stderr, "reconfigure") else None
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except:
        pass

from .models import (
    AppConfig,
    BatchState,
    BatchStatus,
    ExecutionPlan,
)

logger = logging.getLogger(__name__)


def _safe_print(message: str = "") -> None:
    """安全打印，处理编码失败时降级"""
    try:
        print(message)
    except UnicodeEncodeError:
        ascii_msg = message.encode("ascii", errors="replace").decode("ascii")
        print(ascii_msg)


class ReportError(Exception):
    """报告生成错误"""
    pass


class Reporter:
    """报告生成器"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.report_dir = config.report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def generate_dry_run_report(
        self,
        plan: ExecutionPlan,
        batch_id: str,
    ) -> Dict[str, Path]:
        """生成 dry-run 报告

        包括：待处理列表、缺证据列表、未登记文件列表、冲突报告
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{batch_id}_dryrun_{timestamp}"

        reports: Dict[str, Path] = {}

        reports["pending"] = self._write_pending_csv(plan, f"{prefix}_pending.csv")
        reports["missing_evidence"] = self._write_missing_evidence_csv(plan, f"{prefix}_missing_evidence.csv")
        reports["unregistered"] = self._write_unregistered_csv(plan, f"{prefix}_unregistered.csv")
        reports["conflicts"] = self._write_conflicts_csv(plan, f"{prefix}_conflicts.csv")
        reports["summary_json"] = self._write_summary_json(plan, batch_id, f"{prefix}_summary.json")

        logger.info(f"Dry-run 报告已生成到: {self.report_dir}")
        for name, path in reports.items():
            logger.info(f"  - {name}: {path.name}")

        return reports

    def generate_execution_report(
        self,
        plan: ExecutionPlan,
        batch_id: str,
        successful_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
        batch_status: BatchStatus,
    ) -> Dict[str, Path]:
        """生成执行结果报告"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{batch_id}_result_{timestamp}"

        reports: Dict[str, Path] = {}

        reports["result_json"] = self._write_result_json(
            plan, batch_id, successful_ops, failed_ops, batch_status,
            f"{prefix}_result.json"
        )
        reports["result_csv"] = self._write_result_csv(
            plan, successful_ops, failed_ops, f"{prefix}_result.csv"
        )
        reports["operations_json"] = self._write_operations_json(
            successful_ops, failed_ops, f"{prefix}_operations.json"
        )

        if plan.conflicts:
            reports["conflicts"] = self._write_conflicts_csv(plan, f"{prefix}_conflicts.csv")
        if plan.missing_evidence:
            reports["missing_evidence"] = self._write_missing_evidence_csv(plan, f"{prefix}_missing_evidence.csv")
        if plan.unregistered:
            reports["unregistered"] = self._write_unregistered_csv(plan, f"{prefix}_unregistered.csv")

        logger.info(f"执行结果报告已生成到: {self.report_dir}")
        for name, path in reports.items():
            logger.info(f"  - {name}: {path.name}")

        return reports

    def generate_rollback_report(
        self,
        batch_id: str,
        rolled_back_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
    ) -> Dict[str, Path]:
        """生成回滚结果报告"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = f"{batch_id}_rollback_{timestamp}"

        reports: Dict[str, Path] = {}

        report_data = {
            "batch_id": batch_id,
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total_operations": len(rolled_back_ops) + len(failed_ops),
                "rolled_back": len(rolled_back_ops),
                "failed": len(failed_ops),
            },
            "rolled_back_operations": rolled_back_ops,
            "failed_operations": failed_ops,
        }

        reports["rollback_json"] = self._write_json(report_data, f"{prefix}_result.json")
        reports["rollback_csv"] = self._write_rollback_csv(
            rolled_back_ops, failed_ops, f"{prefix}_result.csv"
        )

        logger.info(f"回滚结果报告已生成到: {self.report_dir}")
        for name, path in reports.items():
            logger.info(f"  - {name}: {path.name}")

        return reports

    def print_plan_summary(self, plan: ExecutionPlan) -> None:
        """打印计划摘要到控制台"""
        total_items = len(plan.items)
        items_with_photos = sum(1 for item in plan.items if item.photos)
        items_no_photos = total_items - items_with_photos
        total_photos = sum(len(item.photos) for item in plan.items)

        _safe_print("\n" + "=" * 60)
        _safe_print("执行计划摘要")
        _safe_print("=" * 60)
        _safe_print(f"批次 ID: {plan.batch_id}")
        _safe_print(f"生成时间: {plan.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        _safe_print("-" * 60)
        _safe_print(f"总映射数:      {total_items}")
        _safe_print(f"有照片的映射:  {items_with_photos}")
        _safe_print(f"无照片的映射:  {items_no_photos}")
        _safe_print(f"总照片数:      {total_photos}")
        _safe_print(f"冲突数:        {len(plan.conflicts)}")
        _safe_print(f"缺证据数:      {len(plan.missing_evidence)}")
        _safe_print(f"未登记目录数:  {len(plan.unregistered)}")
        _safe_print(f"错误数:        {len(plan.errors)}")

        if plan.conflicts:
            _safe_print("\n" + "-" * 60)
            _safe_print("冲突详情:")
            for i, conflict in enumerate(plan.conflicts, 1):
                _safe_print(f"  {i}. [{conflict.get('type', 'unknown')}] {conflict.get('message', '')}")

        if plan.missing_evidence:
            _safe_print("\n" + "-" * 60)
            _safe_print("缺证据详情:")
            for i, item in enumerate(plan.missing_evidence, 1):
                _safe_print(f"  {i}. {item['old_id']} -> {item['new_tag']}: {item.get('reason', '')}")

        if plan.unregistered:
            _safe_print("\n" + "-" * 60)
            _safe_print("未登记目录:")
            for i, item in enumerate(plan.unregistered, 1):
                _safe_print(f"  {i}. {item['directory']} ({item['photo_count']} 个照片)")

        if plan.errors:
            _safe_print("\n" + "-" * 60)
            _safe_print("错误详情:")
            for i, error in enumerate(plan.errors, 1):
                _safe_print(f"  {i}. {error}")

        _safe_print("=" * 60 + "\n")

    def print_execution_summary(
        self,
        successful_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
    ) -> None:
        """打印执行结果摘要"""
        total = len(successful_ops) + len(failed_ops)
        success_count = len(successful_ops)
        fail_count = len(failed_ops)

        _safe_print("\n" + "=" * 60)
        _safe_print("执行结果摘要")
        _safe_print("=" * 60)
        _safe_print(f"总操作数: {total}")
        _safe_print(f"成功:     {success_count}")
        _safe_print(f"失败:     {fail_count}")
        if total > 0:
            _safe_print(f"成功率:   {success_count / total * 100:.1f}%")

        if failed_ops:
            _safe_print("\n失败详情:")
            for i, failure in enumerate(failed_ops, 1):
                new_tag = failure.get("new_tag", "unknown")
                source = failure.get("source_path", "unknown")
                error = failure.get("error", "unknown error")
                _safe_print(f"  {i}. [{new_tag}] {source}")
                _safe_print(f"     错误: {error}")

        _safe_print("=" * 60 + "\n")

    def print_rollback_summary(
        self,
        rolled_back_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
    ) -> None:
        """打印回滚结果摘要"""
        total = len(rolled_back_ops) + len(failed_ops)
        success_count = len(rolled_back_ops)
        fail_count = len(failed_ops)

        _safe_print("\n" + "=" * 60)
        _safe_print("回滚结果摘要")
        _safe_print("=" * 60)
        _safe_print(f"总操作数: {total}")
        _safe_print(f"已回滚:   {success_count}")
        _safe_print(f"失败:     {fail_count}")
        if total > 0:
            _safe_print(f"成功率:   {success_count / total * 100:.1f}%")

        stopped = any(f.get("rollback_stopped") for f in failed_ops)
        if stopped:
            _safe_print("\n[WARN] 回滚因文件锁定而中止，部分操作未完成回滚")

        if failed_ops:
            _safe_print("\n失败详情:")
            for i, failure in enumerate(failed_ops, 1):
                new_tag = failure.get("new_tag", "unknown")
                target = failure.get("target_path", "unknown")
                error = failure.get("error", "unknown error")
                _safe_print(f"  {i}. [{new_tag}] {target}")
                _safe_print(f"     错误: {error}")

        _safe_print("=" * 60 + "\n")

    def print_batch_list(self, batches: List[BatchState]) -> None:
        """打印批次列表"""
        if not batches:
            _safe_print("\n暂无批次记录\n")
            return

        _safe_print("\n" + "=" * 100)
        _safe_print(f"{'批次 ID':<32} {'状态':<16} {'创建时间':<20} {'更新时间':<20} {'操作数':<8}")
        _safe_print("-" * 100)

        for batch in batches:
            status_icon = self._get_status_icon(batch.status)
            created = batch.created_at.strftime("%Y-%m-%d %H:%M")
            updated = batch.updated_at.strftime("%Y-%m-%d %H:%M")
            op_count = len(batch.operations)
            _safe_print(
                f"{batch.batch_id:<32} {status_icon} {batch.status.value:<14} "
                f"{created:<20} {updated:<20} {op_count:<8}"
            )

        _safe_print("=" * 100 + "\n")

    def print_batch_detail(self, batch: BatchState, show_logs: bool = False) -> None:
        """打印批次详情"""
        _safe_print("\n" + "=" * 80)
        _safe_print("批次详情")
        _safe_print("=" * 80)
        _safe_print(f"批次 ID:    {batch.batch_id}")
        _safe_print(f"状态:       {self._get_status_icon(batch.status)} {batch.status.value}")
        _safe_print(f"创建时间:   {batch.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
        _safe_print(f"更新时间:   {batch.updated_at.strftime('%Y-%m-%d %H:%M:%S')}")

        if batch.plan:
            item_count = len(batch.plan.get("items", []))
            photo_count = sum(item.get("photo_count", 0) for item in batch.plan.get("items", []))
            _safe_print(f"计划项目:   {item_count} 个映射, {photo_count} 个照片")

        _safe_print(f"操作记录:   {len(batch.operations)} 条")
        if batch.errors:
            _safe_print(f"错误记录:   {len(batch.errors)} 条")

        if batch.plan and batch.plan.get("conflicts"):
            _safe_print(f"冲突数:     {len(batch.plan['conflicts'])}")
        if batch.plan and batch.plan.get("missing_evidence"):
            _safe_print(f"缺证据数:   {len(batch.plan['missing_evidence'])}")

        if batch.config:
            _safe_print("\n配置摘要:")
            _safe_print(f"  源目录:   {batch.config.get('source_root', 'N/A')}")
            _safe_print(f"  目标目录: {batch.config.get('target_root', 'N/A')}")
            _safe_print(f"  操作:     {batch.config.get('operation', 'N/A')}")
            _safe_print(f"  CSV 文件: {batch.config.get('csv_path', 'N/A')}")

        if show_logs:
            _safe_print("\n最近日志:")
            log_file = Path(batch.config.get("log_dir", "")) / f"{batch.batch_id}.log" \
                if batch.config else None
            if log_file and log_file.exists():
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in lines[-20:]:
                        _safe_print(f"  {line.rstrip()}")
            else:
                _safe_print("  (无日志文件)")

        _safe_print("=" * 80 + "\n")

    @staticmethod
    def _get_status_icon(status: BatchStatus) -> str:
        """获取状态图标（ASCII 版本，避免编码问题）"""
        icons = {
            BatchStatus.PENDING: "[PEND]",
            BatchStatus.PLANNING: "[PLAN]",
            BatchStatus.PLANNED: "[OK]",
            BatchStatus.EXECUTING: "[RUN]",
            BatchStatus.COMPLETED: "[OK]",
            BatchStatus.PARTIAL: "[WARN]",
            BatchStatus.FAILED: "[ERR]",
            BatchStatus.ROLLING_BACK: "[RBK]",
            BatchStatus.ROLLED_BACK: "[RBK]",
            BatchStatus.ROLLBACK_FAILED: "[ERR]",
        }
        return icons.get(status, "[?]")

    def _write_pending_csv(self, plan: ExecutionPlan, filename: str) -> Path:
        """写入待处理列表 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "序号", "旧编号", "新标签", "资产类型", "照片目录",
                "目标目录", "照片数量", "状态"
            ])

            for idx, item in enumerate(plan.items, 1):
                writer.writerow([
                    idx,
                    item.mapping.old_id,
                    item.mapping.new_tag,
                    item.mapping.asset_type.value,
                    str(item.mapping.photo_dir),
                    str(item.target_dir) if item.target_dir else "",
                    len(item.photos),
                    item.status,
                ])
        return path

    def _write_missing_evidence_csv(self, plan: ExecutionPlan, filename: str) -> Path:
        """写入缺证据列表 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "旧编号", "新标签", "照片目录", "原因"])

            for idx, item in enumerate(plan.missing_evidence, 1):
                writer.writerow([
                    idx,
                    item.get("old_id", ""),
                    item.get("new_tag", ""),
                    item.get("photo_dir", ""),
                    item.get("reason", ""),
                ])
        return path

    def _write_unregistered_csv(self, plan: ExecutionPlan, filename: str) -> Path:
        """写入未登记目录 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "目录路径", "照片数量", "说明"])

            for idx, item in enumerate(plan.unregistered, 1):
                writer.writerow([
                    idx,
                    item.get("directory", ""),
                    item.get("photo_count", 0),
                    item.get("message", ""),
                ])
        return path

    def _write_conflicts_csv(self, plan: ExecutionPlan, filename: str) -> Path:
        """写入冲突报告 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "冲突类型", "相关信息", "详细说明"])

            for idx, conflict in enumerate(plan.conflicts, 1):
                related = ""
                if "new_tag" in conflict:
                    related = f"新标签: {conflict['new_tag']}"
                elif "target_path" in conflict:
                    related = f"目标路径: {conflict.get('target_path', '')}"
                elif "old_id" in conflict:
                    related = f"旧编号: {conflict.get('old_id', '')}"

                writer.writerow([
                    idx,
                    conflict.get("type", "unknown"),
                    related,
                    conflict.get("message", ""),
                ])
        return path

    def _write_summary_json(self, plan: ExecutionPlan, batch_id: str, filename: str) -> Path:
        """写入计划摘要 JSON"""
        total_items = len(plan.items)
        items_with_photos = sum(1 for item in plan.items if item.photos)
        total_photos = sum(len(item.photos) for item in plan.items)

        summary = {
            "batch_id": batch_id,
            "generated_at": datetime.now().isoformat(),
            "plan_created_at": plan.created_at.isoformat(),
            "summary": {
                "total_mappings": total_items,
                "mappings_with_photos": items_with_photos,
                "mappings_no_photos": total_items - items_with_photos,
                "total_photos": total_photos,
                "conflicts_count": len(plan.conflicts),
                "missing_evidence_count": len(plan.missing_evidence),
                "unregistered_count": len(plan.unregistered),
                "errors_count": len(plan.errors),
            },
            "items": [
                {
                    "old_id": item.mapping.old_id,
                    "new_tag": item.mapping.new_tag,
                    "asset_type": item.mapping.asset_type.value,
                    "photo_dir": str(item.mapping.photo_dir),
                    "target_dir": str(item.target_dir) if item.target_dir else None,
                    "photo_count": len(item.photos),
                    "photos": [
                        {"source": str(p.source_path), "size": p.file_size}
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

        return self._write_json(summary, filename)

    def _write_result_json(
        self,
        plan: ExecutionPlan,
        batch_id: str,
        successful_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
        batch_status: BatchStatus,
        filename: str,
    ) -> Path:
        """写入执行结果 JSON"""
        result = {
            "batch_id": batch_id,
            "generated_at": datetime.now().isoformat(),
            "final_status": batch_status.value,
            "summary": {
                "total_operations": len(successful_ops) + len(failed_ops),
                "successful": len(successful_ops),
                "failed": len(failed_ops),
                "conflicts": len(plan.conflicts),
                "missing_evidence": len(plan.missing_evidence),
            },
            "successful_operations": successful_ops,
            "failed_operations": failed_ops,
            "conflicts": plan.conflicts,
            "missing_evidence": plan.missing_evidence,
        }

        return self._write_json(result, filename)

    def _write_result_csv(
        self,
        plan: ExecutionPlan,
        successful_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
        filename: str,
    ) -> Path:
        """写入执行结果 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "状态", "旧编号", "新标签", "资产类型", "照片序号",
                "源路径", "目标路径", "文件大小", "操作类型", "归档路径",
                "执行时间", "错误信息"
            ])

            for op in successful_ops:
                writer.writerow([
                    "成功",
                    op.get("old_id", ""),
                    op.get("new_tag", ""),
                    op.get("asset_type", ""),
                    op.get("photo_index", ""),
                    op.get("source_path", ""),
                    op.get("target_path", ""),
                    op.get("file_size", ""),
                    op.get("operation", ""),
                    op.get("archived_path", ""),
                    op.get("timestamp", ""),
                    "",
                ])

            for op in failed_ops:
                writer.writerow([
                    "失败",
                    op.get("old_id", ""),
                    op.get("new_tag", ""),
                    "",
                    op.get("photo_index", ""),
                    op.get("source_path", ""),
                    "",
                    "",
                    "",
                    "",
                    op.get("timestamp", ""),
                    op.get("error", ""),
                ])

        return path

    def _write_operations_json(
        self,
        successful_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
        filename: str,
    ) -> Path:
        """写入操作记录 JSON（可用于回滚）"""
        operations = []
        for op in successful_ops:
            operations.append({
                "status": "successful",
                **op,
            })
        for op in failed_ops:
            operations.append({
                "status": "failed",
                **op,
            })

        return self._write_json({"operations": operations}, filename)

    def _write_rollback_csv(
        self,
        rolled_back_ops: List[Dict[str, Any]],
        failed_ops: List[Dict[str, Any]],
        filename: str,
    ) -> Path:
        """写入回滚结果 CSV"""
        path = self.report_dir / filename
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "状态", "旧编号", "新标签", "照片序号", "源路径", "目标路径",
                "回滚操作", "执行时间", "错误信息"
            ])

            for op in rolled_back_ops:
                writer.writerow([
                    "已回滚",
                    op.get("old_id", ""),
                    op.get("new_tag", ""),
                    op.get("photo_index", ""),
                    op.get("source_path", ""),
                    op.get("target_path", ""),
                    op.get("rollback_operation", ""),
                    op.get("timestamp", ""),
                    "",
                ])

            for op in failed_ops:
                writer.writerow([
                    "失败",
                    op.get("old_id", ""),
                    op.get("new_tag", ""),
                    op.get("photo_index", ""),
                    op.get("source_path", ""),
                    op.get("target_path", ""),
                    "",
                    op.get("timestamp", ""),
                    op.get("error", ""),
                ])

        return path

    def _write_json(self, data: Dict[str, Any], filename: str) -> Path:
        """写入 JSON 文件"""
        path = self.report_dir / filename
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path
