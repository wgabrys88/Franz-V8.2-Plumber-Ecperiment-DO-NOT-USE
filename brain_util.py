import json
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class SharedConfig:
    norm: int = 1000
    panel_port: int = 1236


@dataclass(frozen=True, slots=True)
class VLMConfig:
    model: str = "qwen2.5-vl-3b"
    temperature: float = 0.2
    max_tokens: int = 300
    top_p: float = 0.9
    stream: bool = False
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    route: float = 120.0
    capture: float = 30.0
    annotate: float = 25.0
    vlm: float = 360.0
    device: float = 30.0
    overlay_store: float = 10.0


SHARED: SharedConfig = SharedConfig()
VLM: VLMConfig = VLMConfig()
TIMEOUTS: TimeoutConfig = TimeoutConfig()
PANEL_URL: str = f"http://127.0.0.1:{SHARED.panel_port}/route"


@dataclass(frozen=True, slots=True)
class BrainArgs:
    region: str = "NONE"
    scale: float = 1.0


def _vlm_params(cfg: VLMConfig, **overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for f in fields(cfg):
        v: Any = getattr(cfg, f.name)
        if v is None or v is False:
            continue
        params[f.name] = v
    params.update(overrides)
    return params


def parse_brain_args(argv: list[str]) -> BrainArgs:
    region: str = "NONE"
    scale: float = 1.0
    for idx, arg in enumerate(argv):
        match arg:
            case "--region" if idx + 1 < len(argv):
                region = argv[idx + 1]
            case "--scale" if idx + 1 < len(argv):
                scale = float(argv[idx + 1])
    return BrainArgs(region=region, scale=scale)


def route(
    agent: str,
    recipients: list[str],
    timeout: float = TIMEOUTS.route,
    **payload: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"agent": agent, "recipients": recipients}
    body.update(payload)
    req: urllib.request.Request = urllib.request.Request(
        PANEL_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        error_body: str = ""
        try:
            error_body = exc.read().decode(errors="replace")
        except Exception:
            pass
        return {"error": f"HTTP {exc.code}: {error_body}"}
    except Exception as exc:
        return {"error": str(exc)}


def capture(
    agent: str, region: str,
    width: int = 0, height: int = 0,
    scale: float = 0.0, timeout: float = TIMEOUTS.capture,
) -> str:
    payload: dict[str, Any] = {"region": region}
    if scale > 0:
        payload["capture_scale"] = scale
    else:
        payload["capture_size"] = [width, height]
    resp: dict[str, Any] = route(agent, ["win32_capture"], timeout=timeout, **payload)
    return resp.get("image_b64", "")


def annotate(
    agent: str,
    image_b64: str, overlays: list[dict[str, Any]],
    timeout: float = TIMEOUTS.annotate,
) -> str:
    resp: dict[str, Any] = route(agent, ["annotate"], timeout=timeout,
                                 image_b64=image_b64, overlays=overlays)
    return resp.get("image_b64", "")


def vlm_text(
    agent: str,
    vlm_request: dict[str, Any], timeout: float = TIMEOUTS.vlm,
) -> str:
    resp: dict[str, Any] = route(agent, ["vlm"], timeout=timeout, vlm_request=vlm_request)
    if "error" in resp:
        return ""
    choices: list[dict[str, Any]] = resp.get("choices", [])
    if not choices:
        return ""
    message: dict[str, Any] = choices[0].get("message", {})
    return message.get("content", "")


def device(
    agent: str, region: str,
    actions: list[dict[str, Any]], timeout: float = TIMEOUTS.device,
) -> dict[str, Any]:
    return route(agent, ["win32_device"], timeout=timeout,
                 region=region, actions=actions)


def overlay(
    points: list[list[int]],
    closed: bool = False,
    stroke: str = "",
    stroke_width: int = 1,
    fill: str = "",
    label: str = "",
) -> dict[str, Any]:
    ov: dict[str, Any] = {"points": points, "closed": closed}
    if stroke:
        ov["stroke"] = stroke
        ov["stroke_width"] = stroke_width
    if fill:
        ov["fill"] = fill
    if label:
        ov["label"] = label
    return ov


def make_vlm_request(
    system_prompt: str,
    user_text: str,
    image_b64: str = "",
    **overrides: Any,
) -> dict[str, Any]:
    params: dict[str, Any] = _vlm_params(VLM, **overrides)
    user_content: str | list[dict[str, Any]]
    if image_b64:
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            {"type": "text", "text": user_text},
        ]
    else:
        user_content = user_text
    params["messages"] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    return params


def store_overlays(
    agent: str,
    overlays: list[dict[str, Any]],
    timeout: float = TIMEOUTS.overlay_store,
) -> dict[str, Any]:
    return route(agent, ["overlay_store"], timeout=timeout,
                 store_action="put", store_overlays=overlays)


def fetch_shared_overlays(
    agent: str,
    timeout: float = TIMEOUTS.overlay_store,
) -> list[dict[str, Any]]:
    resp: dict[str, Any] = route(agent, ["overlay_store"], timeout=timeout,
                                 store_action="get")
    return resp.get("overlays", [])


def clear_shared_overlays(
    agent: str,
    timeout: float = TIMEOUTS.overlay_store,
) -> dict[str, Any]:
    return route(agent, ["overlay_store"], timeout=timeout,
                 store_action="clear")
