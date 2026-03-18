import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import brain_util as bu


@dataclass(frozen=True, slots=True)
class SwarmConfig:
    region: str = "NONE"
    scale: float = 1.0
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
    step_delay: float = 3.0
    max_debate_rounds: int = 8
    proposal_color_a: str = "rgba(163,230,53,0.9)"
    proposal_color_b: str = "rgba(96,165,250,0.9)"
    proposal_stroke_width: int = 3
    proposal_label_y: int = 40
    consensus_label_y: int = 65


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    name: str
    parser_name: str
    color: str
    system_prompt: str


AGENT_A: AgentIdentity = AgentIdentity(
    name="aggressive",
    parser_name="parser_a",
    color="rgba(163,230,53,0.9)",
    system_prompt="""\
You are an aggressive chess expert playing as White. White pieces are at the bottom.
You favor attacks, piece activity, and tactical threats over safety.
The image shows the board with a green grid. Square labels mark columns a-h and rows 1-8.
Red arrow marks the last executed move — do not repeat it.
Green arrows with labels show OTHER agents' move proposals. Read them carefully.
If you see a proposal from another agent and you AGREE it is the best move, reply: agree FROM TO
If you disagree or see no proposals, reply with YOUR best aggressive move: move FROM TO
Only reply with one line. Nothing else.\
""",
)

AGENT_B: AgentIdentity = AgentIdentity(
    name="positional",
    parser_name="parser_b",
    color="rgba(96,165,250,0.9)",
    system_prompt="""\
You are a positional chess expert playing as White. White pieces are at the bottom.
You favor solid pawn structure, piece coordination, and long-term advantage over tactics.
The image shows the board with a green grid. Square labels mark columns a-h and rows 1-8.
Red arrow marks the last executed move — do not repeat it.
Blue arrows with labels show OTHER agents' move proposals. Read them carefully.
If you see a proposal from another agent and you AGREE it is the best move, reply: agree FROM TO
If you disagree or see no proposals, reply with YOUR best positional move: move FROM TO
Only reply with one line. Nothing else.\
""",
)

PARSER_SYSTEM: str = """\
You are a Python programmer. You have exactly two functions:
  propose('FROM', 'TO')
  drag('FROM', 'TO')
FROM and TO are chess squares like 'e2', 'g1', 'f3'.
If the user says "move", output a single propose() call.
If the user says "agree", output a single drag() call.
Nothing else.\
"""

USER_TEMPLATE: str = """\
IT IS YOUR TURN. You must act now or the game will be lost.
{context}Reply with only the move or agreement.\
"""


def main() -> None:
    args: bu.BrainArgs = bu.parse_brain_args(sys.argv[1:])
    cfg: SwarmConfig = SwarmConfig(region=args.region, scale=args.scale)
    context: dict[str, Any] = {}

    while True:
        run_debate(cfg, context)
        time.sleep(cfg.step_delay)


