"""
ComfyUI API service for image generation.

Handles queuing workflows, polling for completion, and fetching output images.
"""
import json
import logging
import uuid
import aiohttp

from config.settings import COMFYUI_URL

logger = logging.getLogger(__name__)

# Path to the workflow JSON relative to the project root
WORKFLOW_PATH = "workflows/art.json"

# Workflow node ID containing the positive prompt text
PROMPT_NODE_ID = "15"


class ComfyUIError(Exception):
    """Raised when a ComfyUI API operation fails."""


async def generate_image(prompt: str, workflow_path: str = WORKFLOW_PATH) -> bytes:
    """
    High-level orchestrator: load workflow, inject prompt, queue, poll, fetch image.

    Args:
        prompt: The positive prompt text to send to ComfyUI.
        workflow_path: Path to the workflow JSON file.

    Returns:
        Raw bytes of the generated PNG image.

    Raises:
        ComfyUIError: If any step in the pipeline fails.
    """
    # 1. Load workflow
    workflow = _load_workflow(workflow_path)

    # 2. Inject prompt
    if PROMPT_NODE_ID not in workflow:
        raise ComfyUIError(
            f"Node '{PROMPT_NODE_ID}' not found in workflow. "
            "Cannot inject prompt."
        )
    workflow[PROMPT_NODE_ID]["inputs"]["text"] = prompt

    # 3. Generate a unique client ID
    client_id = f"umacore-{uuid.uuid4().hex[:12]}"

    # 4. Queue the prompt
    prompt_id = await _queue_prompt(workflow, client_id)
    logger.info(f"ComfyUI prompt queued: {prompt_id}")

    # 5. Poll for completion
    images = await _poll_history(prompt_id)
    if not images:
        raise ComfyUIError(
            f"Generation completed (prompt_id={prompt_id}) but no output images were found."
        )

    # 6. Fetch the first output image
    image_bytes = await _fetch_image(images[0])
    logger.info(
        f"ComfyUI image fetched: {images[0]['filename']} "
        f"({len(image_bytes)} bytes)"
    )
    return image_bytes


def _load_workflow(workflow_path: str) -> dict:
    """Load and return a deep copy of the workflow JSON."""
    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ComfyUIError(f"Workflow file not found: {workflow_path}")
    except json.JSONDecodeError as e:
        raise ComfyUIError(f"Invalid JSON in workflow file: {e}")


async def _queue_prompt(workflow: dict, client_id: str) -> str:
    """
    POST the workflow to ComfyUI's /prompt endpoint.

    Returns the prompt_id used for polling.
    """
    url = f"{COMFYUI_URL}/prompt"
    payload = {
        "prompt": workflow,
        "client_id": client_id,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ComfyUIError(
                        f"ComfyUI queue error: HTTP {resp.status} — {body}"
                    )
                data = await resp.json()
    except aiohttp.ClientError as e:
        raise ComfyUIError(f"Failed to connect to ComfyUI at {COMFYUI_URL}: {e}")

    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise ComfyUIError(
            f"No prompt_id returned from ComfyUI. Response: {data}"
        )
    return prompt_id


async def _poll_history(
    prompt_id: str,
    timeout: float = 300,
    interval: float = 2,
) -> list[dict]:
    """
    Poll ComfyUI's /history/{prompt_id} until the prompt completes or times out.

    Returns a list of image info dicts: [{"filename": ..., "subfolder": ..., "type": ...}]
    """
    url = f"{COMFYUI_URL}/history/{prompt_id}"
    elapsed = 0.0

    async with aiohttp.ClientSession() as session:
        while elapsed < timeout:
            await _async_sleep(interval)
            elapsed += interval

            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        # Still processing — ComfyUI may return non-200 while queuing
                        logger.debug(
                            f"ComfyUI history returned {resp.status} "
                            f"after {elapsed:.0f}s — still processing"
                        )
                        continue
                    history_data = await resp.json()
            except aiohttp.ClientError:
                # Transient network error — retry
                logger.debug(f"ComfyUI poll network error at {elapsed:.0f}s — retrying")
                continue

            prompt_data = history_data.get(prompt_id)
            if not prompt_data:
                logger.debug(f"Prompt {prompt_id} not yet in history ({elapsed:.0f}s)")
                continue

            # Check completion status
            status = prompt_data.get("status", {})
            if not status.get("completed", False):
                logger.debug(f"Prompt {prompt_id} not yet completed ({elapsed:.0f}s)")
                continue

            # Extract images from outputs
            images = _extract_images(prompt_data.get("outputs", {}))
            return images

    raise ComfyUIError(
        f"Timed out after {timeout:.0f}s waiting for ComfyUI prompt {prompt_id}"
    )


def _extract_images(outputs: dict) -> list[dict]:
    """Extract image info dicts from all output nodes."""
    images = []
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue
        for img in node_output.get("images", []):
            images.append({
                "filename": img["filename"],
                "subfolder": img.get("subfolder", ""),
                "type": img.get("type", "output"),
            })
    return images


async def _fetch_image(image_info: dict) -> bytes:
    """Fetch the raw image bytes from ComfyUI's /view endpoint."""
    url = f"{COMFYUI_URL}/view"
    params = {
        "filename": image_info["filename"],
        "subfolder": image_info["subfolder"],
        "type": image_info["type"],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise ComfyUIError(
                        f"Failed to fetch image '{image_info['filename']}': "
                        f"HTTP {resp.status} — {body}"
                    )
                return await resp.read()
    except aiohttp.ClientError as e:
        raise ComfyUIError(f"Failed to fetch image from ComfyUI: {e}")


async def _async_sleep(seconds: float) -> None:
    """Wrapper around asyncio.sleep for readability."""
    import asyncio
    await asyncio.sleep(seconds)