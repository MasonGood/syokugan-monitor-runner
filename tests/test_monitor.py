from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import runpy
import smtplib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

try:
    import monitor
except ModuleNotFoundError as error:
    if error.name != "monitor":
        raise
    monitor = None


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_AUTH_CODE = "SYNTHETIC_TEST_AUTH_CODE_NOT_A_REAL_SECRET"
TEST_ENV = {
    "SMTP_USERNAME": "fixture-sender@example.invalid",
    "SMTP_AUTH_CODE": SYNTHETIC_AUTH_CODE,
    "EMAIL_TO": "fixture-recipient@example.invalid",
    "MONITOR_SECTIONS": "hc1,hc3,hc7",
}
LIST_1_URL = "https://syokugan-ohkoku.com/list-shohin.php?id=1"
LIST_3_URL = "https://syokugan-ohkoku.com/list-shohin.php?id=3"
LIST_7_URL = "https://syokugan-ohkoku.com/list-shohin.php?id=7"
NEW_IMAGE_URL = "https://syokugan-ohkoku.com/images/new.png"

NEW_LIST_HTML = """
<html><body><div class="item-wrap">
  <div><a href="item.php?code=1001"><img class="item-icon"
    src="/images/existing.jpg"></a></div>
  <span class="item-webtitle">Existing &amp; Original</span>
  <b>2026-10-01</b>
</div></body></html>
"""
PREORDER_LIST_HTML = """
<div class="item-wrap-small">
  <a href="/item.php?category=3&amp;code=3001">Details</a>
  <span class="item-webtitle">Preorder Figure</span><b>2026-11</b>
</div>
"""
RESTOCK_LIST_HTML = """
<div class="item-wrap">
  <a href="item.php?code=7001">Details</a>
  <span class="item-webtitle">Existing Restock</span><b>2026-10-10</b>
</div>
"""
NEW_PRODUCT_HTML = """
<div class="item-wrap-large">
  <a href="item.php?code=2002"><img class="item-icon" src="/images/new.png"></a>
  <span class="item-webtitle">Brand New &lt;Special&gt;</span><b>2026-12</b>
</div>
"""
NEW_RESTOCK_HTML = """
<div class="item-wrap">
  <a href="item.php?code=7002">Details</a>
  <span class="item-webtitle">Returning Figure</span><b>2026-10-19</b>
</div>
"""
MIGRATION_ALERT_HTML = """
<div class="item-wrap">
  <a href="item.php?code=2608231">Details</a>
  <span class="item-webtitle">Migration Alert One</span>
</div>
<div class="item-wrap">
  <a href="item.php?code=2608233">Details</a>
  <span class="item-webtitle">Migration Alert Two</span>
</div>
"""
HOMEPAGE_HTML = """
<h1 id="hc4">Not monitored</h1>
<div class="item-wrap"><a href="item.php?code=ignored">Details</a>
  <span class="item-webtitle">Ignored Arrival</span></div>
<h1 id="hc1">New</h1>
<div class="item-wrap extra-class">
  <div><a href="item.php?code=1001"><img class="item-icon"
    src="images/existing.jpg"></a></div>
  <span class="item-webtitle">Existing <em>&amp;</em> Original</span>
  <b>2026-10-01</b><b>Not the release date</b>
</div>
<div class="item-wrap"><a href="item.php?code=1001">Duplicate</a>
  <span class="item-webtitle">Duplicate Title</span></div>
<div class="item-wrap"><span class="item-webtitle">Missing Code</span></div>
<div class="item-wrap"><a href="item.php?code=no-title">Details</a></div>
<h1 id="hc7">Restock</h1>
<div class="item-wrap"><a href="item.php?code=1001">Details</a>
  <span class="item-webtitle">Same Code Restocked</span></div>
"""
BASELINE_STATE = {
    "initialized": True,
    "source_mode": "full_lists_v1",
    "known_codes": ["1001", "3001"],
    "active_restock_codes": ["7001"],
    "monitored_sections": ["hc1", "hc3", "hc7"],
    "last_snapshot": {
        "hc1:1001": {
            "code": "1001",
            "title": "Existing & Original",
            "release_date": "2026-10-01",
            "url": "https://syokugan-ohkoku.com/item.php?code=1001",
            "section": "hc1",
            "image_url": "https://syokugan-ohkoku.com/images/existing.jpg",
        },
        "hc3:3001": {
            "code": "3001",
            "title": "Preorder Figure",
            "release_date": "2026-11",
            "url": "https://syokugan-ohkoku.com/item.php?category=3&code=3001",
            "section": "hc3",
            "image_url": "",
        },
        "hc7:7001": {
            "code": "7001",
            "title": "Existing Restock",
            "release_date": "2026-10-10",
            "url": "https://syokugan-ohkoku.com/item.php?code=7001",
            "section": "hc7",
            "image_url": "",
        },
    },
}


