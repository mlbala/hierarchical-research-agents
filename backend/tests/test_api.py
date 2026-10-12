"""HTTP API behaviour: idempotency, validation, state transitions, security."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from app.services.worker import ResearchWorker
from tests.conftest import MUTATION_HEADERS

pytestmark = pytest.mark.db

QUESTION = {"question": "Should our city fund e-bike vouchers?", "depth": "quick"}


async def create_project(client: httpx.AsyncClient, name: str = "Mobility") -> dict[str, Any]:
    response = await client.post(
        "/api/v1/projects", headers=MUTATION_HEADERS, json={"name": name, "description": ""}
    )
    assert response.status_code == 201
    return response.json()


async def post_run(
    client: httpx.AsyncClient, project_id: str, body: dict[str, Any], key: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/projects/{project_id}/runs",
        headers={**MUTATION_HEADERS, "Idempotency-Key": key},
        json=body,
    )


async def test_run_creation_is_idempotent(client: httpx.AsyncClient) -> None:
    project = await create_project(client)
    first = await post_run(client, project["id"], QUESTION, "idem-key-0001")
    again = await post_run(client, project["id"], QUESTION, "idem-key-0001")
    assert first.status_code == 201 and again.status_code == 200
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["status"] == "queued"
    listing = (await client.get(f"/api/v1/projects/{project['id']}/runs")).json()
    assert listing["total"] == 1

    reused = await post_run(
        client,
        project["id"],
        {**QUESTION, "question": "A different question here"},
        "idem-key-0001",
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    missing = await client.post(
        f"/api/v1/projects/{project['id']}/runs", headers=MUTATION_HEADERS, json=QUESTION
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"question": "short"}, "question"),
        ({"question": "x" * 2001}, "question"),
        ({**QUESTION, "depth": "deep"}, "depth"),
        ({**QUESTION, "input_urls": ["http://127.0.0.1/admin"]}, "input_urls"),
        ({**QUESTION, "input_urls": ["https://a.example/"] * 6}, "input_urls"),
        ({**QUESTION, "unexpected": True}, "unexpected"),
    ],
)
async def test_run_input_is_validated(
    client: httpx.AsyncClient, body: dict[str, Any], field: str
) -> None:
    project = await create_project(client)
    response = await post_run(client, project["id"], body, f"key-{uuid.uuid4()}")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert any(d["field"] == field for d in error["details"])
    assert "Traceback" not in response.text


async def test_unknown_resources_return_the_error_envelope(client: httpx.AsyncClient) -> None:
    missing = uuid.uuid4()
    for path in (
        f"/api/v1/projects/{missing}",
        f"/api/v1/runs/{missing}",
        f"/api/v1/runs/{missing}/report",
        "/api/v1/nope",
    ):
        response = await client.get(path)
        assert response.status_code == 404
        body = response.json()["error"]
        assert set(body) >= {"code", "message", "request_id", "retryable"}
        assert body["request_id"] == response.headers["x-request-id"]
    bad_id = await client.get("/api/v1/runs/not-a-uuid")
    assert bad_id.status_code == 422


async def test_pagination_is_bounded(client: httpx.AsyncClient) -> None:
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        response = await client.get("/api/v1/projects", params=params)
        assert response.status_code == 422
    for i in range(3):
        await create_project(client, f"P{i}")
    page = (await client.get("/api/v1/projects", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["offset"] == 1


async def test_cancel_queued_run_and_reject_invalid_transitions(
    client: httpx.AsyncClient,
) -> None:
    project = await create_project(client)
    run = (await post_run(client, project["id"], QUESTION, "cancel-key-001")).json()
    cancelled = await client.post(
        f"/api/v1/runs/{run['id']}/cancel", headers=MUTATION_HEADERS, json={}
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    again = await client.post(f"/api/v1/runs/{run['id']}/cancel", headers=MUTATION_HEADERS, json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_STATE"
    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert detail["cancel_requested_at"] is not None and detail["finished_at"] is not None


async def test_rerun_creates_a_new_run_with_the_same_inputs(client: httpx.AsyncClient) -> None:
    project = await create_project(client)
    body = {**QUESTION, "context": "Mid-sized city", "input_urls": ["https://example.org/a"]}
    original = (await post_run(client, project["id"], body, "rerun-key-0001")).json()
    rerun = await client.post(
        f"/api/v1/runs/{original['id']}/rerun",
        headers={**MUTATION_HEADERS, "Idempotency-Key": "rerun-key-0002"},
        json={},
    )
    assert rerun.status_code == 201
    new = (await client.get(f"/api/v1/runs/{rerun.json()['id']}")).json()
    assert new["id"] != original["id"]
    assert new["question"] == body["question"] and new["context"] == "Mid-sized city"
    assert new["input_urls"] == ["https://example.org/a"]


async def test_queue_cap_and_rate_limit(client: httpx.AsyncClient) -> None:
    services = client._transport.app.state.services  # type: ignore[attr-defined]
    services.settings = services.settings.model_copy(update={"max_queued_runs": 2})
    project = await create_project(client)
    for i in range(2):
        assert (
            await post_run(client, project["id"], QUESTION, f"cap-key-{i:04}")
        ).status_code == 201
    full = await post_run(client, project["id"], QUESTION, "cap-key-9999")
    assert full.status_code == 429 and full.json()["error"]["code"] == "QUEUE_FULL"

    services.settings = services.settings.model_copy(update={"max_queued_runs": 100})
    services.run_rate_limiter.limit = 3  # two already used above, plus the rejected one
    limited = await post_run(client, project["id"], QUESTION, "rate-key-0001")
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.headers["retry-after"] == "60"


async def test_live_mode_without_keys_fails_clearly(client: httpx.AsyncClient) -> None:
    services = client._transport.app.state.services  # type: ignore[attr-defined]
    services.settings = services.settings.model_copy(update={"demo_mode": False})
    project = await create_project(client)
    response = await post_run(client, project["id"], QUESTION, "live-key-0001")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "CONFIGURATION_ERROR"
    assert "ANTHROPIC_API_KEY" in error["message"] and "TAVILY_API_KEY" in error["message"]
    config = (await client.get("/api/v1/config")).json()
    assert config["execution_mode"] == "live" and config["live_ready"] is False
    assert {i["key"] for i in config["issues"]} >= {"ANTHROPIC_API_KEY", "TAVILY_API_KEY"}


@pytest.mark.parametrize(
    ("headers", "code", "status"),
    [
        ({"Content-Type": "application/json"}, "CSRF_HEADER_MISSING", 403),
        ({**MUTATION_HEADERS, "Origin": "https://evil.example"}, "ORIGIN_NOT_ALLOWED", 403),
        ({**MUTATION_HEADERS, "Sec-Fetch-Site": "cross-site"}, "ORIGIN_NOT_ALLOWED", 403),
        ({"X-Research-Hive": "1", "Content-Type": "text/plain"}, "UNSUPPORTED_MEDIA_TYPE", 415),
        ({**MUTATION_HEADERS, "Host": "attacker.example"}, "HOST_NOT_ALLOWED", 400),
    ],
)
async def test_mutation_guard(
    client: httpx.AsyncClient, headers: dict[str, str], code: str, status: int
) -> None:
    response = await client.post("/api/v1/projects", headers=headers, content=b'{"name": "x"}')
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


async def test_allowed_origin_and_security_headers(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/projects",
        headers={**MUTATION_HEADERS, "Origin": "http://localhost:5173"},
        json={"name": "From the dev UI"},
    )
    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    health = await client.get("/api/v1/health/live")
    assert "default-src 'self'" in health.headers["content-security-policy"]
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["cache-control"] == "no-store"
    preflight = await client.options(
        "/api/v1/projects",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in preflight.headers


async def test_readiness_reports_without_secrets(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["database"] == "ok" and body["checks"]["migrations"] == "ok"
    assert "password" not in response.text.lower()


async def test_full_demo_flow_over_http(client: httpx.AsyncClient) -> None:
    """Question -> bounded agents -> evidence -> cited report -> saved history."""
    services = client._transport.app.state.services  # type: ignore[attr-defined]
    project = await create_project(client)
    run = (await post_run(client, project["id"], QUESTION, "flow-key-0001")).json()
    worker = ResearchWorker(services.settings, services.db, services.broker)
    claimed = await worker.claim_next()
    assert str(claimed) == run["id"]
    import asyncio

    await worker.execute(claimed, asyncio.Event())

    detail = (await client.get(f"/api/v1/runs/{run['id']}")).json()
    assert detail["status"] == "completed"
    assert detail["estimated_cost_usd"] == "0.000000"  # demo mode makes no paid calls
    assert detail["handoff"]["confidence"] in {"low", "medium", "high"}

    report = (await client.get(f"/api/v1/runs/{run['id']}/report")).json()
    assert report["execution_mode"] == "demo"
    numbers = [c["number"] for c in report["citations"]]
    assert numbers == list(range(1, len(numbers) + 1))
    for citation in report["citations"]:
        assert f"[{citation['number']}]" in report["body_markdown"]
        assert citation["source"]["content_kind"] == "demo"

    markdown = await client.get(f"/api/v1/runs/{run['id']}/report.md")
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in markdown.headers["content-disposition"]
    text = markdown.text
    for heading in (
        "## Executive summary",
        "## Direct answer",
        "## Key findings",
        "## Detailed analysis",
        "## Limitations and unresolved questions",
        "## Sources",
    ):
        assert heading in text
    assert "Demo mode" in text
    assert "<script" not in text  # the injection fixture never reaches the report

    findings = (await client.get(f"/api/v1/runs/{run['id']}/findings")).json()
    assert findings["total"] > 0
    assert {f["review_status"] for f in findings["items"]} == {"approved"}
    sources = (await client.get(f"/api/v1/runs/{run['id']}/sources")).json()
    injection = [s for s in sources["items"] if "prompt-injection" in s["title"]]
    assert injection, "the prompt-injection fixture should be collected as plain data"
    source_detail = (
        await client.get(f"/api/v1/runs/{run['id']}/sources/{injection[0]['id']}")
    ).json()
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in source_detail["content_text"]

    hits = (await client.get("/api/v1/search/reports", params={"q": "voucher"})).json()
    assert hits["total"] == 1 and hits["items"][0]["run_id"] == run["id"]
    none = (
        await client.get("/api/v1/search/reports", params={"q": "quantum chromodynamics"})
    ).json()
    assert none["total"] == 0
