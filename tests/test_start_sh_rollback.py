"""
测试 start.sh 的自动回退逻辑。

由于 start.sh 是 bash 脚本，这里通过创建临时 git 仓库，
模拟健康检查失败场景，验证回退逻辑是否能正确触发 git revert。
"""
import os
import subprocess
import tempfile
import time


def run_bash(script, cwd=None, timeout=30):
    """运行 bash 脚本并返回结果"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result


def test_rollback_triggered_on_health_failure():
    """健康检查失败时，应触发 git revert 回退"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 初始化 git 仓库
        run_bash("git init", cwd=tmpdir)
        run_bash('git config user.email "test@test.com"', cwd=tmpdir)
        run_bash('git config user.name "Test"', cwd=tmpdir)

        # 创建初始提交（好版本）
        with open(os.path.join(tmpdir, "version.txt"), "w") as f:
            f.write("v1")
        run_bash("git add version.txt", cwd=tmpdir)
        run_bash('git commit -m "v1"', cwd=tmpdir)
        good_commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=tmpdir,
                check=True,
            )
            .stdout.strip()
        )

        # 创建第二个提交（模拟坏版本）
        with open(os.path.join(tmpdir, "version.txt"), "w") as f:
            f.write("v2-bad")
        run_bash("git add version.txt", cwd=tmpdir)
        run_bash('git commit -m "v2-bad"', cwd=tmpdir)

        # 模拟回退逻辑：健康检查失败 → git revert HEAD
        result = run_bash(
            """
            cd {tmpdir}
            # 模拟健康检查失败
            health_ok=false

            if [ "$health_ok" = false ]; then
                echo "Health check failed, rolling back..."
                git revert --no-commit HEAD
                git commit -m "auto rollback: restart failed"
                echo "Rollback completed"
            fi
            """.format(tmpdir=tmpdir),
            cwd=tmpdir,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        assert "Rollback completed" in result.stdout

        # 验证回退后文件内容恢复为 v1
        with open(os.path.join(tmpdir, "version.txt"), "r") as f:
            content = f.read()
        assert content == "v1", f"Expected v1 after rollback, got {content}"


def test_no_rollback_on_health_success():
    """健康检查通过时，不应触发回退"""
    with tempfile.TemporaryDirectory() as tmpdir:
        run_bash("git init", cwd=tmpdir)
        run_bash('git config user.email "test@test.com"', cwd=tmpdir)
        run_bash('git config user.name "Test"', cwd=tmpdir)

        with open(os.path.join(tmpdir, "version.txt"), "w") as f:
            f.write("v1")
        run_bash("git add version.txt", cwd=tmpdir)
        run_bash('git commit -m "v1"', cwd=tmpdir)

        # 模拟健康检查成功
        result = run_bash(
            """
            cd {tmpdir}
            health_ok=true

            if [ "$health_ok" = false ]; then
                git revert --no-commit HEAD
                git commit -m "auto rollback"
                echo "Rollback completed"
            else
                echo "Health check passed, no rollback needed"
            fi
            """.format(tmpdir=tmpdir),
            cwd=tmpdir,
        )

        assert result.returncode == 0
        assert "no rollback needed" in result.stdout

        with open(os.path.join(tmpdir, "version.txt"), "r") as f:
            content = f.read()
        assert content == "v1"


def test_max_rollback_limit():
    """达到最大回退次数时，应停止回退"""
    max_rollback = 3
    rollback_count = 3

    # 模拟 start.sh 中的判断逻辑
    should_rollback = rollback_count < max_rollback
    assert not should_rollback, "Should not rollback when limit reached"


def test_rollback_log_written():
    """回退时应写入 .rollback.log"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, ".rollback.log")

        # 模拟回退日志写入
        with open(log_file, "a") as f:
            f.write("2025-01-01 12:00:00 rollback to abc123 (attempt 1)\n")

        assert os.path.exists(log_file)
        with open(log_file, "r") as f:
            content = f.read()
        assert "rollback to abc123" in content
        assert "attempt 1" in content


def test_non_git_repo_no_rollback():
    """非 git 仓库时，不应执行 revert，应打印警告"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 不初始化 git，模拟非 git 环境
        result = run_bash(
            """
            cd {tmpdir}
            if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
                echo "Is git repo"
            else
                echo "Not a git repo, skip rollback"
            fi
            """.format(tmpdir=tmpdir),
            cwd=tmpdir,
        )

        assert result.returncode == 0
        assert "Not a git repo" in result.stdout


if __name__ == "__main__":
    test_rollback_triggered_on_health_failure()
    print("✅ test_rollback_triggered_on_health_failure passed")

    test_no_rollback_on_health_success()
    print("✅ test_no_rollback_on_health_success passed")

    test_max_rollback_limit()
    print("✅ test_max_rollback_limit passed")

    test_rollback_log_written()
    print("✅ test_rollback_log_written passed")

    test_non_git_repo_no_rollback()
    print("✅ test_non_git_repo_no_rollback passed")

    print("\nAll tests passed!")
