import unittest
from app.processor import EVENT_TYPE_MAP, idempotency_key

class ProcessorTests(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(EVENT_TYPE_MAP["Reception"], "mta_accepted")
        self.assertEqual(EVENT_TYPE_MAP["Delivery"], "remote_accepted")
        self.assertEqual(EVENT_TYPE_MAP["TransientFailure"], "deferred")
        self.assertEqual(EVENT_TYPE_MAP["Bounce"], "failed")
        self.assertEqual(EVENT_TYPE_MAP["Expiration"], "failed")

    def test_key_stable_for_exact_replay(self):
        record = {
            "type": "Delivery", "id": "same-mta-id",
            "created_time": "2026-10-22T12:22:57.669905200Z",
            "num_attempts": 0, "recipient": "redacted@example.test",
            "response": {"code": 250, "content": "OK"},
        }
        self.assertEqual(idempotency_key(record), idempotency_key(record.copy()))

    def test_same_mta_id_different_event_types_differ(self):
        common = {
            "id": "shared-id", "created_time": "2026-10-22T12:22:57Z",
            "num_attempts": 0, "recipient": "redacted@example.test",
            "response": {"code": 250, "content": ""},
        }
        self.assertNotEqual(
            idempotency_key(dict(common, type="Reception")),
            idempotency_key(dict(common, type="Delivery")),
        )

if __name__ == "__main__":
    unittest.main()
    