class HTTPBoundary:
    """Serve real HTML and image bytes at the external urlopen boundary."""

    def __init__(self):
        self.pages = {
            LIST_1_URL: NEW_LIST_HTML,
            LIST_3_URL: PREORDER_LIST_HTML,
            LIST_7_URL: RESTOCK_LIST_HTML,
            NEW_IMAGE_URL: b"\x89PNG\r\n\x1a\nsynthetic-test-image",
        }
        self.requests = []

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.requests.append(url)
        if url not in self.pages:
            raise AssertionError(f"Unexpected external HTTP request: {url}")
        payload = self.pages[url]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return io.BytesIO(payload)


class SMTPBoundary:
    """Record messages delivered to a controlled SMTP server."""

    def __init__(self):
        self.connections = []
        self.credentials = []
        self.messages = []
        self.send_error = None
        self.login_error = None
        self.refused = {}

    def __call__(self, host, port, *, context, timeout):
        self.connections.append((host, port))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, username, auth_code):
        self.credentials.append((username, auth_code))
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, message):
        if self.send_error is not None:
            raise self.send_error
        self.messages.append(message)
        return self.refused


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            monitor, "Task 1 standalone monitor.py has not been implemented"
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.state_file = self.directory / "data" / "state.json"
        self.http = HTTPBoundary()
        self.smtp = SMTPBoundary()
        self.enterContext(
            patch.dict(os.environ, {**TEST_ENV, "STATE_FILE": str(self.state_file)})
        )
        self.enterContext(patch.object(monitor, "urlopen", self.http))
        self.enterContext(patch.object(smtplib, "SMTP_SSL", self.smtp))

    def seed_state(self, state=None):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(BASELINE_STATE if state is None else state, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.state_file.read_bytes()

    def read_state(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def run_monitor(self, argv=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = monitor.main(list(argv))
        return status, stdout.getvalue() + stderr.getvalue()

    def run_cli(self, argv=()):
        stdout, stderr = io.StringIO(), io.StringIO()
        previous_argv = sys.argv
        try:
            sys.argv = [str(ROOT / "monitor.py"), *argv]
            with (
                patch("urllib.request.urlopen", self.http),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                runpy.run_path(str(ROOT / "monitor.py"), run_name="__main__")
        finally:
            sys.argv = previous_argv
        return raised.exception.code, stdout.getvalue() + stderr.getvalue()

    def assert_state_unchanged(self, before):
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertFalse(self.state_file.with_suffix(".json.tmp").exists())

    def assert_no_sensitive_output(self, output):
        for sensitive in (
            *TEST_ENV.values(),
            "unexpected-debug+owner@example.invalid",
        ):
            if sensitive != TEST_ENV["MONITOR_SECTIONS"]:
                self.assertNotIn(sensitive, output)


class ProductParserTests(MonitorTestCase):
    def test_homepage_filters_sections_and_preserves_same_code_in_two_sections(self):
        parser = monitor.ProductParser(("hc1", "hc7"))
        parser.feed(HOMEPAGE_HTML)

        self.assertEqual(set(parser.products), {"hc1:1001", "hc7:1001"})
        product = parser.products["hc1:1001"]
        self.assertEqual(product.title, "Existing & Original")
        self.assertEqual(product.release_date, "2026-10-01")
        self.assertEqual(
            product.image_url, "https://syokugan-ohkoku.com/images/existing.jpg"
        )
        self.assertEqual(
            parser.products["hc7:1001"].title, "Same Code Restocked"
        )

    def test_full_list_uses_fixed_section_and_keeps_first_duplicate(self):
        parser = monitor.ProductParser(("hc3",), fixed_section="hc3")
        parser.feed(PREORDER_LIST_HTML + PREORDER_LIST_HTML)

        self.assertEqual(list(parser.products), ["hc3:3001"])
        self.assertEqual(
            vars(parser.products["hc3:3001"]),
            BASELINE_STATE["last_snapshot"]["hc3:3001"],
        )

    def test_fetch_parses_all_three_full_list_fixtures(self):
        products = monitor.fetch_products(("hc1", "hc3", "hc7"))

        self.assertEqual(
            {key: vars(product) for key, product in products.items()},
            BASELINE_STATE["last_snapshot"],
        )
        self.assertEqual(self.http.requests, [LIST_1_URL, LIST_3_URL, LIST_7_URL])

    def test_empty_list_cannot_replace_a_valid_baseline(self):
        before = self.seed_state()
        self.http.pages[LIST_3_URL] = "<html><body>Layout changed</body></html>"

        with self.assertRaises(RuntimeError):
            self.run_monitor()

        self.assert_state_unchanged(before)
        self.assertEqual(self.smtp.connections, [])


class StateTransitionTests(MonitorTestCase):
    def test_first_run_creates_baseline_without_notifying_historical_products(self):
        with patch.dict(
            os.environ, {"SMTP_USERNAME": "", "SMTP_AUTH_CODE": "", "EMAIL_TO": ""}
        ):
            status, output = self.run_monitor()

        self.assertEqual(status, 0)
        self.assertEqual(self.read_state(), BASELINE_STATE)
        self.assertEqual(self.smtp.connections, [])
        self.assert_no_sensitive_output(output)

    def test_unchanged_products_do_not_send_mail(self):
        self.seed_state()

        status, output = self.run_monitor()

        self.assertEqual(status, 0)
        self.assertEqual(self.read_state(), BASELINE_STATE)
        self.assertEqual(self.smtp.connections, [])
        self.assert_no_sensitive_output(output)

    def test_new_product_sends_real_multipart_mail_and_retains_historical_codes(self):
        state = copy.deepcopy(BASELINE_STATE)
        state["known_codes"].append("historic-42")
        self.seed_state(state)
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML

        status, output = self.run_monitor()

        self.assertEqual(status, 0)
        self.assertEqual(len(self.smtp.messages), 1)
        message = self.smtp.messages[0]
        self.assertIn("Brand New <Special>", message.get_body(("plain",)).get_content())
        self.assertNotIn("Existing Restock", message.get_body(("plain",)).get_content())
        html = message.get_body(("html",)).get_content()
        self.assertIn("Brand New &lt;Special&gt;", html)
        self.assertIn('src="cid:', html)
        images = [part for part in message.walk() if part.get_content_maintype() == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual(
            images[0].get_payload(decode=True), self.http.pages[NEW_IMAGE_URL]
        )
        self.assertEqual(message["From"], TEST_ENV["SMTP_USERNAME"])
        self.assertEqual(message["To"], TEST_ENV["EMAIL_TO"])
        self.assertEqual(
            self.read_state()["known_codes"], ["1001", "2002", "3001", "historic-42"]
        )
        self.assertEqual(self.read_state()["active_restock_codes"], ["7001"])
        self.assertEqual(
            self.read_state()["last_snapshot"]["hc1:2002"]["title"],
            "Brand New <Special>",
        )
        self.assert_no_sensitive_output(output)

    def test_restock_notifies_even_if_product_code_was_previously_known(self):
        state = copy.deepcopy(BASELINE_STATE)
        state["known_codes"].append("7002")
        self.seed_state(state)
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML

        status, _ = self.run_monitor()

        self.assertEqual(status, 0)
        self.assertEqual(len(self.smtp.messages), 1)
        body = self.smtp.messages[0].get_body(("plain",)).get_content()
        self.assertIn("Returning Figure", body)
        self.assertNotIn("Existing Restock", body)
        self.assertEqual(self.read_state()["active_restock_codes"], ["7001", "7002"])
        self.assertEqual(self.read_state()["known_codes"], ["1001", "3001", "7002"])

    def test_restock_disappearance_and_reappearance_notifies_once(self):
        self.seed_state()
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML
        self.run_monitor()
        self.run_monitor()
        self.assertEqual(len(self.smtp.messages), 1)

        self.http.pages[LIST_7_URL] = RESTOCK_LIST_HTML
        self.run_monitor()
        self.assertEqual(self.read_state()["active_restock_codes"], ["7001"])
        self.assertEqual(len(self.smtp.messages), 1)

        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML
        self.run_monitor()
        self.assertEqual(len(self.smtp.messages), 2)
        self.assertEqual(self.read_state()["known_codes"], ["1001", "3001"])

    def test_enabling_restock_section_does_not_notify_existing_restock(self):
        state = copy.deepcopy(BASELINE_STATE)
        state["monitored_sections"] = ["hc1", "hc3"]
        state["active_restock_codes"] = []
        del state["last_snapshot"]["hc7:7001"]
        self.seed_state(state)

        self.run_monitor()

        self.assertEqual(self.smtp.connections, [])
        self.assertEqual(self.read_state()["active_restock_codes"], ["7001"])
        self.assertEqual(
            self.read_state()["monitored_sections"], ["hc1", "hc3", "hc7"]
        )

    def test_missing_monitored_sections_are_inferred_from_snapshot(self):
        state = copy.deepcopy(BASELINE_STATE)
        del state["monitored_sections"]
        self.seed_state(state)
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML

        self.run_monitor()

        self.assertEqual(len(self.smtp.messages), 1)
        self.assertIn(
            "Returning Figure", self.smtp.messages[0].get_body(("plain",)).get_content()
        )

    def test_full_list_migration_baselines_non_alert_products_without_mail(self):
        state = copy.deepcopy(BASELINE_STATE)
        del state["source_mode"]
        state["known_codes"] = ["1001"]
        self.seed_state(state)
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML

        self.run_monitor()

        self.assertEqual(self.smtp.connections, [])
        self.assertEqual(self.read_state()["source_mode"], "full_lists_v1")
        self.assertEqual(self.read_state()["known_codes"], ["1001", "2002", "3001"])
        self.assertEqual(self.read_state()["active_restock_codes"], ["7001", "7002"])

    def test_full_list_migration_only_sends_unknown_allowlisted_alerts(self):
        state = copy.deepcopy(BASELINE_STATE)
        del state["source_mode"]
        state["known_codes"].append("2608231")
        self.seed_state(state)
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML + MIGRATION_ALERT_HTML
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML

        self.run_monitor()
        self.run_monitor()

        self.assertEqual(len(self.smtp.messages), 1)
        body = self.smtp.messages[0].get_body(("plain",)).get_content()
        self.assertIn("Migration Alert Two", body)
        self.assertNotIn("Migration Alert One", body)
        self.assertNotIn("Brand New", body)
        self.assertNotIn("Returning Figure", body)
        self.assertEqual(
            self.read_state()["known_codes"],
            ["1001", "2002", "2608231", "2608233", "3001"],
        )


class DryRunTests(MonitorTestCase):
    def test_dry_run_without_baseline_or_credentials_creates_nothing(self):
        with patch.dict(
            os.environ, {"SMTP_USERNAME": "", "SMTP_AUTH_CODE": "", "EMAIL_TO": ""}
        ):
            status, output = self.run_monitor(["--dry-run"])

        self.assertEqual(status, 0)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.smtp.connections, [])
        self.assertEqual(self.http.requests, [LIST_1_URL, LIST_3_URL, LIST_7_URL])
        self.assertTrue(output.strip())
        self.assert_no_sensitive_output(output)

    def test_dry_run_with_new_product_does_not_write_state_or_download_images(self):
        before = self.seed_state()
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML

        status, output = self.run_monitor(["--dry-run"])

        self.assertEqual(status, 0)
        self.assert_state_unchanged(before)
        self.assertEqual(self.smtp.connections, [])
        self.assertEqual(self.http.requests, [LIST_1_URL, LIST_3_URL, LIST_7_URL])
        self.assertTrue(output.strip())
        self.assert_no_sensitive_output(output)

    def test_dry_run_without_changes_does_not_rewrite_state(self):
        self.seed_state()
        before = self.state_file.read_bytes() + b"\n"
        self.state_file.write_bytes(before)

        status, _ = self.run_monitor(["--dry-run"])

        self.assertEqual(status, 0)
        self.assert_state_unchanged(before)
        self.assertEqual(self.smtp.connections, [])

    def test_dry_run_combined_with_test_notification_never_sends_or_writes(self):
        status, output = self.run_monitor(["--dry-run", "--test-notification"])

        self.assertEqual(status, 0)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.smtp.connections, [])
        self.assert_no_sensitive_output(output)

    def test_parse_args_accepts_explicit_argv_without_reading_process_arguments(self):
        args = monitor.parse_args(["--dry-run"])

        self.assertTrue(args.dry_run)
        self.assertFalse(args.test_notification)


class DeliverySafetyTests(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML

    def test_smtp_send_exception_does_not_advance_new_or_restock_state(self):
        before = self.seed_state()
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML
        self.smtp.send_error = smtplib.SMTPDataError(554, b"Synthetic delivery failure")

        with self.assertRaises(smtplib.SMTPException):
            self.run_monitor()

        self.assert_state_unchanged(before)

    def test_authentication_failure_is_safe_and_preserves_baseline(self):
        before = self.seed_state()
        self.smtp.login_error = smtplib.SMTPAuthenticationError(
            535, f"Synthetic failure {SYNTHETIC_AUTH_CODE}".encode()
        )

        with self.assertRaises(RuntimeError) as raised:
            self.run_monitor()

        self.assert_state_unchanged(before)
        self.assert_no_sensitive_output(str(raised.exception))

    def test_missing_mail_configuration_cannot_advance_baseline(self):
        before = self.seed_state()

        with (
            patch.dict(os.environ, {"SMTP_AUTH_CODE": ""}),
            self.assertRaises(RuntimeError),
        ):
            self.run_monitor()

        self.assert_state_unchanged(before)
        self.assertEqual(self.smtp.connections, [])

    def test_refused_recipient_result_raises_safe_error_without_advancing_state(self):
        before = self.seed_state()
        self.http.pages[LIST_7_URL] += NEW_RESTOCK_HTML
        self.smtp.refused = {
            "unexpected-debug+owner@example.invalid": (
                550,
                f"Synthetic refusal {SYNTHETIC_AUTH_CODE}".encode(),
            )
        }

        with self.assertRaises(RuntimeError) as raised:
            self.run_monitor()

        self.assert_state_unchanged(before)
        self.assert_no_sensitive_output(str(raised.exception))
        self.assertTrue(str(raised.exception))

    def test_image_failure_falls_back_to_text_and_redacts_warning(self):
        self.seed_state()
        self.http.pages[NEW_IMAGE_URL] = RuntimeError(
            f"Image failed for unexpected-debug+owner@example.invalid {SYNTHETIC_AUTH_CODE}"
        )

        status, output = self.run_monitor()

        self.assertEqual(status, 0)
        self.assertEqual(len(self.smtp.messages), 1)
        message = self.smtp.messages[0]
        self.assertIn("Brand New <Special>", message.get_body(("plain",)).get_content())
        self.assertIn("Brand New &lt;Special&gt;", message.get_body(("html",)).get_content())
        self.assertNotIn("cid:", message.get_body(("html",)).get_content())
        self.assertFalse(any(part.get_content_maintype() == "image" for part in message.walk()))
        self.assertEqual(self.read_state()["known_codes"], ["1001", "2002", "3001"])
        self.assert_no_sensitive_output(output)

    def test_test_notification_never_modifies_existing_state(self):
        before = self.seed_state()
        self.http.pages["https://syokugan-ohkoku.com/images/existing.jpg"] = (
            b"synthetic-test-jpeg"
        )

        status, output = self.run_monitor(["--test-notification"])

        self.assertEqual(status, 0)
        self.assert_state_unchanged(before)
        self.assertEqual(len(self.smtp.messages), 1)
        self.assertIn(
            "Existing & Original", self.smtp.messages[0].get_body(("plain",)).get_content()
        )
        self.assert_no_sensitive_output(output)


class RedactionTests(MonitorTestCase):
    def test_redaction_masks_configured_values_and_unconfigured_email_addresses(self):
        text = (
            f"user: {TEST_ENV['SMTP_USERNAME']} secret: {SYNTHETIC_AUTH_CODE} "
            f"to: {TEST_ENV['EMAIL_TO']} "
            "debug: unexpected-debug+owner@example.invalid"
        )

        redacted = monitor.redact_sensitive(text)

        self.assert_no_sensitive_output(redacted)
        self.assertIn("user: ", redacted)
        self.assertIn("debug: ", redacted)

    def test_non_email_configuration_values_are_also_redacted(self):
        with patch.dict(
            os.environ,
            {
                "SMTP_USERNAME": "synthetic-non-email-smtp-username",
                "EMAIL_TO": "synthetic-non-email-recipient",
            },
        ):
            redacted = monitor.redact_sensitive(
                "synthetic-non-email-smtp-username "
                "synthetic-non-email-recipient "
                + SYNTHETIC_AUTH_CODE
            )

        self.assertNotIn("synthetic-non-email-smtp-username", redacted)
        self.assertNotIn("synthetic-non-email-recipient", redacted)
        self.assertNotIn(SYNTHETIC_AUTH_CODE, redacted)

    def test_empty_configuration_does_not_replace_every_character(self):
        with patch.dict(
            os.environ, {"SMTP_USERNAME": "", "SMTP_AUTH_CODE": "", "EMAIL_TO": ""}
        ):
            self.assertEqual(monitor.redact_sensitive("Ordinary error 550"), "Ordinary error 550")
            self.assertNotIn(
                "Debug.Owner+tag@example.invalid",
                monitor.redact_sensitive("Rejected <Debug.Owner+tag@example.invalid>"),
            )

    def test_top_level_network_error_is_sanitized_without_traceback(self):
        self.http.pages[LIST_1_URL] = RuntimeError(
            f"Failure {TEST_ENV['SMTP_USERNAME']} {TEST_ENV['EMAIL_TO']} "
            f"{SYNTHETIC_AUTH_CODE} unexpected-debug+owner@example.invalid"
        )

        status, output = self.run_cli(["--dry-run"])

        self.assertEqual(status, 1)
        self.assert_no_sensitive_output(output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(output.strip())
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.smtp.connections, [])

    def test_top_level_smtp_error_is_sanitized_and_preserves_state(self):
        before = self.seed_state()
        self.http.pages[LIST_1_URL] += NEW_PRODUCT_HTML
        self.smtp.send_error = smtplib.SMTPDataError(
            554,
            (
                f"{SYNTHETIC_AUTH_CODE} {TEST_ENV['SMTP_USERNAME']} "
                f"{TEST_ENV['EMAIL_TO']} unexpected-debug+owner@example.invalid"
            ).encode(),
        )

        status, output = self.run_cli()

        self.assertEqual(status, 1)
        self.assert_state_unchanged(before)
        self.assert_no_sensitive_output(output)
        self.assertNotIn("Traceback", output)

    def test_top_level_argument_errors_do_not_echo_sensitive_values(self):
        status, output = self.run_cli(
            [
                "--unknown-option",
                SYNTHETIC_AUTH_CODE,
                "unexpected-debug+owner@example.invalid",
            ]
        )

        self.assertEqual(status, 2)
        self.assert_no_sensitive_output(output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.smtp.connections, [])
        self.assertEqual(self.http.requests, [])


if __name__ == "__main__":
    unittest.main()
