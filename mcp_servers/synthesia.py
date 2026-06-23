"""Thin client for the Synthesia REST API v2."""

import os
import time
from pathlib import Path
from typing import Any, Optional

import requests


API_BASE = "https://api.synthesia.io/v2"


class SynthesiaError(Exception):
    """Raised when the Synthesia API returns an error response."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def load_api_key() -> str:
    """Load SYNTHESIA_API_KEY from the process environment."""

    key = os.environ.get("SYNTHESIA_API_KEY", "").strip()
    if not key:
        raise SynthesiaError(
            "SYNTHESIA_API_KEY is not set. Configure it in the deployment environment."
        )
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": load_api_key(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> Any:
    """Send an HTTP request to the Synthesia API and return parsed JSON."""

    url = f"{API_BASE}{path}"
    response = requests.request(
        method,
        url,
        headers=_headers(),
        params=params,
        json=json_body,
        timeout=120,
    )
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = response.text
        message = body.get("error", response.text) if isinstance(body, dict) else str(body)
        raise SynthesiaError(str(message), response.status_code, body)
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


def list_avatars(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List available avatars, trying known API paths for different account types."""

    params = {"limit": limit, "offset": offset}
    paths = ("/avatars", "/personal-avatars", "/studio-avatars", "/personas")
    errors: list[str] = []

    for path in paths:
        try:
            return _request("GET", path, params=params)
        except SynthesiaError as exc:
            if exc.status_code == 404:
                errors.append(f"{path}: not found")
                continue
            raise

    return {
        "avatars": [],
        "note": (
            "No avatar list endpoint is available for this API key. "
            "Copy avatar IDs from Synthesia Studio (avatar menu -> Copy ID) "
            "or see the stock avatar table in the API docs."
        ),
        "documentation": "https://docs.synthesia.io/reference/avatars",
        "attempted_paths": errors,
    }


def list_templates(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List video templates in the workspace."""

    return _request("GET", "/templates", params={"limit": limit, "offset": offset})


def create_video(payload: dict[str, Any]) -> dict[str, Any]:
    """Create a video from a full API request body."""

    return _request("POST", "/videos", json_body=payload)


def build_simple_video_payload(
    *,
    title: str,
    script_text: str,
    avatar: str,
    background: str = "green_screen",
    voice: Optional[str] = None,
    test: bool = True,
    aspect_ratio: str = "16:9",
    callback_id: Optional[str] = None,
) -> dict[str, Any]:
    """Build a single-scene create-video payload from common parameters."""

    clip: dict[str, Any] = {
        "avatar": avatar,
        "background": background,
        "scriptText": script_text,
    }
    if voice:
        clip["avatarSettings"] = {
            "style": "rectangular",
            "voice": voice,
        }
    payload: dict[str, Any] = {
        "title": title,
        "test": test,
        "aspectRatio": aspect_ratio,
        "input": [clip],
    }
    if callback_id:
        payload["callbackId"] = callback_id
    return payload


def create_video_from_template(
    template_id: str,
    template_data: dict[str, Any],
    *,
    title: Optional[str] = None,
    test: bool = True,
    callback_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create a video from a Synthesia Studio template."""

    payload: dict[str, Any] = {
        "templateId": template_id,
        "templateData": template_data,
        "test": test,
    }
    if title:
        payload["title"] = title
    if callback_id:
        payload["callbackId"] = callback_id
    return _request("POST", "/videos/fromTemplate", json_body=payload)


def get_video(video_id: str) -> dict[str, Any]:
    """Retrieve one video by ID."""

    return _request("GET", f"/videos/{video_id}")


def list_videos(limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """List videos in the workspace."""

    return _request("GET", "/videos", params={"limit": limit, "offset": offset})


def delete_video(video_id: str) -> dict[str, Any]:
    """Delete a video by ID."""

    return _request("DELETE", f"/videos/{video_id}")


def wait_for_video(
    video_id: str,
    *,
    poll_interval_seconds: float = 15.0,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    """Poll until the video reaches a terminal status or the timeout elapses."""

    terminal = {"complete", "error", "rejected", "deleted"}
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        last = get_video(video_id)
        status = last.get("status")
        if status in terminal:
            return last
        time.sleep(poll_interval_seconds)

    raise SynthesiaError(
        f"Timed out after {timeout_seconds}s waiting for video {video_id}. "
        f"Last status: {last.get('status', 'unknown')}"
    )


def download_video(video_id: str, output_path: str) -> str:
    """Download the MP4 for a completed video to a local file path."""

    video = get_video(video_id)
    if video.get("status") != "complete":
        raise SynthesiaError(
            f"Video {video_id} is not ready (status={video.get('status')}). "
            "Use wait_for_video or get_video until status is complete."
        )
    download_url = video.get("download")
    if not download_url:
        raise SynthesiaError(f"Video {video_id} has no download URL.")

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(download_url, stream=True, timeout=300)
    response.raise_for_status()
    with path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 64):
            if chunk:
                handle.write(chunk)
    return str(path.resolve())
