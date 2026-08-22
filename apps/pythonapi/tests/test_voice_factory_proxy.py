"""The one door to the voice factory.

These replace the per-route tests that used to live in test_voice.py for
search, characters, videos, speakers, clip decisions, clip audio, and commit.
Those routes were typed passthroughs and are gone; the proxy forwards all of
them, so what is worth asserting changed with them.

A typed route's test could stub the gateway. The proxy has no gateway - it
speaks HTTP - so these stub the transport instead, which is also the only way
to prove the parts that matter: that the method, path, query, body, and status
all survive the hop unedited.
"""

import httpx
import pytest

from pythonapi.config import settings

FACTORY_URL = "http://voice-factory.test"


@pytest.fixture
def factory(monkeypatch):
    """Point the proxy at a stubbed factory and record what reaches it."""
    monkeypatch.setattr(settings, "VOICE_FACTORY_URL", FACTORY_URL)

    seen: list[httpx.Request] = []
    responses: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        return responses.get(
            key, httpx.Response(404, json={"detail": "no stub for this path"})
        )

    transport = httpx.MockTransport(handler)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    class Factory:
        requests = seen

        @staticmethod
        def reply(method: str, path: str, response: httpx.Response) -> None:
            responses[(method, path)] = response

    return Factory


def test_the_proxy_reports_503_when_the_factory_is_not_configured(
    client, monkeypatch
):
    """Unset means the feature is off, not that the deployment is broken -
    the same contract every typed voice route already had."""
    monkeypatch.setattr(settings, "VOICE_FACTORY_URL", "")

    response = client.get("/api/voice-factory/videos")

    assert response.status_code == 503
    assert "VOICE_FACTORY_URL" in response.json()["detail"]


def test_it_forwards_the_path_and_query_unchanged(client, factory):
    factory.reply(
        "GET",
        "/search",
        httpx.Response(200, json={"query": "janeway", "videos": []}),
    )

    response = client.get("/api/voice-factory/search", params={"query": "janeway"})

    assert response.status_code == 200
    assert response.json()["query"] == "janeway"
    forwarded = factory.requests[-1]
    assert forwarded.url.path == "/search"
    assert forwarded.url.params["query"] == "janeway"


def test_it_returns_the_factory_body_verbatim(client, factory):
    """A field the factory adds must reach the browser with no edit here -
    the whole reason there is no response model on this route."""
    body = {
        "videos": [
            {
                "video_id": "vid_abc123",
                "title": "Janeway speaks",
                "a_field_added_later": "survives",
            }
        ]
    }
    factory.reply("GET", "/videos", httpx.Response(200, json=body))

    response = client.get("/api/voice-factory/videos")

    assert response.json() == body


def test_it_forwards_a_patch_body(client, factory):
    factory.reply(
        "PATCH", "/videos/vid_abc123/clips", httpx.Response(200, json={"updated": 2})
    )

    response = client.patch(
        "/api/voice-factory/videos/vid_abc123/clips",
        json={"decisions": [{"clip_id": "c1", "text": "corrected"}]},
    )

    assert response.status_code == 200
    assert response.json() == {"updated": 2}
    assert b"corrected" in factory.requests[-1].content


def test_it_forwards_a_rename(client, factory):
    factory.reply(
        "PATCH",
        "/videos/vid_abc123",
        httpx.Response(200, json={"video_id": "vid_abc123", "title": "Renamed"}),
    )

    response = client.patch(
        "/api/voice-factory/videos/vid_abc123", json={"title": "Renamed"}
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Renamed"


@pytest.mark.parametrize("status_code", [400, 404, 409, 422])
def test_it_preserves_a_factory_4xx(client, factory, status_code):
    """A 409 on a clip already labelled for another character is the
    operator's answer to read, not a factory outage, so it must not be
    reshaped into a 502."""
    factory.reply(
        "PATCH",
        "/videos/vid_abc123/clips",
        httpx.Response(status_code, json={"detail": "the factory's own words"}),
    )

    response = client.patch(
        "/api/voice-factory/videos/vid_abc123/clips",
        json={"decisions": [{"clip_id": "c1", "speaker_label": "SPEAKER_01"}]},
    )

    assert response.status_code == status_code
    assert response.json()["detail"] == "the factory's own words"


def test_it_reports_502_when_the_factory_does_not_answer(client, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_FACTORY_URL", FACTORY_URL)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(refuse)
    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    response = client.get("/api/voice-factory/videos")

    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]


def test_it_streams_clip_audio(client, factory):
    """Audio is the one response worth streaming: a clip is megabytes, and
    buffering it whole would hold a worker for the download."""
    factory.reply(
        "GET",
        "/videos/vid_abc123/clips/c1/audio",
        httpx.Response(
            200, content=b"RIFFfake-wav-bytes", headers={"content-type": "audio/wav"}
        ),
    )

    response = client.get("/api/voice-factory/videos/vid_abc123/clips/c1/audio")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content == b"RIFFfake-wav-bytes"


def test_it_forwards_a_commit(client, factory):
    factory.reply(
        "POST", "/commit", httpx.Response(200, json={"committed": {"vid_abc123": 12}})
    )

    response = client.post(
        "/api/voice-factory/commit",
        json={"assignments": {"vid_abc123": {"SPEAKER_00": "janeway"}}},
    )

    assert response.status_code == 200
    assert response.json()["committed"]["vid_abc123"] == 12
