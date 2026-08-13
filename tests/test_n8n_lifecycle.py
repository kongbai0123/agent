import copy
import base64
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import n8n_lifecycle


READY_ISOLATION = {
    "isolation_ready": True,
    "blockers": [],
    "account": "WorkbenchN8n",
    "account_exists": True,
    "account_enabled": True,
    "account_non_admin": True,
    "credential_ready": True,
    "acl_ready": True,
    "account_sid": "S-1-5-21-1-2-3-1001",
}


class N8nLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.node = self.runtime / "tools" / "node" / "24.15.0" / "node.exe"
        self.paths = n8n_lifecycle.ManagedN8nPaths.from_runtime_root(
            self.runtime, node_executable=self.node
        )
        package_root = self.paths.tool_dir / "node_modules" / "n8n"
        self.paths.n8n_entry.parent.mkdir(parents=True)
        self.paths.n8n_entry.write_text("entry", encoding="utf-8")
        (package_root / "package.json").write_text(
            json.dumps({"version": "2.32.5"}), encoding="utf-8"
        )
        self.node.parent.mkdir(parents=True)
        self.node.write_bytes(b"node")

    def tearDown(self):
        self.temporary.cleanup()

    def test_environment_keeps_writable_homes_in_runtime_and_drops_secrets(self):
        environment = n8n_lifecycle.build_managed_environment(
            self.paths,
            source={
                "PATH": "safe-path",
                "SYSTEMROOT": "C:\\Windows",
                "DATABASE_URL": "must-not-pass",
                "N8N_API_KEY": "must-not-pass",
                "HERMES_API_SERVER_KEY": "must-not-pass",
            },
        )
        self.assertEqual(environment["N8N_USER_FOLDER"], str(self.paths.data_home))
        for name in (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "NPM_CONFIG_CACHE",
            "N8N_RESTRICT_FILE_ACCESS_TO",
        ):
            self.assertTrue(
                Path(environment[name]).is_relative_to(self.paths.runtime_root),
                f"{name} escaped runtime",
            )
        self.assertNotIn("DATABASE_URL", environment)
        self.assertNotIn("N8N_API_KEY", environment)
        self.assertNotIn("HERMES_API_SERVER_KEY", environment)
        self.assertEqual(environment["N8N_LISTEN_ADDRESS"], "127.0.0.1")
        self.assertEqual(environment["N8N_PORT"], "5678")
        self.assertEqual(environment["N8N_COMMUNITY_PACKAGES_ENABLED"], "false")
        self.assertEqual(environment["N8N_BLOCK_ENV_ACCESS_IN_NODE"], "true")
        excluded = json.loads(environment["NODES_EXCLUDE"])
        self.assertIn("n8n-nodes-base.code", excluded)
        self.assertIn("n8n-nodes-base.executeCommand", excluded)

    def test_installation_is_exactly_pinned_without_running_n8n(self):
        result = n8n_lifecycle.validate_installation(self.paths, probe_node=False)
        self.assertTrue(result["valid"])
        self.assertEqual(result["version"], "2.32.5")
        package_path = self.paths.tool_dir / "node_modules" / "n8n" / "package.json"
        package_path.write_text(json.dumps({"version": "2.33.0"}), encoding="utf-8")
        result = n8n_lifecycle.validate_installation(self.paths, probe_node=False)
        self.assertFalse(result["valid"])
        self.assertIn("n8n_version_mismatch", result["issues"])

    def test_status_reports_isolation_blocker_and_does_not_start(self):
        isolation = {
            "isolation_ready": False,
            "blockers": ["service_account_missing"],
        }
        manager = n8n_lifecycle.ManagedN8nLifecycle(
            self.paths,
            require_d_drive=False,
            isolation_checker=lambda _paths: isolation,
        )
        with patch.object(
            n8n_lifecycle,
            "inspect_port",
            return_value=n8n_lifecycle.PortInspection("free", (), "no_listener"),
        ):
            status = manager.status()
            self.assertFalse(status["isolation_ready"])
            self.assertEqual(status["isolation_blockers"], ["service_account_missing"])
            with self.assertRaises(n8n_lifecycle.N8nConfigurationError) as context:
                manager.start(timeout_seconds=1)
        self.assertIn("service_account_missing", context.exception.details["blockers"])

    def test_start_never_falls_back_to_interactive_user(self):
        manager = n8n_lifecycle.ManagedN8nLifecycle(
            self.paths,
            require_d_drive=False,
            isolation_checker=lambda _paths: READY_ISOLATION,
            isolated_launcher=None,
        )
        with self.assertRaises(n8n_lifecycle.N8nConfigurationError) as context:
            manager.start(timeout_seconds=1)
        self.assertEqual(
            context.exception.details["blockers"],
            ["isolated_launcher_unconfigured"],
        )

    def test_windows_run_as_launcher_uses_only_dpapi_password_and_pinned_command(self):
        self.paths.secrets_dir.mkdir(parents=True)
        ciphertext = b"protected-launch-ciphertext-123456"
        self.paths.launch_credential_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "account": f"{n8n_lifecycle.socket.gethostname()}\\WorkbenchN8n",
                    "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                    "created_at": "2026-08-13T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

        class FakeApi:
            def __init__(self):
                self.spawned = None

            def unprotect(self, value):
                self.ciphertext = value
                return bytearray(b"a-usable-test-password-that-is-long-enough")

            def spawn(self, **options):
                self.spawned = {**options, "password_utf8": bytes(options["password_utf8"])}
                return Mock(pid=8123)

        api = FakeApi()
        launcher = n8n_lifecycle.WindowsRunAsLauncher(self.paths, api=api)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = launcher(
                command=list(n8n_lifecycle._command(self.paths)),
                cwd=str(self.paths.tool_dir),
                env=n8n_lifecycle.build_managed_environment(self.paths, source={"PATH": "safe"}),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                creationflags=0,
            )
        self.assertEqual(process.pid, 8123)
        self.assertEqual(api.ciphertext, ciphertext)
        self.assertEqual(api.spawned["username"], "WorkbenchN8n")
        self.assertEqual(tuple(api.spawned["command"]), n8n_lifecycle._command(self.paths))
        self.assertNotIn("password", json.dumps(api.spawned["env"]).casefold())
        with self.assertRaises(n8n_lifecycle.N8nConfigurationError):
            launcher(
                command=[str(self.node), "unreviewed.js"],
                cwd=str(self.paths.tool_dir), env={}, stdin=subprocess.DEVNULL,
                stdout=None, stderr=None, shell=False, creationflags=0,
            )

    @unittest.skipUnless(os.name == "nt", "Windows HANDLE regression")
    def test_windows_run_as_api_declares_pointer_sized_duplicate_handle_signatures(self):
        api = n8n_lifecycle._WindowsRunAsApi()
        self.assertIs(api.kernel32.GetCurrentProcess.restype, api.wintypes.HANDLE)
        self.assertIs(api.kernel32.DuplicateHandle.restype, api.wintypes.BOOL)
        self.assertEqual(len(api.kernel32.DuplicateHandle.argtypes), 7)

    def test_status_and_stop_fail_closed_for_unknown_listener(self):
        manager = n8n_lifecycle.ManagedN8nLifecycle(
            self.paths,
            require_d_drive=False,
            isolation_checker=lambda _paths: READY_ISOLATION,
        )
        listener = n8n_lifecycle.PortInspection("listening", (4444,), "listener_found")
        with patch.object(n8n_lifecycle, "inspect_port", return_value=listener):
            status = manager.status()
            self.assertEqual(status["state"], "port_conflict")
            with self.assertRaises(n8n_lifecycle.N8nOwnershipError):
                manager.stop()

    def test_windows_console_signal_system_error_uses_verified_stop_fallback(self):
        manager = n8n_lifecycle.ManagedN8nLifecycle(
            self.paths,
            require_d_drive=False,
            isolation_checker=lambda _paths: READY_ISOLATION,
            isolated_launcher=None,
        )
        process = Mock()
        process.children.return_value = []
        process.send_signal.side_effect = SystemError("invalid console handle")
        process.wait.return_value = 0
        with patch.object(
            n8n_lifecycle,
            "verify_owned_process",
            return_value=(True, "owned", process),
        ):
            manager._stop_verified_record(Mock(), graceful_seconds=1)
        process.kill.assert_called_once_with()

    def test_process_ownership_rejects_pid_reuse_and_command_changes(self):
        command = n8n_lifecycle._command(self.paths)
        record = n8n_lifecycle.LifecycleRecord(
            schema_version=1,
            owner="local-ai-workbench",
            owner_id="a" * 32,
            pid=1234,
            process_created_at=100.0,
            started_at="2026-01-01T00:00:00+00:00",
            version="2.32.5",
            node_version="24.15.0",
            node_executable=str(self.paths.node_executable),
            n8n_entry=str(self.paths.n8n_entry),
            command_sha256=n8n_lifecycle._command_digest(command),
            host="127.0.0.1",
            port=5678,
        )
        process = Mock()
        process.create_time.return_value = 101.0
        process.exe.return_value = str(self.paths.node_executable)
        process.cmdline.return_value = list(command)
        with patch.object(n8n_lifecycle.psutil, "Process", return_value=process):
            owned, reason, _ = n8n_lifecycle.verify_owned_process(
                self.paths, record, listener_pids=(1234,)
            )
        self.assertFalse(owned)
        self.assertEqual(reason, "pid_reused")

        process.create_time.return_value = 100.0
        process.cmdline.return_value = [*command[:-1], "worker"]
        with patch.object(n8n_lifecycle.psutil, "Process", return_value=process):
            owned, reason, _ = n8n_lifecycle.verify_owned_process(
                self.paths, record, listener_pids=(1234,)
            )
        self.assertFalse(owned)
        self.assertEqual(reason, "process_command_mismatch")

    def test_stray_profile_helper_is_read_only_and_rejects_extra_data(self):
        managed_config = self.paths.n8n_dir / "config"
        managed_config.parent.mkdir(parents=True)
        managed_config.write_text(json.dumps({"encryptionKey": "m" * 32}), encoding="utf-8")
        profile = self.root / "user-profile" / ".n8n"
        profile.mkdir(parents=True)
        config = profile / "config"
        config.write_text(json.dumps({"encryptionKey": "c" * 32}), encoding="utf-8")
        before = config.read_bytes()
        report = n8n_lifecycle.inspect_stray_user_profile(
            self.paths, profile_dir=profile
        )
        self.assertTrue(report["safe_candidate"])
        self.assertFalse(report["matches_managed_key"])
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue(profile.is_dir())

        (profile / "database.sqlite").write_bytes(b"database")
        report = n8n_lifecycle.inspect_stray_user_profile(
            self.paths, profile_dir=profile
        )
        self.assertFalse(report["safe_candidate"])
        self.assertEqual(report["reason"], "contains_additional_data")


