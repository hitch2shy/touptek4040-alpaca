from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {
            "Origin": "https://attacker.invalid",
            "Content-Type": "application/json",
            "X-ToupTek-Console": "1",
        },
        {
            "Origin": "null",
            "Content-Type": "application/json",
            "X-ToupTek-Console": "1",
        },
        {
            "Origin": "http://falconframework.org",
            "Content-Type": "text/plain",
            "X-ToupTek-Console": "1",
        },
        {
            "Origin": "http://falconframework.org",
            "Content-Type": "application/json",
        },
    ],
)
def test_mutations_reject_cross_site_opaque_or_non_json_requests(
    web_client,
    web_coordinator,
    headers,
):
    response = web_client.simulate_post(
        "/web/v1/exposure",
        headers=headers,
        body=b'{"duration":1}',
    )

    assert response.status_code == 403
    assert web_coordinator.exposure_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"duration": True},
        {"duration": 1, "unexpected": 2},
        {"duration": 0},
        {"duration": "1"},
    ],
)
def test_exposure_schema_rejects_wrong_types_ranges_and_unknown_fields(
    web_client,
    web_coordinator,
    mutation_headers,
    payload,
):
    response = web_client.simulate_post(
        "/web/v1/exposure",
        headers=mutation_headers,
        json=payload,
    )

    assert response.status_code == 400
    assert response.json["error"] == "Invalid request body"
    assert web_coordinator.exposure_calls == []


def test_mutation_body_size_is_bounded_before_json_decode(
    web_client,
    web_coordinator,
    mutation_headers,
):
    response = web_client.simulate_post(
        "/web/v1/exposure",
        headers=mutation_headers,
        body=b'{"duration":1,"padding":"' + (b"x" * 5000) + b'"}',
    )

    assert response.status_code == 413
    assert web_coordinator.exposure_calls == []


def test_security_headers_forbid_inline_script_eval_framing_and_referrer_leaks(
    web_client,
):
    response = web_client.simulate_get("/ui/")
    csp = response.headers["content-security-policy"]

    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp
    assert "script-src 'self'" in csp
    assert "img-src 'self' data: blob:" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
