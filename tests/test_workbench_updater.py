import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update_workbench.ps1"
POWERSHELL = Path(
    os.environ.get("SystemRoot", r"C:\Windows")
) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def _run(
    command: list[str],
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd)


def _json_result(completed: subprocess.CompletedProcess) -> dict:
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"missing updater JSON: {completed.stderr}")
    return json.loads(lines[-1])


@unittest.skipUnless(os.name == "nt" and POWERSHELL.exists(), "Windows updater contract")
class WorkbenchUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workbench-updater-test-")
        self.base = Path(self.temporary.name)
        self.author = self.base / "author"
        self.remote = self.base / "remote.git"
        self.install = self.base / "install"

        self.author.mkdir()
        _git(self.author, "init", "-b", "main")
        _git(self.author, "config", "user.name", "Updater Test")
        _git(self.author, "config", "user.email", "updater@example.invalid")
        (self.author / "scripts").mkdir()
        shutil.copy2(UPDATER, self.author / "scripts" / "update_workbench.ps1")
        (self.author / "app.txt").write_text("v1\n", encoding="utf-8")
        (self.author / ".gitignore").write_text(
            ".venv/\nruntime/\n",
            encoding="utf-8",
        )
        _git(self.author, "add", ".")
        _git(self.author, "commit", "-m", "initial")
        _git(self.base, "init", "--bare", str(self.remote))
        _git(self.author, "remote", "add", "origin", str(self.remote))
        _git(self.author, "push", "-u", "origin", "main")
        _git(self.base, "clone", "--branch", "main", str(self.remote), str(self.install))

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, value: str) -> None:
        (self.author / "app.txt").write_text(value + "\n", encoding="utf-8")
        _git(self.author, "add", "app.txt")
        _git(self.author, "commit", "-m", f"publish {value}")
        _git(self.author, "push", "origin", "main")

    def publish_path(self, relative: str, value: str, force: bool = False) -> None:
        target = self.author / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value + "\n", encoding="utf-8")
        add_args = ["add"]
        if force:
            add_args.append("-f")
        add_args.append(relative)
        _git(self.author, *add_args)
        _git(self.author, "commit", "-m", f"publish {relative}")
        _git(self.author, "push", "origin", "main")

    def invoke(self, mode: str, *extra: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKBENCH_RUNTIME_DIR"] = str(self.install / "runtime")
        return _run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.install / "scripts" / "update_workbench.ps1"),
                "-Mode",
                mode,
                "-RepositoryRoot",
                str(self.install),
                "-ExpectedRemoteUrl",
                str(self.remote),
                "-OutputJson",
                "-TestAllowCustomSource",
                *extra,
            ],
            cwd=self.install,
            check=False,
            env=env,
        )

    def invoke_without_repository_root(
        self,
        mode: str,
        *extra: str,
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["WORKBENCH_RUNTIME_DIR"] = str(self.install / "runtime")
        return _run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.install / "scripts" / "update_workbench.ps1"),
                "-Mode",
                mode,
                "-ExpectedRemoteUrl",
                str(self.remote),
                "-OutputJson",
                "-TestAllowCustomSource",
                *extra,
            ],
            cwd=self.install,
            check=False,
            env=env,
        )

    def test_check_and_apply_fast_forward_without_touching_user_data(self):
        secret = self.install / "runtime" / "secrets" / "model-providers.json"
        secret.parent.mkdir(parents=True)
        secret.write_text('{"encrypted":"unchanged"}', encoding="utf-8")
        self.publish("v2")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 10, checked.stderr)
        status = _json_result(checked)
        self.assertEqual(status["status"], "available")
        self.assertEqual(status["behind_by"], 1)

        applied = self.invoke("Apply", "-SkipValidation", "-SkipRestart")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = _json_result(applied)
        self.assertEqual(result["status"], "applied")
        self.assertEqual((self.install / "app.txt").read_text(encoding="utf-8"), "v2\n")
        self.assertEqual(secret.read_text(encoding="utf-8"), '{"encrypted":"unchanged"}')

    def test_repository_root_defaults_from_the_script_location(self):
        self.publish("v2")

        checked = self.invoke_without_repository_root("Check")
        self.assertEqual(checked.returncode, 10, checked.stderr)
        result = _json_result(checked)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["behind_by"], 1)

    def test_tracked_local_change_blocks_update_without_stash_or_reset(self):
        self.publish("v2")
        (self.install / "app.txt").write_text("local work\n", encoding="utf-8")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("local changes", result["message"])
        self.assertEqual(
            (self.install / "app.txt").read_text(encoding="utf-8"),
            "local work\n",
        )

    def test_wrong_remote_identity_is_blocked_before_update(self):
        self.publish("v2")
        completed = _run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.install / "scripts" / "update_workbench.ps1"),
                "-Mode",
                "Check",
                "-RepositoryRoot",
                str(self.install),
                "-ExpectedRemoteUrl",
                "https://github.com/example/not-the-workbench",
                "-OutputJson",
                "-TestAllowCustomSource",
            ],
            cwd=self.install,
            check=False,
            env={
                **os.environ,
                "WORKBENCH_RUNTIME_DIR": str(self.install / "runtime"),
            },
        )
        self.assertEqual(completed.returncode, 20, completed.stderr)
        result = _json_result(completed)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("source mismatch", result["message"])
        self.assertEqual((self.install / "app.txt").read_text(encoding="utf-8"), "v1\n")

    def test_feature_branch_is_blocked(self):
        self.publish("v2")
        _git(self.install, "checkout", "-b", "feature/local-work")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("require the 'main' branch", result["message"])
        self.assertEqual((self.install / "app.txt").read_text(encoding="utf-8"), "v1\n")

    def test_nonignored_untracked_file_blocks_update(self):
        self.publish("v2")
        local_note = self.install / "local-construction-note.txt"
        local_note.write_text("keep me\n", encoding="utf-8")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("local changes", result["message"])
        self.assertEqual(local_note.read_text(encoding="utf-8"), "keep me\n")

    def test_dependency_lock_change_requires_full_package(self):
        self.publish_path("backend/requirements.lock", "locked")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertIn("requires a full packaged update", result["message"])

    def test_venv_change_requires_full_package(self):
        self.publish_path(".venv/remote-owned.txt", "never merge", force=True)

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertIn("requires a full packaged update", result["message"])

    def test_post_merge_failure_rolls_back_code_and_runtime(self):
        original_head = _git(self.install, "rev-parse", "HEAD").stdout.strip()
        secret = self.install / "runtime" / "secrets" / "model-providers.json"
        secret.parent.mkdir(parents=True)
        secret.write_text('{"encrypted":"unchanged"}', encoding="utf-8")
        self.publish("v2")

        failed = self.invoke(
            "Apply",
            "-SkipValidation",
            "-SkipRestart",
            "-TestFailurePoint",
            "AfterMerge",
        )
        self.assertEqual(failed.returncode, 40, failed.stderr)
        result = _json_result(failed)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(_git(self.install, "rev-parse", "HEAD").stdout.strip(), original_head)
        self.assertEqual((self.install / "app.txt").read_text(encoding="utf-8"), "v1\n")
        self.assertEqual(secret.read_text(encoding="utf-8"), '{"encrypted":"unchanged"}')
        rollback_path = self.install / "runtime" / "update" / "previous-version.json"
        update_log = self.install / "runtime" / "update" / "update.log"
        self.assertTrue(
            rollback_path.exists(),
            failed.stdout
            + failed.stderr
            + (update_log.read_text(encoding="utf-8-sig") if update_log.exists() else ""),
        )
        rollback_record = json.loads(rollback_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(rollback_record["status"], "rolled_back")

    def test_diverged_history_is_blocked(self):
        self.publish("v2")
        _git(self.install, "config", "user.name", "Updater Test")
        _git(self.install, "config", "user.email", "updater@example.invalid")
        (self.install / "local.txt").write_text("local\n", encoding="utf-8")
        _git(self.install, "add", "local.txt")
        _git(self.install, "commit", "-m", "local commit")

        checked = self.invoke("Check")
        self.assertEqual(checked.returncode, 20, checked.stderr)
        result = _json_result(checked)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("histories have diverged", result["message"])


if __name__ == "__main__":
    unittest.main()
