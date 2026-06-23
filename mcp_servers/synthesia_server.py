"""MCP server exposing Synthesia video API tools over Streamable HTTP."""

import json
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from mcp_servers import synthesia


# SYNTHESIA MCP
mcp = FastMCP("synthesia", stateless_http=True, streamable_http_path="/")


@mcp.tool()
def list_avatars(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List Synthesia avatars (stock and personal) with IDs for use in create_video.

    Args:
        limit: Maximum avatars to return (1-100).
        offset: Pagination offset.
    """

    return synthesia.list_avatars(limit=limit, offset=offset)


@mcp.tool()
def list_templates(limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """List Synthesia Studio templates available for create_video_from_template.

    Args:
        limit: Maximum templates to return.
        offset: Pagination offset.
    """

    return synthesia.list_templates(limit=limit, offset=offset)


@mcp.tool()
def create_video(
    title: str,
    script_text: str,
    avatar: str,
    background: str = "green_screen",
    voice: Optional[str] = None,
    test: bool = True,
    aspect_ratio: str = "16:9",
    callback_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a single-scene avatar video. Returns immediately with video id; rendering is async.

    Use get_video or wait_for_video to check status. Test mode (default) is free but watermarked.

    Args:
        title: Video title shown on the share page.
        script_text: Spoken script (text-to-speech).
        avatar: Avatar ID from list_avatars.
        background: Synthesia background id or asset id.
        voice: Optional voice UUID; omit for Synthesia default.
        test: If true, free watermarked preview (default true).
        aspect_ratio: One of 16:9, 9:16, 1:1, 4:5, 5:4.
        callback_id: Optional label to correlate this request later.
    """

    payload = synthesia.build_simple_video_payload(
        title=title,
        script_text=script_text,
        avatar=avatar,
        background=background,
        voice=voice,
        test=test,
        aspect_ratio=aspect_ratio,
        callback_id=callback_id,
    )
    return synthesia.create_video(payload)


@mcp.tool()
def create_video_advanced(request_json: str) -> Dict[str, Any]:
    """Create a video using the full Synthesia POST /v2/videos JSON body.

    Args:
        request_json: Full request body as JSON.
    """

    payload = json.loads(request_json)
    if not isinstance(payload, dict):
        raise ValueError("request_json must decode to a JSON object")
    return synthesia.create_video(payload)


@mcp.tool()
def create_video_from_template(
    template_id: str,
    template_data_json: str,
    title: Optional[str] = None,
    test: bool = True,
    callback_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a video from a Synthesia Studio template and variable substitutions.

    Args:
        template_id: Template UUID from list_templates.
        template_data_json: JSON object of template variables (keys match template placeholders).
        title: Optional video title override.
        test: If true, free watermarked preview (default true).
        callback_id: Optional label to correlate this request later.
    """

    template_data = json.loads(template_data_json)
    if not isinstance(template_data, dict):
        raise ValueError("template_data_json must decode to a JSON object")
    return synthesia.create_video_from_template(
        template_id,
        template_data,
        title=title,
        test=test,
        callback_id=callback_id,
    )


@mcp.tool()
def get_video(video_id: str) -> Dict[str, Any]:
    """Get video status and metadata. When status is complete, download URL is in the response.

    Args:
        video_id: Video UUID returned by create_video or create_video_from_template.
    """

    return synthesia.get_video(video_id)


@mcp.tool()
def list_videos(limit: int = 20, offset: int = 0) -> Dict[str, Any]:
    """List videos in the workspace.

    Args:
        limit: Maximum videos to return (1-100).
        offset: Pagination offset.
    """

    return synthesia.list_videos(limit=limit, offset=offset)


@mcp.tool()
def delete_video(video_id: str) -> Dict[str, Any]:
    """Delete a video by ID.

    Args:
        video_id: Video UUID to delete.
    """

    return synthesia.delete_video(video_id)


@mcp.tool()
def wait_for_video(
    video_id: str,
    poll_interval_seconds: float = 15.0,
    timeout_seconds: float = 1800.0,
) -> Dict[str, Any]:
    """Poll until the video is complete, failed, or the timeout is reached.

    Args:
        video_id: Video UUID to poll.
        poll_interval_seconds: Seconds between status checks.
        timeout_seconds: Maximum wait time (default 30 minutes).
    """

    return synthesia.wait_for_video(
        video_id,
        poll_interval_seconds=poll_interval_seconds,
        timeout_seconds=timeout_seconds,
    )


@mcp.tool()
def download_video(video_id: str, output_path: str) -> Dict[str, Any]:
    """Download the MP4 for a completed video to a local path.

    Args:
        video_id: Video UUID (must be status complete).
        output_path: Absolute or relative path for the saved .mp4 file.
    """

    saved = synthesia.download_video(video_id, output_path)
    return {"success": True, "file_path": saved}