def run_debate(cfg: SwarmConfig, context: dict[str, Any]) -> None:
    bu.clear_shared_overlays("swarm")

    for debate_round in range(cfg.max_debate_rounds):
        results: list[dict[str, Any]] = []
        threads: list[threading.Thread] = []

        for identity in (AGENT_A, AGENT_B):
            result: dict[str, Any] = {}
            results.append(result)
            t: threading.Thread = threading.Thread(
                target=_agent_step,
                args=(cfg, identity, context, result),
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        executed: bool = False
        for result in results:
            if result.get("action") == "drag":
                _execute_drag(cfg, result["from_sq"], result["to_sq"], context)
                executed = True
                break

        if executed:
            bu.clear_shared_overlays("swarm")
            return

        time.sleep(cfg.step_delay)

    _force_consensus(cfg, context)


def _agent_step(
    cfg: SwarmConfig,
    identity: AgentIdentity,
    context: dict[str, Any],
    result: dict[str, Any],
) -> None:
    base_b64: str = bu.capture(identity.name, cfg.region, scale=cfg.scale)
    if not base_b64:
        return

    grid_overlays: list[dict[str, Any]] = _make_grid_overlays(
        cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width)
    last_move_overlays: list[dict[str, Any]] = _make_last_move_overlay(cfg, context)
    shared_overlays: list[dict[str, Any]] = bu.fetch_shared_overlays(identity.name)
    all_overlays: list[dict[str, Any]] = grid_overlays + last_move_overlays + shared_overlays

    annotated_b64: str = bu.annotate(identity.name, base_b64, all_overlays)
    if not annotated_b64:
        return

    user_message: str = _build_user_message(context)
    agent_reply: str = bu.vlm_text(
        identity.name,
        bu.make_vlm_request(
            identity.system_prompt, user_message,
            image_b64=annotated_b64,
            max_tokens=cfg.agent_max_tokens,
        ),
    )
    if not agent_reply:
        return

    parser_reply: str = bu.vlm_text(
        identity.parser_name,
        bu.make_vlm_request(
            PARSER_SYSTEM, agent_reply,
            max_tokens=cfg.parser_max_tokens,
        ),
    )
    if not parser_reply:
        return

    _exec_parser_output(cfg, identity, parser_reply, context, result)


def _exec_parser_output(
    cfg: SwarmConfig,
    identity: AgentIdentity,
    code: str,
    context: dict[str, Any],
    result: dict[str, Any],
) -> None:
    clean: str = re.sub(r"<think>.*?</think>", "", code, flags=re.DOTALL)
    clean = re.sub(r"^```\w*\n?|```$", "", clean.strip(), flags=re.MULTILINE).strip()

    def propose(fr: str, to: str) -> None:
        fr, to = fr.strip().lower(), to.strip().lower()
        proposal_overlays: list[dict[str, Any]] = _make_proposal_overlay(
            cfg, identity, fr, to)
        bu.store_overlays(identity.name, proposal_overlays)
        result["action"] = "propose"
        result["from_sq"] = fr
        result["to_sq"] = to

    def drag(fr: str, to: str) -> None:
        fr, to = fr.strip().lower(), to.strip().lower()
        result["action"] = "drag"
        result["from_sq"] = fr
        result["to_sq"] = to

    try:
        exec(clean, {"__builtins__": {}}, {"propose": propose, "drag": drag})
    except Exception:
        pass


def _execute_drag(
    cfg: SwarmConfig,
    from_sq: str,
    to_sq: str,
    context: dict[str, Any],
) -> None:
    from_x, from_y = _uci_to_norm(from_sq, cfg.grid_size)
    to_x, to_y = _uci_to_norm(to_sq, cfg.grid_size)
    bu.device("swarm", cfg.region, [
        {"type": "drag", "x1": from_x, "y1": from_y, "x2": to_x, "y2": to_y}
    ])
    context["last_move"] = f"{from_sq}{to_sq}"


def _force_consensus(cfg: SwarmConfig, context: dict[str, Any]) -> None:
    shared: list[dict[str, Any]] = bu.fetch_shared_overlays("swarm")
    if not shared:
        return

    base_b64: str = bu.capture("swarm", cfg.region, scale=cfg.scale)
    if not base_b64:
        return

    grid_overlays: list[dict[str, Any]] = _make_grid_overlays(
        cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width)
    all_overlays: list[dict[str, Any]] = grid_overlays + shared

    annotated_b64: str = bu.annotate("swarm", base_b64, all_overlays)
    if not annotated_b64:
        return

    tiebreak_reply: str = bu.vlm_text(
        "swarm",
        bu.make_vlm_request(
            "You are a chess tiebreaker. Look at the proposed moves on the board. "
            "Pick the single best legal move for White. Reply: move FROM TO. Nothing else.",
            "Debate timed out. Choose the best proposal now.",
            image_b64=annotated_b64,
            max_tokens=cfg.agent_max_tokens,
        ),
    )
    if not tiebreak_reply:
        bu.clear_shared_overlays("swarm")
        return

    parser_reply: str = bu.vlm_text(
        "parser_tb",
        bu.make_vlm_request(
            PARSER_SYSTEM, tiebreak_reply,
            max_tokens=cfg.parser_max_tokens,
        ),
    )
    if not parser_reply:
        bu.clear_shared_overlays("swarm")
        return

    clean: str = re.sub(r"<think>.*?</think>", "", parser_reply, flags=re.DOTALL)
    clean = re.sub(r"^```\w*\n?|```$", "", clean.strip(), flags=re.MULTILINE).strip()
    moved: list[str] = []

    def propose(fr: str, to: str) -> None:
        moved.append(f"{fr.strip().lower()}{to.strip().lower()}")

    def drag(fr: str, to: str) -> None:
        moved.append(f"{fr.strip().lower()}{to.strip().lower()}")

    try:
        exec(clean, {"__builtins__": {}}, {"propose": propose, "drag": drag})
    except Exception:
        pass

    if moved:
        sq: str = moved[-1]
        _execute_drag(cfg, sq[:2], sq[2:4], context)

    bu.clear_shared_overlays("swarm")


def _build_user_message(context: dict[str, Any]) -> str:
    last_move: str = context.get("last_move", "")
    ctx: str = (
        f"Your last executed move was {last_move}. Make a different legal move with a White piece. "
        if last_move
        else "Make a legal move with a White piece to advance your position. "
    )
    return USER_TEMPLATE.format(context=ctx)


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


def _make_last_move_overlay(cfg: SwarmConfig, context: dict[str, Any]) -> list[dict[str, Any]]:
    last_move: str = context.get("last_move", "")
    from_sq: str = last_move[:2] if len(last_move) >= 4 else ""
    to_sq: str = last_move[2:4] if len(last_move) >= 4 else ""
    if not from_sq or not to_sq:
        return []
    return _make_arrow_overlay(cfg, from_sq, to_sq, cfg.arrow_color, cfg.arrow_stroke_width)


def _make_arrow_overlay(
    cfg: SwarmConfig,
    from_sq: str, to_sq: str,
    color: str, stroke_width: int,
) -> list[dict[str, Any]]:
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
            stroke=color, stroke_width=stroke_width),
        bu.overlay(
            points=[[round(tx), round(ty)], [w1x, w1y], [w2x, w2y]],
            closed=True, fill=color, stroke=color, stroke_width=1),
        bu.overlay(
            points=[[round(fx), round(fy)]], stroke=color, stroke_width=1,
            label=from_sq.upper()),
        bu.overlay(
            points=[[round(tx), round(ty)]], stroke=color, stroke_width=1,
            label=to_sq.upper()),
    ]


def _make_proposal_overlay(
    cfg: SwarmConfig,
    identity: AgentIdentity,
    from_sq: str, to_sq: str,
) -> list[dict[str, Any]]:
    arrow_overlays: list[dict[str, Any]] = _make_arrow_overlay(
        cfg, from_sq, to_sq, identity.color, cfg.proposal_stroke_width)
    label_text: str = f"{identity.name.upper()} PROPOSES: {from_sq.upper()}\u2192{to_sq.upper()} \u2014 AGREE?"
    label_y: int = (cfg.proposal_label_y
                    if identity.name == AGENT_A.name
                    else cfg.consensus_label_y)
    arrow_overlays.append(bu.overlay(
        points=[[bu.SHARED.norm // 2, label_y]],
        stroke=identity.color, stroke_width=1,
        label=label_text))
    return arrow_overlays


if __name__ == "__main__":
    main()
