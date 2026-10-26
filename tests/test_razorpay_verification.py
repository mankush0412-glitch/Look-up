"""Run with: python -m unittest discover -s tests -v

Pure unit tests: no MongoDB, Telegram network, Razorpay account or webhook needed.
"""
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Minimal Telegram stubs for dependency-light testing if python-telegram-bot
# has not been installed yet. In production, the real package is used.
try:
    import telegram
except ImportError:
    fake = types.ModuleType("telegram")
    fake.InlineKeyboardButton = type("InlineKeyboardButton", (), {"__init__": lambda self, *a, **kw: None})
    fake.InlineKeyboardMarkup = type("InlineKeyboardMarkup", (), {"__init__": lambda self, *a, **kw: None})
    sys.modules["telegram"] = fake

import razorpay_payments as rzp


class FakeDB:
    def __init__(self, order):
        self.order = dict(order)
        self.verifications = []
        self.statuses = []
        self.failed = False
        self.allow_record = True

    def get_credit_order(self, order_id):
        return dict(self.order)

    def record_verified_razorpay_payment(self, order_id, **kwargs):
        self.verifications.append(kwargs)
        return self.allow_record

    def record_verified_razorpay_link_payment(self, order_id, **kwargs):
        self.verifications.append(kwargs)
        return self.allow_record

    def mark_order_payment_failed(self, order_id, reason):
        self.failed = True
        self.statuses.append("failed")

    def update_razorpay_payment_status(self, order_id, status, **kwargs):
        self.statuses.append(status)
        return True


ORDER = {
    "order_id": "AABBCCDDEE",
    "user_id": 123,
    "payment_method": "razorpay",
    "status": "pending",
    "order_type": "credits",
    "price_amount": 50,
    "razorpay_qr_id": "qr_abc123",
    "razorpay_qr_close_by": 2_000_000_000,
}

QR = {
    "id": "qr_abc123",
    "type": "upi_qr",
    "usage": "single_use",
    "fixed_amount": True,
    "payment_amount": 5000,
    "notes": {"order_id": "AABBCCDDEE", "telegram_user_id": "123"},
}

PAYMENT = {
    "id": "pay_123",
    "entity": "payment",
    "status": "captured",
    "amount": 5000,
    "currency": "INR",
    "method": "upi",
    "captured": True,
    "amount_refunded": 0,
    "acquirer_data": {"rrn": "42"},
}


class RazorpayVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = FakeDB(ORDER)
        self.mocks = [
            patch.object(rzp, "fetch_qr_details", new_callable=AsyncMock, return_value=dict(QR)),
            patch.object(rzp, "fetch_qr_payments", new_callable=AsyncMock, return_value={"items": [dict(PAYMENT)]}),
            patch.object(rzp, "fetch_payment_by_id", new_callable=AsyncMock, return_value=dict(PAYMENT)),
            patch.object(rzp, "activate_paid_order", new_callable=AsyncMock, return_value=True),
        ]
        self.patched = [p.start() for p in self.mocks]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.mocks)])

    async def check(self):
        return await rzp.check_and_process_order(None, None, self.db, self.db.order)

    async def test_captured_exact_amount_is_verified_and_activated(self):
        self.assertEqual(await self.check(), ("paid", True))
        self.assertEqual(self.db.verifications[0]["payment_id"], "pay_123")
        self.assertEqual(self.db.verifications[0]["rrn"], "42")
        self.patched[3].assert_awaited_once()

    async def test_wrong_qr_amount_is_blocked(self):
        self.patched[0].return_value = {**QR, "payment_amount": 100}
        self.assertEqual(await self.check(), ("mismatch", False))
        self.assertEqual(self.db.verifications, [])

    async def test_wrong_qr_notes_are_blocked(self):
        self.patched[0].return_value = {**QR, "notes": {"order_id": "another"}}
        self.assertEqual(await self.check(), ("mismatch", False))

    async def test_non_captured_or_currency_mismatch_is_blocked(self):
        for overridden in [{"status": "authorized"}, {"currency": "USD"}, {"amount": 4999}, {"amount_refunded": 5000}]:
            with self.subTest(overridden=overridden):
                self.patched[1].return_value = {"items": [{**PAYMENT, **overridden}]}
                self.assertEqual(await self.check(), ("pending", False))
                self.assertEqual(self.db.verifications, [])
        self.patched[2].assert_not_awaited()

    async def test_payment_detail_confirmation_must_match(self):
        self.patched[2].return_value = {**PAYMENT, "status": "refunded"}
        self.assertEqual(await self.check(), ("pending", False))
        self.assertEqual(self.db.verifications, [])

    async def test_duplicate_payment_cannot_be_activated(self):
        self.db.allow_record = False
        self.assertEqual(await self.check(), ("duplicate", False))
        self.patched[3].assert_not_awaited()

    async def test_gateway_error_never_activates(self):
        self.patched[0].side_effect = RuntimeError("network down")
        self.assertEqual(await self.check(), ("error", False))
        self.patched[3].assert_not_awaited()

    async def test_expired_qr_keeps_settlement_grace(self):
        self.db.order["razorpay_qr_close_by"] = 1_000_000
        self.patched[1].return_value = {"items": []}
        with patch.object(rzp.time, "time", return_value=1_000_100):
            self.assertEqual(await self.check(), ("settlement_pending", False))
        self.assertFalse(self.db.failed)
        with patch.object(rzp.time, "time", return_value=1_010_000):
            self.assertEqual(await self.check(), ("expired", False))
        self.assertTrue(self.db.failed)

    async def test_legacy_link_requires_verified_payment(self):
        self.db.order["razorpay_qr_id"] = ""
        self.db.order["razorpay_payment_link_id"] = "plink_AAA"
        payload = {"id": "plink_AAA", "status": "paid", "amount": 5000, "amount_paid": 5000,
                   "currency": "INR", "payments": [{"payment_id": "pay_123", "status": "captured", "plink_id": "plink_AAA"}]}
        with patch.object(rzp, "fetch_payment_link", new_callable=AsyncMock, return_value=payload):
            self.assertEqual(await self.check(), ("paid", True))
        self.assertEqual(self.db.verifications[0]["link_id"], "plink_AAA")

    async def test_legacy_link_status_alone_is_not_enough(self):
        self.db.order["razorpay_qr_id"] = ""
        self.db.order["razorpay_payment_link_id"] = "plink_AAA"
        payload = {"id": "plink_AAA", "status": "paid", "amount": 5000, "amount_paid": 5000, "currency": "INR", "payments": []}
        with patch.object(rzp, "fetch_payment_link", new_callable=AsyncMock, return_value=payload):
            self.assertEqual(await self.check(), ("pending", False))
        self.patched[3].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

class OutboundGatewayAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_requests_use_basic_auth_only(self):
        import base64
        import httpx

        class CredentialsOnlyDB:
            @staticmethod
            def get_razorpay_config():
                return {"key_id": "rzp_test_fake", "key_secret": "test-secret", "business_name": "Test"}

        observed = []
        def handler(request):
            observed.append(request.url.path)
            self.assertEqual(request.url.host, "api.razorpay.com")
            token = request.headers.get("Authorization", "").split(" ", 1)
            self.assertEqual(token[0], "Basic")
            self.assertEqual(base64.b64decode(token[1]).decode(), "rzp_test_fake:test-secret")
            if request.url.path.endswith("/payments/pay_123"):
                return httpx.Response(200, json=dict(PAYMENT))
            return httpx.Response(200, json=dict(QR))

        original = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        with patch.object(rzp.httpx, "AsyncClient", side_effect=lambda **kw: original(transport=transport, **kw)):
            qr = await rzp.fetch_qr_details(CredentialsOnlyDB(), "qr_abc123")
            payment = await rzp.fetch_payment_by_id(CredentialsOnlyDB(), "pay_123")
        self.assertEqual(qr["id"], "qr_abc123")
        self.assertEqual(payment["id"], "pay_123")
        self.assertEqual(observed, ["/v1/payments/qr_codes/qr_abc123", "/v1/payments/pay_123"])


