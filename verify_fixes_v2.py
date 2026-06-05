"""验证所有安全修复的 CLI 测试脚本 - 使用 subprocess 调用"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


EXAMPLES_DIR = Path("examples")
STATE_DIR = Path.home() / ".asset-retag" / "state"
LOG_DIR = Path.home() / ".asset-retag" / "logs"


def run_cli(args, cwd=None):
    """运行 CLI 命令并返回 (退出码, stdout, stderr)"""
    cmd = [sys.executable, "-m", "asset_retag.cli"] + args
    result = subprocess.run(
        cmd,
        cwd=cwd or os.getcwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


def cleanup():
    """清理测试环境"""
    print("\n[INFO] 清理测试环境...")
    for dir_name in ["target", "archive", "reports"]:
        dir_path = EXAMPLES_DIR / dir_name
        if dir_path.exists():
            shutil.rmtree(dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)

    for directory in [STATE_DIR, LOG_DIR]:
        if directory.exists():
            for f in directory.glob("*"):
                try:
                    if f.is_file():
                        f.unlink()
                except:
                    pass

    for f in EXAMPLES_DIR.glob("test_*.csv"):
        try:
            f.unlink()
        except:
            pass


def test_scenario_1_duplicate_new_tag():
    """场景1：重复新标签 - 应硬错误拦住，退出码 1"""
    print("\n" + "=" * 70)
    print("场景1：重复新标签硬错误检测")
    print("=" * 70)

    csv_content = """old_id,new_tag,asset_type,photo_dir
OLD001,AST-DUP-001,hardware,OLD001_laptop
OLD002,AST-DUP-001,hardware,OLD002_monitor
OLD003,AST-003,software,OLD003_software
"""
    csv_path = EXAMPLES_DIR / "test_scenario1.csv"
    csv_path.write_text(csv_content, encoding="utf-8-sig")

    before_state = set(STATE_DIR.glob("*")) if STATE_DIR.exists() else set()
    before_log = set(LOG_DIR.glob("*")) if LOG_DIR.exists() else set()

    print("\n执行: asset-retag dry-run -c examples/config.yaml -m examples/test_scenario1.csv --skip-confirm")
    code, stdout, stderr = run_cli([
        "dry-run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(csv_path),
        "--skip-confirm",
    ])

    output = stdout + stderr
    print(f"\n退出码: {code} (预期: 1)")
    assert code == 1, f"预期退出码 1，实际 {code}\n输出: {output[:500]}"

    assert "致命冲突" in output, f"输出应包含'致命冲突'\n输出: {output[:500]}"
    assert "新标签" in output, f"输出应包含'新标签'\n输出: {output[:500]}"
    assert "AST-DUP-001" in output, f"输出应包含重复标签名\n输出: {output[:500]}"
    assert "目标路径" in output, f"输出应包含'目标路径'冲突\n输出: {output[:500]}"

    after_state = set(STATE_DIR.glob("*")) if STATE_DIR.exists() else set()
    after_log = set(LOG_DIR.glob("*")) if LOG_DIR.exists() else set()

    new_state = after_state - before_state
    new_log = after_log - before_log

    print(f"新建状态文件: {len(new_state)}")
    print(f"新建日志文件: {len(new_log)}")

    print("\n[PASS] 场景1：重复新标签正确拦截")
    return True


def test_scenario_2_photo_dir_not_exist():
    """场景2：照片目录不存在 - dry-run 直接失败，不留半成品"""
    print("\n" + "=" * 70)
    print("场景2：照片目录不存在 - dry-run 直接失败")
    print("=" * 70)

    csv_content = """old_id,new_tag,asset_type,photo_dir
