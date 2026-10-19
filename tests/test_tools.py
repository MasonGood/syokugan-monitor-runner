import copy
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.audit_public import audit
from tools.import_baseline import validate_baseline


def baseline():
    return {
        "initialized": True,
        "source_mode": "full_lists_v1",
        "known_codes": ["2601001"],
        "active_restock_codes": ["2601002"],
        "monitored_sections": ["hc1", "hc3", "hc7"],
        "last_snapshot": {
            "hc1:2601001": {
                "code": "2601001",
                "title": "Example product",
                "release_date": "2026/10",
                "url": "https://syokugan-ohkoku.com/item.php?code=2601001",
                "section": "hc1",
                "image_url": "https://syokugan-ohkoku.com/images/2601001.jpg",
            },
        },
    }


class BaselineTests(unittest.TestCase):
    def test_valid_public_fields_are_preserved(self):
        data = baseline()
        self.assertEqual(validate_baseline(data), data)

    def test_unknown_fields_are_rejected_not_silently_published(self):
        data = baseline()
        data["private_note"] = "not public"
        with self.assertRaises(ValueError):
            validate_baseline(data)

    def test_nested_private_fields_are_rejected(self):
        data = baseline()
        data["last_snapshot"]["hc1:2601001"]["email"] = "fixture@example.invalid"
        with self.assertRaises(ValueError):
            validate_baseline(data)

    def test_nonpublic_origin_or_personal_title_is_rejected(self):
        for field, value in [
            ("url", "https://private.example.invalid/item?code=2601001"),
            ("image_url", "file:///private/photo.jpg"),
            ("title", "Contact fixture@example.invalid"),
        ]:
            with self.subTest(field=field):
                data = baseline()
                data["last_snapshot"]["hc1:2601001"][field] = value
                with self.assertRaises(ValueError):
                    validate_baseline(data)

    def test_wrong_schema_and_snapshot_identity_are_rejected(self):
        invalid = [
            {"initialized": False},
            {**baseline(), "source_mode": "old"},
            {**baseline(), "known_codes": ["../../private"]},
        ]
        wrong_key = copy.deepcopy(baseline())
        wrong_key["last_snapshot"]["hc1:2601001"]["code"] = "2601999"
        invalid.append(wrong_key)
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    validate_baseline(data)


class AuditTests(unittest.TestCase):
    def test_clean_source_and_invented_fixture_addresses_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "monitor.py").write_text('SITE = "https://syokugan-ohkoku.com/"\n')
            (root / "tests").mkdir()
            (root / "tests" / "test_monitor.py").write_text('"fixture@example.invalid"\n')
            self.assertEqual(audit(root), [])

    def test_personal_address_is_rejected_without_echoing_it(self):
        address = "invented.person@" + "example.com"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text(address)
            result = audit(root)
            self.assertTrue(result)
            self.assertNotIn(address, str(result))

    def test_secret_shaped_literal_is_rejected_without_echoing_it(self):
        token = "ghp_" + "x" * 36
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "monitor.py").write_text(f'TOKEN = "{token}"')
            result = audit(root)
            self.assertTrue(result)
            self.assertNotIn(token, str(result))

    def test_unapproved_files_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "personal.env").write_text("PRIVATE=example")
            self.assertTrue(audit(root))


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/monitor.yml"
        cls.workflow = path.read_text(encoding="utf8")
        cls.validation_step = cls.workflow.split(
            "      - name: Validate inputs\n", 1
        )[1].split("\n      - ", 1)[0]
        cls.validator = textwrap.dedent(
            cls.validation_step.split("          python3 - <<'PY'\n", 1)[1].split(
                "          PY", 1
            )[0]
        )

    def validate_inputs(self, mode, slot_id):
        return subprocess.run(
            [sys.executable, "-B", "-"],
            input=self.validator,
            env={"MODE": mode, "SLOT_ID": slot_id},
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_run_name_reserves_slot_correlation_for_monitor_mode(self):
        run_name = next(
            line.removeprefix("run-name: ")
            for line in self.workflow.splitlines()
            if line.startswith("run-name: ")
        )
        self.assertEqual(
            run_name,
            "${{ inputs.mode == 'monitor' && "
            "format('monitor {0}', inputs.slot_id || inputs.mode) || inputs.mode }}",
        )

    def test_input_validation_runs_first_with_dispatch_inputs(self):
        first_step = self.workflow.split("    steps:\n", 1)[1].splitlines()[0]
        self.assertEqual(first_step.strip(), "- name: Validate inputs")
        self.assertIn("          MODE: ${{ inputs.mode }}\n", self.validation_step)
        self.assertIn("          SLOT_ID: ${{ inputs.slot_id }}\n", self.validation_step)

    def test_monitor_mode_accepts_optional_valid_slot(self):
        for slot_id in ("", "slot_2026-10-19_001", "a" * 80):
            with self.subTest(slot_id=slot_id):
                result = self.validate_inputs("monitor", slot_id)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonmonitor_modes_accept_empty_slot(self):
        for mode in ("check", "test_notification"):
            with self.subTest(mode=mode):
                result = self.validate_inputs(mode, "")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_nonmonitor_modes_reject_any_nonempty_slot(self):
        for mode in ("check", "test_notification"):
            for slot_id in ("slot_2026-10-19_001", "a" * 80, " ", "\n"):
                with self.subTest(mode=mode, slot_id=slot_id):
                    result = self.validate_inputs(mode, slot_id)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("only valid in monitor mode", result.stderr)

    def test_monitor_mode_rejects_invalid_slots(self):
        for slot_id in ("a" * 81, "slot/123", " slot", "slot\n"):
            with self.subTest(slot_id=slot_id):
                result = self.validate_inputs("monitor", slot_id)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid slot identifier", result.stderr)

    def test_unknown_modes_are_rejected_before_slot_validation(self):
        for mode in ("", "invalid", "Monitor"):
            with self.subTest(mode=mode):
                result = self.validate_inputs(mode, "slot/123")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
