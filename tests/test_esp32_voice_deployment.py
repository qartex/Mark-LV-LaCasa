import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "esp32-satellite"


class ESP32VoiceDeploymentTests(unittest.TestCase):
    def test_bridge_unit_runs_repo_module_with_restart_and_private_queue(self):
        unit = (DEPLOY / "esp32-jarvis-bridge.service").read_text()
        self.assertIn("Restart=always", unit)
        self.assertIn("WorkingDirectory=/home/lacasa/Mark-LV-LaCasa", unit)
        self.assertIn("PYTHONPATH=/home/lacasa/Mark-LV-LaCasa:/home/lacasa/Mark-LV-LaCasa/scripts", unit)
        self.assertIn("-m scripts.esp32_jarvis_bridge", unit)
        self.assertIn("--learning-queue-dir", unit)
        self.assertIn("growraum-sprache-1-home-test.local", unit)
        self.assertNotIn("API_KEY=", unit)
        self.assertNotIn("CANNANAS_API_KEY=", unit)

    def test_learning_dispatch_timer_is_bounded_and_persistent(self):
        service = (DEPLOY / "jarvis-learning-dispatch.service").read_text()
        timer = (DEPLOY / "jarvis-learning-dispatch.timer").read_text()
        self.assertIn("Type=oneshot", service)
        self.assertIn("-m scripts.jarvis_learning_dispatch", service)
        self.assertIn("--webhook-url-file", service)
        self.assertIn("--webhook-secret-file", service)
        self.assertIn("OnUnitActiveSec=30s", timer)
        self.assertIn("Persistent=true", timer)

    def test_learning_pipeline_document_keeps_voice_requirement_untrusted(self):
        document = (ROOT / "docs" / "jarvis-learning-pipeline.md").read_text()
        lowered = document.lower()
        self.assertIn("untrusted", lowered)
        self.assertIn("tests", lowered)
        self.assertIn("review", lowered)
        self.assertIn("jarvis_learning_complete", document)
        self.assertIn("so, jetzt kannst du die funktion", lowered)
        self.assertNotIn("WEBHOOK_SECRET=", document)


if __name__ == "__main__":
    unittest.main()
