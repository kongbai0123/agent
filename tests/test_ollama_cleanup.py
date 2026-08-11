import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from ollama_cleanup import monitor_cancel_release


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class OllamaCleanupTests(unittest.TestCase):
    def test_normal_release_does_not_force_unload(self):
        snapshots = [
            {"models": [{"name": "model-a"}]},
            {"models": []},
        ]
        posts = []

        def fake_get(*_args, **_kwargs):
            return FakeResponse(payload=snapshots.pop(0))

        result = monitor_cancel_release(
            ollama_url="http://ollama.test",
            tracked_models={"model-a"},
            protected_models=set(),
            preexisting_snapshot_known=True,
            resource_sampler=lambda: {"ram_free_gb": 12, "vram_free_gb": 6},
            grace_seconds=0.1,
            poll_seconds=0.1,
            request_get=fake_get,
            request_post=lambda *args, **kwargs: posts.append((args, kwargs)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result["state"], "released")
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["cleanup_performed"])
        self.assertEqual(posts, [])

    def test_timeout_unloads_only_owned_model(self):
        unloaded = False
        posts = []
        resources = iter((
            {"ram_free_gb": 8, "vram_free_gb": 2},
            {"ram_free_gb": 12, "vram_free_gb": 6},
        ))

        def fake_get(*_args, **_kwargs):
            models = [] if unloaded else [{"name": "model-a"}]
            return FakeResponse(payload={"models": models})

        def fake_post(*args, **kwargs):
            nonlocal unloaded
            posts.append((args, kwargs))
            unloaded = True
            return FakeResponse(payload={"done": True, "done_reason": "unload"})

        result = monitor_cancel_release(
            ollama_url="http://ollama.test",
            tracked_models={"model-a"},
            protected_models=set(),
            preexisting_snapshot_known=True,
            resource_sampler=lambda: next(resources),
            grace_seconds=0.1,
            poll_seconds=0.1,
            cleanup_wait_seconds=0.1,
            request_get=fake_get,
            request_post=fake_post,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result["state"], "cleaned")
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["cleanup_performed"])
        self.assertEqual(result["models_unloaded"], ["model-a"])
        self.assertEqual(result["resources_recovered"], {"ram_gb": 4.0, "vram_gb": 4.0})
        self.assertEqual(posts[0][1]["json"], {
            "model": "model-a", "messages": [], "keep_alive": 0, "stream": False,
        })

    def test_preexisting_model_is_protected(self):
        posts = []
        result = monitor_cancel_release(
            ollama_url="http://ollama.test",
            tracked_models={"shared-model"},
            protected_models={"shared-model"},
            preexisting_snapshot_known=True,
            resource_sampler=lambda: {"ram_free_gb": 10, "vram_free_gb": 4},
            request_get=lambda *_args, **_kwargs: FakeResponse(
                payload={"models": [{"name": "shared-model"}]}
            ),
            request_post=lambda *args, **kwargs: posts.append((args, kwargs)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result["state"], "protected")
        self.assertFalse(result["cleanup_performed"])
        self.assertEqual(posts, [])

    def test_unknown_initial_state_never_forces_unload(self):
        posts = []
        result = monitor_cancel_release(
            ollama_url="http://ollama.test",
            tracked_models={"model-a"},
            protected_models=set(),
            preexisting_snapshot_known=False,
            resource_sampler=lambda: {"ram_free_gb": 10, "vram_free_gb": 4},
            request_get=lambda *_args, **_kwargs: FakeResponse(
                payload={"models": [{"name": "model-a"}]}
            ),
            request_post=lambda *args, **kwargs: posts.append((args, kwargs)),
            sleep=lambda _seconds: None,
        )
        self.assertEqual(result["state"], "unavailable")
        self.assertTrue(result["warning"])
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
