import unittest

from pydantic import ValidationError

from notamify_sdk.models import (
    ActiveNotamsQuery,
    BriefingResponse,
    HistoricalNotamsQuery,
    Listener,
    ListenerRequest,
    NearbyNotamsQuery,
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

    def test_notam_list_result_accepts_affected_element_semantics(self):
        payload = {
            "notams": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "notam_number": "A1234/26",
                    "location": "KJFK",
                    "starts_at": "2026-02-25T10:00:00Z",
                    "ends_at": "2026-02-26T10:00:00Z",
                    "issued_at": "2026-02-25T09:00:00Z",
                    "is_estimated": False,
                    "is_permanent": False,
                    "message": "RWY 11 restrictions",
                    "interpretation": {
                        "description": "Runway operations restricted.",
                        "excerpt": "Runway 11 restrictions in effect.",
                        "category": "AERODROME",
                        "subcategory": "RUNWAY_OPERATIONS",
                        "affected_elements": [
                            {
                                "type": "RUNWAY",
                                "identifier": "11",
                                "effect": "RESTRICTED",
                                "details": "Runway 11 restricted to lighter traffic.",
                                "subtype": "DEPARTURE_RUNWAY",
                                "semantics": {
                                    "scope": [],
                                    "conditions": [
                                        {
                                            "dimension": "operation_phase",
                                            "operator": "IN",
                                            "value": ["TAKEOFF", "LANDING"],
                                            "unit": None,
                                            "details": None,
                                        },
                                        {
                                            "dimension": "weight",
                                            "operator": "LTE",
                                            "value": {
                                                "kind": "MEASUREMENT",
                                                "raw_string": "5700KG",
                                                "value": 5700,
                                                "unit": "KG",
                                            },
                                            "unit": "KG",
                                            "details": None,
                                        },
                                        {
                                            "dimension": "procedure_capability",
                                            "operator": "EQ",
                                            "value": {
                                                "scheme": "ILS_CATEGORY",
                                                "category": "CAT_I",
                                                "level": "LVL_1",
                                            },
                                            "unit": None,
                                            "details": None,
                                        },
                                    ],
                                    "exceptions": [
                                        {
                                            "dimension": "aircraft_type",
                                            "operator": "IN",
                                            "value": ["HELICOPTER"],
                                            "unit": None,
                                            "details": None,
                                        }
                                    ],
                                    "changes": [
                                        {
                                            "subject": "PROCEDURE_CAPABILITY",
                                            "from": [
                                                {
                                                    "scheme": "ILS_CATEGORY",
                                                    "category": "CAT_II",
                                                    "level": "LVL_2",
                                                }
                                            ],
                                            "to": [
                                                {
                                                    "scheme": "ILS_CATEGORY",
                                                    "category": "CAT_I",
                                                    "level": "LVL_1",
                                                }
                                            ],
                                            "details": "Downgraded due to maintenance.",
                                        }
                                    ],
                                    "references": [
                                        {
                                            "relation": "DEPENDS_ON",
                                            "type": "NOTAM",
                                            "identifier": "A1234/26",
                                        }
                                    ],
                                },
                            }
                        ],
                        "schedules": [],
                    },
                }
            ],
            "total_count": 1,
            "page": 1,
            "per_page": 30,
        }

        result = NotamListResult.model_validate(payload)
        affected = result.notams[0].interpretation.affected_elements[0]

        self.assertEqual(affected.subtype, "DEPARTURE_RUNWAY")
        self.assertEqual(affected.semantics.conditions[0].value, ["TAKEOFF", "LANDING"])
        self.assertEqual(affected.semantics.conditions[1].value.value, 5700)
        self.assertEqual(affected.semantics.conditions[2].value.scheme, "ILS_CATEGORY")
        self.assertEqual(affected.semantics.changes[0].from_[0].category, "CAT_II")
        self.assertEqual(affected.semantics.references[0].relation, "DEPENDS_ON")

    def test_required_fields_enforced(self):
        with self.assertRaises(ValidationError):
            NotamListResult.model_validate({"notams": []})

    def test_notam_query_per_page_is_limited_to_30(self):
        with self.assertRaises(ValidationError):
            ActiveNotamsQuery.model_validate({"location": ["KJFK"], "per_page": 31})

        with self.assertRaises(ValidationError):
            NearbyNotamsQuery.model_validate({"lat": 40.0, "lon": -73.0, "per_page": 31})

        with self.assertRaises(ValidationError):
            HistoricalNotamsQuery.model_validate({"valid_at": "2026-02-20", "per_page": 31})

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

    def test_listener_request_accepts_nested_lifecycle_shape(self):
        model = ListenerRequest.model_validate(
            {
                "webhook_url": "https://example.com/hook",
                "emails": ["ops@example.com"],
                "filters": {},
                "lifecycle": {
                    "enabled": True,
                    "types": ["cancelled", "REPLACED"],
                },
            }
        )
        self.assertEqual(model.emails, ["ops@example.com"])
        self.assertTrue(model.lifecycle.enabled)
        self.assertEqual([item.value for item in model.lifecycle.types], ["CANCELLED", "REPLACED"])

    def test_listener_request_rejects_scalar_emails(self):
        with self.assertRaises(ValidationError):
            ListenerRequest.model_validate({"webhook_url": "https://example.com/hook", "emails": "ops@example.com"})

    def test_listener_model_maps_legacy_lifecycle_enabled_to_nested_shape(self):
        model = Listener.model_validate(
            {
                "id": "l1",
                "emails": ["ops@example.com"],
                "filters": {},
                "metadata": {"notams_shipped": 0},
                "active": True,
                "mode": "prod",
                "lifecycle_enabled": True,
                "created_at": "2026-03-01T10:00:00Z",
                "updated_at": "2026-03-01T10:01:00Z",
            }
        )
        self.assertEqual(model.emails, ["ops@example.com"])
        self.assertTrue(model.lifecycle.enabled)
        self.assertTrue(model.lifecycle_enabled)

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
