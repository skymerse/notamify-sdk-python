import unittest

from pydantic import ValidationError

from notamify_sdk.models import (
    BriefingResponse,
    NotamListResult,
    NotamPriority,
    PrioritizedNotamDTO,
    WatcherWebhookEvent,
)


class ModelTests(unittest.TestCase):
    def test_notam_list_result_validation(self):
        payload = {
            "notams": [
                {
                    "id": "n1",
                    "notam_number": "A1234/26",
                    "location": "KJFK",
                    "starts_at": "2026-02-25T10:00:00Z",
                    "ends_at": "2026-02-26T10:00:00Z",
                    "issued_at": "2026-02-25T09:00:00Z",
                    "is_estimated": False,
                    "is_permanent": False,
                    "message": "RWY closed",
                }
            ],
            "total_count": 1,
            "page": 1,
            "per_page": 30,
        }
        result = NotamListResult.model_validate(payload)
        self.assertEqual(result.notams[0].id, "n1")

    def test_required_fields_enforced(self):
        with self.assertRaises(ValidationError):
            NotamListResult.model_validate({"notams": []})

    def test_priority_enum(self):
        model = PrioritizedNotamDTO.model_validate(
            {
                "notam": {
                    "id": "n1",
                    "notam_number": "A1234/26",
                    "notam_type": "N",
                    "location": "KJFK",
                    "starts_at": "2026-02-25T10:00:00Z",
                    "ends_at": "2026-02-26T10:00:00Z",
                    "issued_at": "2026-02-25T09:00:00Z",
                    "is_estimated": False,
                    "is_permanent": False,
                    "message": "RWY closed",
                },
                "priority": "HIGH",
                "explanation": "Important",
            }
        )
        self.assertEqual(model.priority, NotamPriority.high)

    def test_briefing_response_supports_critical_restrictions(self):
        model = BriefingResponse.model_validate(
            {
                "text": "Briefing text",
                "critical_operational_restrictions": [
                    {
                        "location_code": "KJFK",
                        "location_role": "origin",
                        "items": ["Runway closed"],
                    }
                ],
            }
        )
        self.assertEqual(model.critical_operational_restrictions[0].location_role.value, "origin")

    def test_watcher_lifecycle_payload_validation(self):
        model = WatcherWebhookEvent.model_validate(
            {
                "listener_id": "listener-1",
                "kind": "lifecycle",
                "event_id": "event-1",
                "notam": {
                    "id": "n1",
                    "notam_number": "A1234/26",
                    "notam_type": "R",
                    "location": "KJFK",
                    "starts_at": "2026-02-25T10:00:00Z",
                    "ends_at": "2026-02-26T10:00:00Z",
                    "issued_at": "2026-02-25T09:00:00Z",
                    "is_estimated": False,
                    "is_permanent": False,
                    "message": "RWY closed",
                },
                "change": {
                    "changed_notam_id": "old-n1",
                    "notam_type": "R",
                },
                "sent_at": "2026-03-06T12:00:00Z",
            }
        )
        self.assertEqual(model.kind.value, "lifecycle")
        self.assertEqual(model.change.changed_notam_id, "old-n1")

    def test_watcher_lifecycle_requires_change(self):
        with self.assertRaises(ValidationError):
            WatcherWebhookEvent.model_validate(
                {
                    "listener_id": "listener-1",
                    "kind": "lifecycle",
                    "event_id": "event-1",
                    "notam": {
                        "id": "n1",
                        "notam_number": "A1234/26",
                        "notam_type": "R",
                        "location": "KJFK",
                        "starts_at": "2026-02-25T10:00:00Z",
                        "ends_at": "2026-02-26T10:00:00Z",
                        "issued_at": "2026-02-25T09:00:00Z",
                        "is_estimated": False,
                        "is_permanent": False,
                        "message": "RWY closed",
                    },
                    "sent_at": "2026-03-06T12:00:00Z",
                }
            )


if __name__ == "__main__":
    unittest.main()