class N8nWorkflowTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.template_dir = cls.root / "config" / "n8n-workflows"

    def _load(self, name):
        return n8n_lifecycle.load_workflow_template(self.template_dir / name)

    def test_reviewed_templates_validate_and_bind_without_activation(self):
        for name in (
            "workbench-gmail-inbound-v1.json",
            "workbench-gmail-send-v1.json",
        ):
            workflow = self._load(name)
            result = n8n_lifecycle.validate_workflow_template(workflow)
            self.assertTrue(result["valid"])
            self.assertFalse(result["active"])
            bound = n8n_lifecycle.bind_workflow_credentials(
                workflow,
                workbench_hmac_credential_id="hmacCredential1",
                workbench_webhook_credential_id="webhookCredential1",
                gmail_credential_id="gmailCredential1",
                workflow_key="workbench-gmail-inbound-v1",
            )
            self.assertFalse(bound["active"])
            n8n_lifecycle.validate_workflow_template(
                bound, require_placeholders=False
            )
            self.assertIn(
                n8n_lifecycle.WORKBENCH_HMAC_CREDENTIAL_PLACEHOLDER,
                json.dumps(workflow),
            )

    def test_templates_reject_activation_arbitrary_url_and_code(self):
        workflow = self._load("workbench-gmail-inbound-v1.json")
        activated = copy.deepcopy(workflow)
        activated["active"] = True
        with self.assertRaises(n8n_lifecycle.N8nTemplateError):
            n8n_lifecycle.validate_workflow_template(activated)

        arbitrary = copy.deepcopy(workflow)
        arbitrary["nodes"][-1]["parameters"]["url"] = "https://example.com/collect"
        with self.assertRaises(n8n_lifecycle.N8nTemplateError):
            n8n_lifecycle.validate_workflow_template(arbitrary)

        code = copy.deepcopy(workflow)
        code["nodes"][1]["type"] = "n8n-nodes-base.code"
        with self.assertRaises(n8n_lifecycle.N8nTemplateError):
            n8n_lifecycle.validate_workflow_template(code)

    def test_template_cannot_bind_without_both_credentials(self):
        workflow = self._load("workbench-gmail-inbound-v1.json")
        with self.assertRaises(n8n_lifecycle.N8nTemplateError):
            n8n_lifecycle.bind_workflow_credentials(
                workflow,
                workbench_hmac_credential_id="",
                workbench_webhook_credential_id="webhookCredential1",
                gmail_credential_id="gmailCredential1",
                workflow_key="workbench-gmail-inbound-v1",
            )

        with self.assertRaises(n8n_lifecycle.N8nTemplateError):
            n8n_lifecycle.bind_workflow_credentials(
                workflow,
                workbench_hmac_credential_id="sameCredential",
                workbench_webhook_credential_id="sameCredential",
                gmail_credential_id="gmailCredential1",
                workflow_key="workbench-gmail-inbound-v1",
            )

    def test_templates_enforce_signed_contract_and_draft_send_sequence(self):
        inbound = self._load("workbench-gmail-inbound-v1.json")
        serialized = json.dumps(inbound)
        self.assertIn("label:Workbench-Agent in:inbox -in:sent", serialized)
        self.assertNotIn("workflow_instruction", serialized)
        self.assertIn("X-N8N-Signature", serialized)

        send = self._load("workbench-gmail-send-v1.json")
        serialized = json.dumps(send)
        self.assertIn(n8n_lifecycle.WORKBENCH_WEBHOOK_CREDENTIAL_PLACEHOLDER, serialized)
        self.assertIn(n8n_lifecycle.GMAIL_DRAFT_SEND_URL, serialized)
        self.assertNotIn("$json.body.to", serialized)
        self.assertNotIn("$json.body.subject", serialized)
        self.assertNotIn("$json.body.body", serialized)

    def test_inbound_thread_context_is_bounded_metadata_only_and_retry_safe(self):
        inbound = self._load("workbench-gmail-inbound-v1.json")
        nodes = {node["name"]: node for node in inbound["nodes"]}

        self.assertEqual(
            nodes["Gmail Thread Get"]["parameters"],
            {
                "resource": "thread",
                "operation": "get",
                "threadId": "={{$json.threadId}}",
                "simple": False,
                "options": {"returnOnlyMessages": False},
            },
        )
        context_rows = {
            row["name"]: row["value"]
            for row in nodes["Build Gmail Context"]["parameters"]
            ["assignments"]["assignments"]
        }
        self.assertIn(".slice(-20).reverse()", context_rows["thread_messages"])
        self.assertIn(
            "remaining=100000-currentBody.length", context_rows["thread_messages"]
        )
        self.assertIn("remaining-=text.length", context_rows["thread_messages"])
        self.assertIn("part.body?.attachmentId", context_rows["attachments"])
        self.assertIn("metadata.length>=50", context_rows["attachments"])
        for forbidden in (
            "body.data", "content:", "path:", "url:", "attachmentsBinary"
        ):
            self.assertNotIn(forbidden, context_rows["attachments"])
        self.assertFalse(
            nodes["Gmail Search"]["parameters"]["options"]["downloadAttachments"]
        )

        submit_nodes = [
            node for node in inbound["nodes"]
            if node["type"] == "n8n-nodes-base.httpRequest"
            and node["parameters"].get("url") == n8n_lifecycle.GMAIL_INBOUND_URL
        ]
        nonce_nodes = [
            node for node in inbound["nodes"]
            if node["type"] == "n8n-nodes-base.crypto"
            and node["parameters"].get("action") == "generate"
        ]
        sign_nodes = [
            node for node in inbound["nodes"]
            if node["type"] == "n8n-nodes-base.crypto"
            and node["parameters"].get("action") == "hmac"
        ]
        self.assertEqual(len(submit_nodes), 3)
        self.assertEqual(len(nonce_nodes), 3)
        self.assertEqual(len(sign_nodes), 3)
        self.assertTrue(
            all(node.get("onError") == "continueRegularOutput" for node in submit_nodes)
        )
        self.assertTrue(
            all(
                node["parameters"]["options"]["response"]["response"] == {
                    "fullResponse": True,
                    "neverError": True,
                    "responseFormat": "json",
                }
                for node in submit_nodes
            )
        )
        self.assertNotIn("retryOnFail", json.dumps(inbound))

        event_body = {
            row["name"]: row["value"]
            for row in nodes["Prepare Gmail Event"]["parameters"]
            ["assignments"]["assignments"]
        }
        self.assertIn("event_id:'gmail-'+$json.gmail_message_id", event_body["request_body"])
        for attempt in range(1, 4):
            attempt_rows = {
                row["name"]: row["value"]
                for row in nodes[f"Prepare Attempt {attempt} Auth"]["parameters"]
                ["assignments"]["assignments"]
            }
            self.assertEqual(
                attempt_rows["request_body"],
                "={{$('Prepare Gmail Event').item.json.request_body}}",
            )
            self.assertIn("Date.now()", attempt_rows["timestamp"])

        accepted_predecessors = {
            source
            for source, outputs in inbound["connections"].items()
            for branch in outputs["main"]
            for target in branch
            if target["node"] == "Workbench Label Available"
        }
        self.assertEqual(
            accepted_predecessors,
            {"Accepted Attempt 1", "Accepted Attempt 2", "Accepted Attempt 3"},
        )

    def test_inbound_validator_rejects_context_retry_and_acknowledgement_drift(self):
        inbound = self._load("workbench-gmail-inbound-v1.json")

        def node(workflow, name):
            return next(item for item in workflow["nodes"] if item["name"] == name)

        mutations = []

        thread_limit = copy.deepcopy(inbound)
        context = node(thread_limit, "Build Gmail Context")
        thread_row = next(
            row for row in context["parameters"]["assignments"]["assignments"]
            if row["name"] == "thread_messages"
        )
        thread_row["value"] = thread_row["value"].replace(".slice(-20)", ".slice(-21)")
        mutations.append(("thread_limit", thread_limit))

        attachment_content = copy.deepcopy(inbound)
        context = node(attachment_content, "Build Gmail Context")
        attachment_row = next(
            row for row in context["parameters"]["assignments"]["assignments"]
            if row["name"] == "attachments"
        )
        attachment_row["value"] = attachment_row["value"].replace(
            "size_bytes:", "content:part.body.data,size_bytes:"
        )
        mutations.append(("attachment_content", attachment_content))

        native_retry = copy.deepcopy(inbound)
        node(native_retry, "Submit Attempt 1")["retryOnFail"] = True
        mutations.append(("native_nonce_reuse_retry", native_retry))

        retry_4xx = copy.deepcopy(inbound)
        retry_if = node(retry_4xx, "Retryable Attempt 1")
        retry_if["parameters"]["conditions"]["conditions"][0]["leftValue"] = (
            "={{Boolean($json.error)||Number($json.statusCode||0)>=400}}"
        )
        mutations.append(("retry_4xx", retry_4xx))

        unsafe_ack = copy.deepcopy(inbound)
        unsafe_ack["connections"]["Accepted Attempt 1"]["main"][0][0]["node"] = (
            "Remove Workbench Label"
        )
        mutations.append(("label_before_guard", unsafe_ack))

        missing_full_response = copy.deepcopy(inbound)
        response = node(missing_full_response, "Submit Attempt 1")["parameters"]
        response["options"]["response"]["response"]["fullResponse"] = False
        mutations.append(("missing_full_response", missing_full_response))

        for name, workflow in mutations:
            with self.subTest(name=name):
                with self.assertRaises(n8n_lifecycle.N8nTemplateError):
                    n8n_lifecycle.validate_workflow_template(workflow)

    def test_policy_file_matches_executable_contract(self):
        policy = n8n_lifecycle.validate_managed_policy_file(
            self.root / "config" / "n8n-managed.json"
        )
        self.assertEqual(policy["n8n_version"], "2.32.5")
        self.assertEqual(policy["port"], 5678)

    def test_isolation_setup_uses_windows_powershell_compatible_csprng(self):
        script = (self.root / "scripts" / "setup_managed_n8n_isolation.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("RandomNumberGenerator]::Create()", script)
        self.assertIn("$generator.GetBytes($bytes)", script)
        self.assertNotIn("RandomNumberGenerator]::Fill", script)
        self.assertIn("Add-Type -AssemblyName System.Security", script)
        description = "Low-privilege Workbench n8n account"
        self.assertIn(f'-Description "{description}"', script)
        self.assertLessEqual(len(description), 48)
        self.assertIn('$descendants = Join-Path $Path "*"', script)
        self.assertIn('& icacls.exe $descendants "/reset" "/T"', script)

    def test_database_readiness_is_read_only_and_requires_separate_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            paths = n8n_lifecycle.ManagedN8nPaths.from_runtime_root(runtime)
            paths.n8n_dir.mkdir(parents=True)
            database_path = paths.n8n_dir / "database.sqlite"
            connection = sqlite3.connect(database_path)
            connection.executescript(
                """
                CREATE TABLE workflow_entity (
                    id TEXT PRIMARY KEY, name TEXT, active INTEGER, isArchived INTEGER,
                    settings TEXT, staticData TEXT, pinData TEXT, activeVersionId TEXT
                );
                CREATE TABLE workflow_published_version (
                    workflowId TEXT PRIMARY KEY, publishedVersionId TEXT
                );
                CREATE TABLE workflow_history (
                    versionId TEXT PRIMARY KEY, nodes TEXT, connections TEXT
                );
                CREATE TABLE credentials_entity (
                    id TEXT PRIMARY KEY, type TEXT, data TEXT
                );
                """
            )
            connection.executemany(
                "INSERT INTO credentials_entity(id,type,data) VALUES (?,?,?)",
                [
                    ("hmacCredential1", "crypto", "encrypted-credential-data"),
                    ("webhookCredential1", "httpHeaderAuth", "encrypted-credential-data"),
                    ("gmailCredential1", "gmailOAuth2", "encrypted-credential-data"),
                ],
            )
            for index, name in enumerate(
                ("workbench-gmail-inbound-v1.json", "workbench-gmail-send-v1.json")
            ):
                workflow = n8n_lifecycle.bind_workflow_credentials(
                    self._load(name),
                    workbench_hmac_credential_id="hmacCredential1",
                    workbench_webhook_credential_id="webhookCredential1",
                    gmail_credential_id="gmailCredential1",
                    workflow_key="workbench-gmail-inbound-v1",
                )
                workflow_id = f"workflow-{index}"
                version_id = f"version-{index}"
                connection.execute(
                    "INSERT INTO workflow_entity VALUES (?,?,?,?,?,?,?,?)",
                    (
                        workflow_id, workflow["name"], 1, 0,
                        json.dumps(workflow["settings"]),
                        json.dumps(workflow["staticData"]),
                        json.dumps(workflow["pinData"]), version_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO workflow_published_version VALUES (?,?)",
                    (workflow_id, version_id),
                )
                connection.execute(
                    "INSERT INTO workflow_history VALUES (?,?,?)",
                    (version_id, json.dumps(workflow["nodes"]), json.dumps(workflow["connections"])),
                )
            connection.commit()
            before = database_path.read_bytes()
            report = n8n_lifecycle.inspect_gmail_workflows_readiness(
                paths, database_path=database_path
            )
            self.assertTrue(report["ready"], report)
            self.assertTrue(n8n_lifecycle.gmail_workflows_ready(paths, database_path=database_path))
            self.assertEqual(before, database_path.read_bytes())
            rendered = json.dumps(report)
            self.assertNotIn("hmacCredential1", rendered)
            self.assertNotIn("gmailCredential1", rendered)

            connection.execute(
                "UPDATE credentials_entity SET type='httpHeaderAuth' WHERE id='hmacCredential1'"
            )
            connection.commit()
            report = n8n_lifecycle.inspect_gmail_workflows_readiness(
                paths, database_path=database_path
            )
            self.assertFalse(report["ready"])
            self.assertIn("hmac_credential_missing", report["blockers"])
            connection.close()


if __name__ == "__main__":
    unittest.main()
