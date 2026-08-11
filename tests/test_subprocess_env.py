import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from subprocess_env import (
    agent_subprocess_env,
    is_allowed_subprocess_env_name,
    is_secret_env_name,
)


class AgentSubprocessEnvironmentTests(unittest.TestCase):
    def test_recognizes_supported_credential_names(self):
        for name in (
            "OPENAI_API_KEY",
            "N8N_API_KEY",
            "GH_TOKEN",
            "SERVICE_SECRET",
            "DATABASE_PASSWORD",
            "AUTH_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AZURE_OPENAI_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GITHUB_PAT",
            "DATABASE_URL",
            "SERVICE_CONNECTION_STRING",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_secret_env_name(name))
        self.assertFalse(is_secret_env_name("PATH"))
        self.assertFalse(is_secret_env_name("PYTHONUTF8"))

    def test_allowlist_contains_only_required_operational_names(self):
        for name in (
            "PATH",
            "TEMP",
            "SYSTEMROOT",
            "USERPROFILE",
            "PROCESSOR_ARCHITECTURE",
            "PYTHONUTF8",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_allowed_subprocess_env_name(name))
        for name in (
            "AGENT_SAFE_VALUE",
            "DATABASE_URL",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "HTTP_PROXY",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_allowed_subprocess_env_name(name))

    def test_real_child_process_cannot_read_unknown_or_secret_parent_values(self):
        with patch.dict(
            os.environ,
            {
                "TEST_API_KEY": "must-not-leak",
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "AZURE_OPENAI_KEY": "must-not-leak",
                "GOOGLE_APPLICATION_CREDENTIALS": "must-not-leak",
                "DATABASE_URL": "postgres://user:password@example.test/db",
                "AGENT_SAFE_VALUE": "also-denied",
                "PYTHONUTF8": "1",
            },
            clear=False,
        ):
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import os;"
                        "names=('TEST_API_KEY','AWS_SECRET_ACCESS_KEY',"
                        "'AZURE_OPENAI_KEY','GOOGLE_APPLICATION_CREDENTIALS',"
                        "'DATABASE_URL','AGENT_SAFE_VALUE');"
                        "print('|'.join(os.getenv(name,'<missing>') for name in names));"
                        "print(os.getenv('PYTHONUTF8','<missing>'))"
                    ),
                ],
                capture_output=True,
                text=True,
                check=True,
                env=agent_subprocess_env(),
            )
        self.assertEqual(
            completed.stdout.splitlines(),
            ["|".join(["<missing>"] * 6), "1"],
        )

    def test_explicit_extra_cannot_reintroduce_secret_or_unknown_name(self):
        environment = agent_subprocess_env(
            {
                "PYTHONUTF8": "1",
                "SECONDARY_TOKEN": "blocked",
                "AGENT_SAFE_VALUE": "also-blocked",
            }
        )
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertNotIn("SECONDARY_TOKEN", environment)
        self.assertNotIn("AGENT_SAFE_VALUE", environment)

    def test_every_backend_subprocess_call_sets_an_explicit_environment(self):
        missing = []
        for path in BACKEND.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "subprocess"
                    and function.attr in {"run", "Popen", "check_output", "check_call"}
                ):
                    continue
                if "env" not in {keyword.arg for keyword in node.keywords}:
                    missing.append(f"{path.relative_to(BACKEND)}:{node.lineno}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