if __name__ == "__main__":
    unittest.main()


class PaymentLinkVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.order = {**ORDER, "razorpay_qr_id": "", "razorpay_payment_link_id": "plink_xyz",
                      "razorpay_link_reference_enforced": True, "razorpay_link_expire_by": 2_000_000_000}
        self.db = FakeDB(self.order)
        self.link = {
            "id": "plink_xyz", "reference_id": "AABBCCDDEE", "status": "paid",
            "amount": 5000, "amount_paid": 5000, "amount_due": 0,
            "currency": "INR", "accept_partial": False, "expire_by": 2_000_000_000,
            "notes": {"order_id": "AABBCCDDEE", "telegram_user_id": "123"},
            "payments": [{"plink_id": "plink_xyz", "payment_id": "pay_123",
                          "amount": 5000, "status": "captured"}],
        }
        self.mocks = [
            patch.object(rzp, "fetch_payment_link", new_callable=AsyncMock, return_value=self.link),
            patch.object(rzp, "fetch_payment_by_id", new_callable=AsyncMock, return_value=dict(PAYMENT)),
            patch.object(rzp, "activate_paid_order", new_callable=AsyncMock, return_value=True),
        ]
        self.patched = [p.start() for p in self.mocks]
        self.addCleanup(lambda: [p.stop() for p in reversed(self.mocks)])

    async def check(self):
        return await rzp.check_and_process_order(None, None, self.db, self.db.order)

    async def test_exact_captured_payment_activates(self):
        self.assertEqual(await self.check(), ("paid", True))
        self.assertEqual(self.db.verifications[0]["link_id"], "plink_xyz")
        self.patched[2].assert_awaited_once()

    async def test_reference_or_notes_mismatch_rejected(self):
        for change in [{"reference_id": "other"},
                       {"notes": {"order_id": "other", "telegram_user_id": "123"}},
                       {"notes": {"order_id": "AABBCCDDEE", "telegram_user_id": "99"}},
                       {"accept_partial": True}]:
            with self.subTest(change=change):
                self.patched[0].return_value = {**self.link, **change}
                self.assertEqual(await self.check(), ("mismatch", False))
        self.assertEqual(self.db.verifications, [])

    async def test_wrong_link_amount_paid_or_currency_rejected(self):
        for change in [{"amount": 4999}, {"amount_paid": 100},
                       {"amount_due": 100}, {"currency": "USD"}, {"id": "plink_wrong"}]:
            with self.subTest(change=change):
                self.patched[0].return_value = {**self.link, **change}
                self.assertEqual(await self.check(), ("mismatch", False))
        self.assertEqual(self.db.verifications, [])

    async def test_paid_status_without_captured_payment_cannot_activate(self):
        self.patched[0].return_value = {**self.link, "payments": []}
        self.assertEqual(await self.check(), ("settlement_pending", False))
        self.patched[2].assert_not_awaited()

    async def test_listed_payment_and_gateway_details_must_match(self):
        self.patched[1].return_value = {**PAYMENT, "status": "authorized"}
        self.assertEqual(await self.check(), ("settlement_pending", False))
        self.patched[2].assert_not_awaited()

    async def test_refunded_wrong_amount_duplicate_are_not_activated(self):
        self.patched[1].return_value = {**PAYMENT, "amount_refunded": 5000}
        self.assertEqual(await self.check(), ("settlement_pending", False))
        self.patched[1].return_value = dict(PAYMENT)
        self.db.allow_record = False
        self.assertEqual(await self.check(), ("duplicate", False))
        self.patched[2].assert_not_awaited()

    async def test_expired_link_grace(self):
        self.patched[0].return_value = {**self.link, "status": "expired", "amount_paid": 0, "payments": []}
        self.db.order["razorpay_link_expire_by"] = 1_000_000
        with patch.object(rzp.time, "time", return_value=1_000_100):
            self.assertEqual(await self.check(), ("settlement_pending", False))
        self.assertFalse(self.db.failed)
        with patch.object(rzp.time, "time", return_value=1_010_000):
            self.assertEqual(await self.check(), ("expired", False))
        self.assertTrue(self.db.failed)

    async def test_gateway_error_never_activates(self):
        self.patched[0].side_effect = RuntimeError("timeout")
        self.assertEqual(await self.check(), ("error", False))
        self.patched[2].assert_not_awaited()


class PaymentLinkCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_uses_basic_auth_no_webhook_and_exact_amount(self):
        import base64
        import httpx
        from types import SimpleNamespace

        class CredentialDB:
            @staticmethod
            def get_razorpay_config():
                return {"key_id": "rzp_test_id", "key_secret": "fake-secret", "business_name": "Test Shop"}

        captured = []
        def handler(request):
            import json
            captured.append((request, json.loads(request.content)))
            return httpx.Response(200, json={"id": "plink_xyz", "short_url": "https://rzp.io/i/abc",
                                             "status": "created", "expire_by": 2_000_000_000})

        actual_client = httpx.AsyncClient
        with patch.object(rzp.httpx, "AsyncClient", side_effect=lambda **kw: actual_client(transport=httpx.MockTransport(handler), **kw)):
            result = await rzp.create_payment_link(
                CredentialDB(), "AABBCCDDEE", {"price": 50, "label": "50 Credits", "type": "credits"},
                SimpleNamespace(id=123))
        req, body = captured[0]
        self.assertEqual(req.url.path, "/v1/payment_links")
        self.assertEqual(req.method, "POST")
        self.assertEqual(base64.b64decode(req.headers["Authorization"].split(" ")[1]).decode(), "rzp_test_id:fake-secret")
        self.assertEqual(body["amount"], 5000)
        self.assertEqual(body["currency"], "INR")
        self.assertIs(body["accept_partial"], False)
        self.assertEqual(body["reference_id"], "AABBCCDDEE")
        self.assertEqual(body["notes"]["telegram_user_id"], "123")
        self.assertNotIn("callback_url", body)
        self.assertNotIn("callback_method", body)
        self.assertNotIn("webhook", body)
        self.assertEqual(result["id"], "plink_xyz")

    async def test_fetch_payment_link_basic_auth(self):
        import httpx
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"id": "plink_xyz", "status": "created"})
        class CredentialDB:
            @staticmethod
            def get_razorpay_config():
                return {"key_id": "rzp_test_id", "key_secret": "fake-secret"}
        actual_client = httpx.AsyncClient
        with patch.object(rzp.httpx, "AsyncClient", side_effect=lambda **kw: actual_client(transport=httpx.MockTransport(handler), **kw)):
            data = await rzp.fetch_payment_link(CredentialDB(), "plink_xyz")
        self.assertEqual(seen[0].url.path, "/v1/payment_links/plink_xyz")
        self.assertTrue(seen[0].headers.get("Authorization", "").startswith("Basic "))
        self.assertEqual(data["id"], "plink_xyz")

    def test_only_razorpay_https_urls_are_accepted(self):
        for allowed in ["https://rzp.io/i/s123", "https://pages.razorpay.com/abcd", "https://razorpay.me/@shop"]:
            self.assertTrue(rzp.valid_payment_link_url(allowed), allowed)
        for blocked in ["http://rzp.io/i/s123", "https://rzp.io.evil.com/a", "javascript:alert(1)",
                        "https://user:pw@rzp.io/i/abc", "https://example.com/pay", "https://rzp.io:444/path"]:
            self.assertFalse(rzp.valid_payment_link_url(blocked), blocked)