OLD001,AST-NOEXIST-001,hardware,NONEXISTENT_DIR_12345
OLD002,AST-002,hardware,OLD002_monitor
"""
    csv_path = EXAMPLES_DIR / "test_scenario2.csv"
    csv_path.write_text(csv_content, encoding="utf-8-sig")

    before_state = set(STATE_DIR.glob("*")) if STATE_DIR.exists() else set()
    before_log = set(LOG_DIR.glob("*")) if LOG_DIR.exists() else set()

    print("\n执行: asset-retag dry-run -c examples/config.yaml -m examples/test_scenario2.csv --skip-confirm")
    code, stdout, stderr = run_cli([
        "dry-run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(csv_path),
        "--skip-confirm",
    ])

    output = stdout + stderr
    print(f"\n退出码: {code} (预期: 1)")
    assert code == 1, f"预期退出码 1，实际 {code}\n输出: {output[:500]}"

    assert "致命错误" in output, f"输出应包含'致命错误'\n输出: {output[:500]}"
    assert "NONEXISTENT_DIR_12345" in output, f"输出应包含不存在的目录名\n输出: {output[:500]}"
    assert "未创建任何批次状态或报告" in output, f"输出应包含清理提示\n输出: {output[:500]}"

    after_state = set(STATE_DIR.glob("*")) if STATE_DIR.exists() else set()
    after_log = set(LOG_DIR.glob("*")) if LOG_DIR.exists() else set()

    new_state = after_state - before_state
    new_log = after_log - before_log

    print(f"新建状态文件: {len(new_state)} (预期: 0)")
    print(f"新建日志文件: {len(new_log)} (预期: 0)")

    assert len(new_state) == 0, f"不应创建新的状态文件，实际创建了 {len(new_state)} 个"
    assert len(new_log) == 0, f"不应创建新的日志文件，实际创建了 {len(new_log)} 个"

    reports = list((EXAMPLES_DIR / "reports").glob("*"))
    print(f"报告文件: {len(reports)} (预期: 0)")
    assert len(reports) == 0, f"不应创建报告文件，实际创建了 {len(reports)} 个"

    print("\n[PASS] 场景2：照片目录不存在正确处理")
    return True


def test_scenario_3_normal_workflow():
    """场景3：正常工作流 - dry-run → run → rollback"""
    print("\n" + "=" * 70)
    print("场景3：正常工作流 - dry-run → run → rollback")
    print("=" * 70)

    batch_id = "test_normal_001"

    # Step 1: dry-run
    print("\n--- Step 1: dry-run ---")
    print("执行: asset-retag dry-run -c examples/config.yaml -m examples/mapping.csv --batch-id " + batch_id)
    code, stdout, stderr = run_cli([
        "dry-run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(EXAMPLES_DIR / "mapping.csv"),
        "--batch-id", batch_id,
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 0)")
    assert code == 0, f"dry-run 预期退出码 0，实际 {code}\n输出: {output[:500]}"

    state_file = STATE_DIR / f"{batch_id}.json"
    assert state_file.exists(), "dry-run 后状态文件应存在"
    print(f"状态文件存在: {state_file}")

    reports = list((EXAMPLES_DIR / "reports").glob(f"{batch_id}_*"))
    assert len(reports) > 0, "dry-run 后应生成报告"
    print(f"报告文件: {len(reports)} 个")

    # Step 2: run
    print("\n--- Step 2: run ---")
    print("执行: asset-retag run -c examples/config.yaml -m examples/mapping.csv --batch-id " + batch_id + " --skip-confirm")
    code, stdout, stderr = run_cli([
        "run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(EXAMPLES_DIR / "mapping.csv"),
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 0)")
    assert code == 0, f"run 预期退出码 0，实际 {code}\n输出: {output[:500]}"

    target_files = list((EXAMPLES_DIR / "target").rglob("*.*"))
    target_files = [f for f in target_files if f.is_file()]
    print(f"目标文件: {len(target_files)} 个 (预期: >0)")
    assert len(target_files) > 0, "执行后目标目录应有文件"

    with open(state_file, "r", encoding="utf-8") as f:
        state_data = json.load(f)
    ops = state_data.get("operations", [])
    assert len(ops) > 0, "应有操作记录"
    assert "target_fingerprint" in ops[0], "操作记录应包含 target_fingerprint"
    print(f"操作记录包含指纹字段: {list(ops[0].keys())}")

    # Step 3: rollback
    print("\n--- Step 3: rollback ---")
    print("执行: asset-retag rollback --batch-id " + batch_id + " --skip-confirm")
    code, stdout, stderr = run_cli([
        "rollback",
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 0)")
    assert code == 0, f"rollback 预期退出码 0，实际 {code}\n输出: {output[:500]}"

    target_files_after = list((EXAMPLES_DIR / "target").rglob("*.*"))
    target_files_after = [f for f in target_files_after if f.is_file()]
    print(f"回滚后目标文件: {len(target_files_after)} 个 (预期: 0)")
    assert len(target_files_after) == 0, "回滚后目标目录不应有文件"

    print("\n[PASS] 场景3：正常工作流正确")
    return True


def test_scenario_4_rollback_ownership():
    """场景4：回滚前校验目标文件所有权"""
    print("\n" + "=" * 70)
    print("场景4：回滚文件所有权校验")
    print("=" * 70)

    batch_id = "test_ownership_001"

    # Step 1: run
    print("\n--- Step 1: 执行批次 ---")
    code, stdout, stderr = run_cli([
        "run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(EXAMPLES_DIR / "mapping.csv"),
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 0)")
    assert code == 0, f"run 预期退出码 0，实际 {code}\n输出: {output[:500]}"

    target_files = list((EXAMPLES_DIR / "target").rglob("*.jpg"))
    if not target_files:
        target_files = list((EXAMPLES_DIR / "target").rglob("*.png"))
    assert target_files, "没有找到目标文件"

    target_file = target_files[0]
    original_size = target_file.stat().st_size
    print(f"\n篡改文件: {target_file}")
    print(f"原始大小: {original_size} 字节")

    with open(target_file, "ab") as f:
        f.write(b"__TAMPERED_TEST_DATA__")

    tampered_size = target_file.stat().st_size
    print(f"篡改后大小: {tampered_size} 字节")

    # Step 3: 尝试回滚
    print("\n--- Step 2: 尝试回滚（应失败） ---")
    code, stdout, stderr = run_cli([
        "rollback",
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 1)")
    assert code == 1, f"rollback 预期退出码 1，实际 {code}\n输出: {output[:500]}"

    assert "所有权校验失败" in output, f"输出应包含'所有权校验失败'\n输出: {output[:500]}"
    assert "未删除任何文件" in output, f"输出应包含'未删除任何文件'\n输出: {output[:500]}"

    assert target_file.exists(), "所有权校验失败时不应删除文件"
    final_size = target_file.stat().st_size
    assert final_size == tampered_size, f"文件不应被修改: {original_size} -> {tampered_size} -> {final_size}"
    print(f"文件未被删除，大小保持: {final_size} 字节")

    print("\n[PASS] 场景4：回滚所有权校验正确")
    return True


def test_scenario_5_idempotency():
    """场景5：幂等控制 - 重复执行同一批次被拒绝"""
    print("\n" + "=" * 70)
    print("场景5：幂等控制 - 重复执行被拒绝")
    print("=" * 70)

    batch_id = "test_idempotent_001"

    # 第一次执行
    print("\n--- Step 1: 第一次执行 ---")
    code, stdout, stderr = run_cli([
        "run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(EXAMPLES_DIR / "mapping.csv"),
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 0)")
    assert code == 0, f"第一次执行预期退出码 0，实际 {code}\n输出: {output[:500]}"

    # 尝试重复执行
    print("\n--- Step 2: 尝试重复执行 ---")
    code, stdout, stderr = run_cli([
        "run",
        "-c", str(EXAMPLES_DIR / "config.yaml"),
        "-m", str(EXAMPLES_DIR / "mapping.csv"),
        "--batch-id", batch_id,
        "--skip-confirm",
    ])
    output = stdout + stderr
    print(f"退出码: {code} (预期: 1)")
    assert code == 1, f"重复执行预期退出码 1，实际 {code}\n输出: {output[:500]}"

    assert "无法执行" in output, f"输出应包含'无法执行'\n输出: {output[:500]}"

    print("\n[PASS] 场景5：幂等控制正确")
    return True


def test_scenario_6_windows_encoding():
    """场景6：Windows 编码兼容性 - 无 emoji，ASCII 图标"""
    print("\n" + "=" * 70)
    print("场景6：输出编码兼容性")
    print("=" * 70)

    code, stdout, stderr = run_cli(["--help"])
    output = stdout + stderr

    emoji_chars = ["⚠️", "✅", "❌", "📋", "⏳", "📝", "⚡", "↩️", "❓"]
    found_emoji = [e for e in emoji_chars if e in output]
    print(f"找到 emoji: {found_emoji} (预期: 无)")
    assert len(found_emoji) == 0, f"输出不应包含 emoji: {found_emoji}"

    ascii_icons = ["[OK]", "[ERR]", "[WARN]", "[INFO]", "[PEND]", "[PLAN]", "[RUN]", "[RBK]"]
    found_icons = [i for i in ascii_icons if i in output]
    print(f"找到 ASCII 图标: {found_icons}")

    print(f"\n退出码: {code} (预期: 0)")
    assert code == 0, f"帮助命令预期退出码 0，实际 {code}\n输出: {output[:500]}"

    print("\n[PASS] 场景6：输出编码兼容性正确")
    return True


def main():
    print("=" * 70)
    print("资产标签重贴 CLI 安全修复验证")
    print("=" * 70)

    scenarios = [
        ("场景1：重复新标签硬错误检测", test_scenario_1_duplicate_new_tag),
        ("场景2：照片目录不存在直接失败", test_scenario_2_photo_dir_not_exist),
        ("场景3：正常工作流（dry-run → run → rollback）", test_scenario_3_normal_workflow),
        ("场景4：回滚所有权校验", test_scenario_4_rollback_ownership),
        ("场景5：幂等控制", test_scenario_5_idempotency),
        ("场景6：输出编码兼容性", test_scenario_6_windows_encoding),
    ]

    passed = 0
    failed = 0

    for name, test_func in scenarios:
        try:
            cleanup()
            test_func()
            passed += 1
        except Exception as e:
            print(f"\n[FAIL] {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    cleanup()

    print("\n" + "=" * 70)
    print(f"验证结果: 通过 {passed} / {len(scenarios)}, 失败 {failed}")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
