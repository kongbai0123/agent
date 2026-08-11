import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import secret_store


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "provider-secrets.json"
        self.environment = patch.dict(
            os.environ,
            {"WORKBENCH_SECRET_STORE_PATH": str(self.path)},
            clear=False,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_dpapi_round_trip_never_writes_plaintext(self):
        status = secret_store.set_provider_secret("openrouter", "sk-example-1234")
        self.assertEqual(status, {
            "provider_id": "openrouter",
            "configured": True,
            "last4": "1234",
        })
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sk-example-1234", raw)
        self.assertEqual(secret_store.get_provider_secret("openrouter"), "sk-example-1234")

        public = secret_store.provider_secret_statuses(["openrouter"])
        self.assertEqual(public[0]["last4"], "1234")
        self.assertNotIn("ciphertext", public[0])
        self.assertNotIn("api_key", json.dumps(public))

    def test_delete_removes_provider_secret(self):
        secret_store.set_provider_secret("nvidia", "nvapi-example")
        self.assertTrue(secret_store.delete_provider_secret("nvidia"))
        self.assertEqual(secret_store.get_provider_secret("nvidia"), "")
        self.assertFalse(secret_store.delete_provider_secret("nvidia"))


if __name__ == "__main__":
    unittest.main()
