import math
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import brain_util as bu


@dataclass(frozen=True, slots=True)
class TaskConfig:
    region: str = "NONE"
    scale: float = 1.0
    agent: str = "chess"
    parser_agent: str = "parser"
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    arrow_color: str = "rgba(255,60,60,0.9)"
    arrow_stroke_width: int = 3
    arrow_head_length_ratio: float = 0.55
    arrow_head_width_ratio: float = 0.32
    warning_y: int = 15
    agent_max_tokens: int = 200
    parser_max_tokens: int = 30
    post_action_delay: float = 5.0


AGENT_SYSTEM: str = """\
You are a chess engine playing as White. White pieces are at the bottom.
Red arrow on the image marks your last move with labeled squares — avoid repeating it.
Reply format: move FROM TO (e.g. move e2 e4). Nothing else.\
"""

AGENT_USER: str = """\
IT IS YOUR TURN. You must act now or the game will be lost.
{context}Reply with only the move.\
"""

PARSER_SYSTEM: str = """\
You are a Python programmer. You have exactly one function:
  drag('FROM', 'TO')
FROM and TO are chess squares like 'e2', 'g1', 'f3'.
Convert the user's move request into a single drag() call. Nothing else.\
"""


def build_overlays(cfg: TaskConfig, context: dict[str, Any]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = _make_grid_overlays(cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width)
    last_move: str = context.get("last_move", "")
    from_sq: str = last_move[:2] if len(last_move) >= 4 else ""
    to_sq: str = last_move[2:4] if len(last_move) >= 4 else ""
    overlays.extend(_make_arrow_overlay(cfg, from_sq, to_sq))
    return overlays


def build_user_message(context: dict[str, Any]) -> str:
    last_move: str = context.get("last_move", "")
    ctx: str = (
        f"Your last move was {last_move}. Make a different legal move with a White piece. "
        if last_move
        else "Make a legal move with a White piece to advance your position. "
    )
    return AGENT_USER.format(context=ctx)


def exec_action(cfg: TaskConfig, code: str, context: dict[str, Any]) -> None:
    clean: str = re.sub(r"<think>.*?</think>", "", code, flags=re.DOTALL)
    clean = re.sub(r"^```\w*\n?|```$", "", clean.strip(), flags=re.MULTILINE).strip()
    moved: list[str] = []

    def drag(fr: str, to: str) -> None:
        fr, to = fr.strip().lower(), to.strip().lower()
        from_x, from_y = _uci_to_norm(fr, cfg.grid_size)
        to_x, to_y = _uci_to_norm(to, cfg.grid_size)
        bu.device(cfg.agent, cfg.region, [
            {"type": "drag", "x1": from_x, "y1": from_y, "x2": to_x, "y2": to_y}
        ])
        moved.append(f"{fr}{to}")

    try:
        exec(clean, {"__builtins__": {}}, {"drag": drag})
    except Exception:
        pass

    if moved:
        context["last_move"] = moved[-1]


def run_step(cfg: TaskConfig, context: dict[str, Any]) -> None:
    base_b64: str = bu.capture(cfg.agent, cfg.region, scale=cfg.scale)
    if not base_b64:
        return

    overlays: list[dict[str, Any]] = build_overlays(cfg, context)
    annotated_b64: str = bu.annotate(cfg.agent, base_b64, overlays)
    if not annotated_b64:
        return

    user_message: str = build_user_message(context)
    agent_reply: str = bu.vlm_text(
        cfg.agent,
        bu.make_vlm_request(
            AGENT_SYSTEM, user_message,
            image_b64=annotated_b64,
            max_tokens=cfg.agent_max_tokens,
        ),
    )
    if not agent_reply:
        return

    parser_reply: str = bu.vlm_text(
        cfg.parser_agent,
        bu.make_vlm_request(
            PARSER_SYSTEM, agent_reply,
            max_tokens=cfg.parser_max_tokens,
        ),
    )
    if not parser_reply:
        return

    exec_action(cfg, parser_reply, context)


def main() -> None:
    args: bu.BrainArgs = bu.parse_brain_args(sys.argv[1:])
    cfg: TaskConfig = TaskConfig(region=args.region, scale=args.scale)
    context: dict[str, Any] = {}

    while True:
        run_step(cfg, context)
        time.sleep(cfg.post_action_delay)


def _uci_to_norm(square: str, grid_size: int) -> tuple[int, int]:
    col: int = ord(square[0]) - ord("a")
    row: int = int(square[1]) - 1
    step: int = bu.SHARED.norm // grid_size
    x: int = col * step + step // 2
    y: int = bu.SHARED.norm - (row * step + step // 2)
    return x, y


def _make_grid_overlays(grid_size: int, color: str, stroke_width: int) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    step: int = bu.SHARED.norm // grid_size
    for i in range(grid_size + 1):
        pos: int = i * step
        overlays.append(bu.overlay(
            points=[[pos, 0], [pos, bu.SHARED.norm]], stroke=color, stroke_width=stroke_width))
        overlays.append(bu.overlay(
            points=[[0, pos], [bu.SHARED.norm, pos]], stroke=color, stroke_width=stroke_width))
    return overlays


def _make_arrow_overlay(cfg: TaskConfig, from_sq: str, to_sq: str) -> list[dict[str, Any]]:
    if not from_sq or not to_sq:
        return []
    step: int = bu.SHARED.norm // cfg.grid_size
    fx, fy = _uci_to_norm(from_sq, cfg.grid_size)
    tx, ty = _uci_to_norm(to_sq, cfg.grid_size)
    dx: int = tx - fx
    dy: int = ty - fy
    length: float = math.hypot(dx, dy)
    if length == 0:
        return []
    ux: float = dx / length
    uy: float = dy / length
    head_len: float = step * cfg.arrow_head_length_ratio
    head_width: float = step * cfg.arrow_head_width_ratio
    shaft_tip_x: float = tx - ux * head_len
    shaft_tip_y: float = ty - uy * head_len
    px: float = -uy
    py: float = ux
    w1x: int = round(shaft_tip_x + px * head_width)
    w1y: int = round(shaft_tip_y + py * head_width)
    w2x: int = round(shaft_tip_x - px * head_width)
    w2y: int = round(shaft_tip_y - py * head_width)
    return [
        bu.overlay(
            points=[[round(fx), round(fy)], [round(shaft_tip_x), round(shaft_tip_y)]],
            stroke=cfg.arrow_color, stroke_width=cfg.arrow_stroke_width),
        bu.overlay(
            points=[[round(tx), round(ty)], [w1x, w1y], [w2x, w2y]],
            closed=True, fill=cfg.arrow_color, stroke=cfg.arrow_color, stroke_width=1),
        bu.overlay(
            points=[[round(fx), round(fy)]], stroke=cfg.arrow_color, stroke_width=1,
            label=from_sq.upper()),
        bu.overlay(
            points=[[round(tx), round(ty)]], stroke=cfg.arrow_color, stroke_width=1,
            label=to_sq.upper()),
        bu.overlay(
            points=[[bu.SHARED.norm // 2, cfg.warning_y]], stroke=cfg.arrow_color, stroke_width=1,
            label="PREVIOUS MOVE \u2014 DO NOT REPEAT UNLESS STRICTLY NECESSARY"),
    ]


if __name__ == "__main__":
    main()
