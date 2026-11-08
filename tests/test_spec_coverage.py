"""The SDK must cover everything the public API spec exposes.

This checks one direction only: every documented operation, query parameter
and response field is reachable through the SDK. The SDK may model fields the
document does not list.

tests/fixtures/openapi.json is a copy of https://api.notamify.com/api/v2/openapi.json.
Refresh it with `make spec`; set NOTAMIFY_SPEC_LIVE=1 to check the live document.
"""

import json
import os
import unittest
from pathlib import Path
from urllib import request

from pydantic import BaseModel

from notamify_sdk import models

FIXTURE = Path(__file__).parent / "fixtures" / "openapi.json"
LIVE_URL = "https://api.notamify.com/api/v2/openapi.json"

# OpenAPI operation -> (SDK client method, SDK query or request model)
OPERATIONS = {
    ("get", "/notams"): ("get_active_notams", models.ActiveNotamsQuery),
    ("get", "/notams/raw"): ("get_raw_notams", models.ActiveNotamsQuery),
    ("get", "/notams/nearby"): ("get_nearby_notams", models.NearbyNotamsQuery),
    ("get", "/notams/archive"): ("get_historical_notams", models.HistoricalNotamsQuery),
    ("get", "/notams/recently-expired"): ("get_recently_expired_notams", models.RecentlyExpiredNotamsQuery),
    ("get", "/notams/briefing/simple"): ("generate_airport_briefing", models.SimpleAirportBriefingQuery),
    ("post", "/notams/briefing"): ("create_async_briefing", models.GenerateFlightBriefingRequest),
    ("get", "/notams/briefing/{uuid}"): ("get_async_briefing_status", None),
    ("post", "/notams/prioritisation"): ("prioritize_notam", models.NotamPrioritisationRequest),
}

# Spec schema -> SDK model, where the names differ.
SCHEMA_MODELS = {
    "Notam": models.NotamDTO,
    "NotamInterpretation": models.NotamInterpretationDTO,
    "NotamMapElement": models.NotamMapElementDTO,
    "NotamScheduleInterpretation": models.NotamScheduleInterpretationDTO,
    "AffectedElement": models.AffectedElementDTO,
    "AffectedElementFilter": models.ListenerAffectedElementFilter,
    "PrioritizedNotam": models.PrioritizedNotamDTO,
    "BriefingJobStatus": models.BriefingJobStatusDTO,
    "RecentlyExpiredNotam": models.RecentlyExpiredNotamDTO,
}


def load_spec() -> dict:
    if os.getenv("NOTAMIFY_SPEC_LIVE") == "1":
        with request.urlopen(LIVE_URL, timeout=20) as resp:
            return json.loads(resp.read())
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def model_fields(model: type[BaseModel]) -> set[str]:
    return set(model.model_fields) | {f.alias for f in model.model_fields.values() if f.alias}


class SpecCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = load_spec()

    def test_every_operation_has_a_client_method(self):
        from notamify_sdk.client import NotamifyClient

        documented = {(m, p) for p, ops in self.spec["paths"].items() for m in ops if m in {"get", "post", "put", "delete"}}
        missing = documented - set(OPERATIONS)
        self.assertFalse(missing, f"API operations without an SDK method: {sorted(missing)}; add them to the SDK and OPERATIONS")
        for method_name, _ in OPERATIONS.values():
            self.assertTrue(hasattr(NotamifyClient, method_name), method_name)

    def test_every_query_parameter_and_body_field_is_accepted(self):
        for (method, path), (_, model) in OPERATIONS.items():
            operation = self.spec["paths"].get(path, {}).get(method)
            if operation is None or model is None:
                continue
            documented = {p["name"] for p in operation.get("parameters", []) if p.get("in") == "query"}
            body = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if body:
                documented |= set(self._resolve(body).get("properties", {}))
            missing = documented - model_fields(model)
            self.assertFalse(missing, f"{method.upper()} {path}: {model.__name__} lacks {sorted(missing)}")

    def test_every_response_field_is_modelled(self):
        for name, schema in self.spec["components"]["schemas"].items():
            properties = schema.get("properties")
            if not properties:
                continue  # enums
            model = SCHEMA_MODELS.get(name) or getattr(models, name, None) or getattr(models, f"{name}DTO", None)
            self.assertIsNotNone(model, f"no SDK model for spec schema {name}")
            missing = set(properties) - model_fields(model)
            self.assertFalse(missing, f"{model.__name__} lacks spec fields {sorted(missing)}")

    def test_enums_match(self):
        schemas = self.spec["components"]["schemas"]
        for name, enum in (
            ("BriefingStatus", models.BriefingStatus),
            ("LocationType", models.LocationType),
            ("NotamPriority", models.NotamPriority),
        ):
            self.assertLessEqual(set(schemas[name]["enum"]), {e.value for e in enum}, name)

    def _resolve(self, schema: dict) -> dict:
        ref = schema.get("$ref")
        if not ref:
            return schema
        node = self.spec
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return node


if __name__ == "__main__":
    unittest.main()
