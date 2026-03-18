```markdown
# Franz Plumbing -- Autonomous Agent Platform

A platform where vision-language models physically control a Windows 11 PC
by looking at screenshots, thinking about what they see, and moving the mouse.
Intelligence lives in VLM calls. Everything else is dumb plumbing.

---

## What This Is

Imagine sitting a very smart robot in front of your computer, letting it look at
the screen through a camera, think about what it sees, and then move the mouse and
type on the keyboard to accomplish tasks. That is what this project does -- except
the "robot" is a vision-language model (a type of AI that understands images)
running on your own PC.

The project is built like plumbing in a house:
- Pipes carry messages between components (the panel server)
- Faucets do physical actions on screen (the Win32 controller)
- A window shows you what flows through the pipes in real time (the dashboard)
- Brains are the smart parts that decide what to do (VLM calls)

The pipes do not care what flows through them. The faucets do not care who turned
them on. Each piece works independently. You can replace any brain with a completely
different one -- chess, web browsing, form filling, game playing, or a swarm of
debating agents -- and the plumbing just works.

---

## Architecture

### File Map

| File | Lines | Role |
|------|------:|------|
| panel.py | ~800 | HTTP router, JSONL logger, subprocess launcher, SSE server, overlay store |
| panel.html | ~950 | PCB dashboard with physics, annotation renderer (OffscreenCanvas), replay UI |
| win32.py | ~860 | Screen capture, mouse/keyboard input, region selector (ctypes, Windows-only) |
| brain_chess_players.py | ~200 | Single-agent chess brain: two-VLM pipeline with exec()-based code generation |
| brain_chess_swarm.py | ~250 | Multi-agent swarm brain: peer debate via shared overlay memory |
| brain_util.py | ~190 | Brain SDK: HTTP client for capture, annotate, VLM, device, overlay store |

### Process Boundaries

Every file runs in its own process. They communicate only through HTTP and
subprocess pipes.

```
panel.py              <-- HTTP server, sole logger, subprocess launcher
  |-- win32.py        <-- subprocess: screen capture, mouse, keyboard
  |-- panel.html      <-- browser: dashboard + annotation renderer
  +-- brain_*.py      <-- subprocess: any brain (uses brain_util.py)
        +-- brain_util.py  <-- SDK: HTTP client to panel.py
```

### Coordinate System

Everything uses a 1000x1000 normalized coordinate space (NORM = 1000).
Screen positions, regions, overlay points -- all in NORM coordinates.
win32.py converts NORM to screen pixels at the boundary using the selected region.

### Key Design Decisions

- exec() code generation, NOT regex parsing. Parser VLM outputs executable Python.
  29/29 success rate vs 1/9 with old regex approach.
- Chess agent says "move FROM TO" so parser receives a natural language request,
  not a bare token. The word "move" is the bridge that prevents parroting.
- Annotation overlays render in browser OffscreenCanvas, round-trip through panel
  via annotate_request.json, SSE notify, browser renders, POST /result.
- Browser-ready gate: brain launch blocked until SSE connects (configurable timeout).
- No safety checks, no fallbacks. exec() the parser output directly.
- All coordinates NORM 1000x1000.
- Shared overlay store enables multi-agent communication through visual annotations.

---

## The Swarm Architecture

### Annotation as Shared Memory

The annotated screenshot IS the shared memory. Agents do not communicate through
message queues or pipes. They communicate by writing proposals as overlays on the
captured screenshot. When agent A proposes a move, it stores an overlay (colored
arrow + label "AGENT A PROPOSES: E2->E4 -- AGREE?"). When agent B captures and
annotates, the panel's overlay store provides all agents' proposals. Agent B's VLM
sees the board + grid + agent A's proposal rendered on the same image.

### Consensus Protocol

There is no arbiter. There is no hierarchy. Every agent is a peer.

Each agent independently:
1. Captures a fresh screenshot
2. Fetches shared overlays from the panel store (other agents' proposals)
3. Annotates the screenshot with grid + last move + all shared proposals
4. Asks its VLM: "Do you agree with a proposal, or do you have a better move?"
5. Parser VLM outputs either propose() or drag()
6. propose() stores a new overlay in the shared store
7. drag() triggers immediate execution -- consensus reached

If the VLM sees a proposal it agrees with, it says "agree FROM TO" and the parser
generates drag(). If it disagrees, it says "move FROM TO" and the parser generates
propose(). The VLM IS the consensus detector. No explicit counting needed.

### Debate Loop

Both agents run in parallel threads. Each round, both capture, think, and act.
If any agent outputs drag() (agreement), the move executes and the debate ends.
If max_debate_rounds is exhausted without consensus, a tiebreaker VLM picks
the best proposal from the shared overlays and executes it.

### VLM Calls Per Cycle

Per debate round: 2 strategy VLMs + 2 parsers = 4 VLM calls.
Typical consensus in 1-3 rounds = 4-12 VLM calls per chess move.
Tiebreaker adds 2 more if needed.

---

## The Single-Agent Brain

brain_chess_players.py implements a simpler two-VLM pipeline:
1. Chess VLM sees annotated screenshot, outputs "move e2 e4"
2. Parser VLM converts to drag('e2', 'e4')
3. exec() runs parser output in sandbox: {"__builtins__": {}, "drag": drag}
4. drag() closure converts squares to NORM coords, calls bu.device()

try/except around exec() -- bad parser output fails the round, not the process.

---

## Panel Server (panel.py)

### Routes

All brain communication goes through POST /route with:
```
{agent: "name", recipients: ["target"], ...payload}
```

Targets:
- win32_capture -- screenshot via win32.py subprocess
- annotate -- overlay rendering via browser round-trip
- vlm -- forward to LM Studio on localhost:1235
- win32_device -- mouse/keyboard actions via win32.py subprocess
- overlay_store -- shared overlay memory (put/get/clear)

### Overlay Store

Thread-safe dict mapping agent names to overlay lists.
- put: agent stores its proposal overlays (replaces previous)
- get: returns flattened list of ALL agents' overlays
- clear: wipes the entire store (called after execution)

### SSE and Browser-Ready Gate

GET /events provides SSE stream. Brain launch is blocked until the browser
connects via SSE. The browser handles annotation rendering via OffscreenCanvas.

### Logging

Every message through panel gets logged to JSONL files in the run directory.
Fields: ts, event, from, to, agent, request_id, label, error, finish_reason,
duration, tokens, image, fields (extra). Images saved as PNG files alongside logs.

### Replay

```
python panel.py --replay runs/TIMESTAMP
```

Opens dashboard with recorded data. Replay controls: play/pause/step/scrub.

---

## Dashboard (panel.html)

### Physics Engine

IC chips float in 2D space with spring-damper physics:
- Each chip has position (x,y), target (tx,ty), velocity (vx,vy)
- Spring pulls position toward target, damping slows velocity
- Dragging uses stiffer spring for responsive following with slight overshoot
- Releasing a chip injects random velocity for oscillation settle effect
- Edge flashes nudge all chips slightly -- the diagram breathes when data flows
- ELK.js computes target positions, physics animates transitions

### Annotation Rendering

When a brain requests annotation, the browser's OffscreenCanvas draws overlays
via drawPoly() and POSTs the result back. Single-point overlays render as
centered text with dark background pill. Multi-point overlays draw lines/polygons.

---

## Running

### Prerequisites

- Windows 11
- Python 3.13+
- Latest Google Chrome
- LM Studio on localhost:1235 with a vision-language model (tested: Qwen 2.5 VL 3B)

### Single Agent

```
python panel.py brain_chess_players.py
```

### Swarm

```
python panel.py brain_chess_swarm.py
```

Both modes: crosshair overlay appears for region selection, then scale calibration,
then browser opens, then brain launches automatically.

---

## Coding Standards

- Python 3.13 only, Windows 11 only, latest Chrome only
- OpenAI-compatible /chat/completions API design
- Maximum code reduction while preserving functionality
- Full typing on all functions and locals
- Frozen dataclasses for all configuration and constants
- No functional magic values
- No duplicate flows
- No dead functionality
- No hidden fallback behavior unless explicitly approved
- No comments in code
- Modern HTML5, CSS, JS only

---

## AI Continuation Prompt

The following prompt provides complete context for any AI to understand, analyze,
modify, or debug the Franz Plumbing project. It can be used with a single file
or all files. It can also be used with JSONL log files for cross-referencing.

```
You are helping build Franz Plumbing -- an autonomous agent platform where VLMs
physically control a Windows 11 PC by looking at screenshots and moving the mouse.
Intelligence lives in VLM calls. Everything else is dumb plumbing.

## Architecture (6 files)

panel.py (~800 lines) -- HTTP server on :1236, sole JSONL logger, subprocess
launcher. Routes POST /route to handlers: win32_capture, annotate, vlm,
win32_device, overlay_store. SSE endpoint /events notifies browser. Browser-ready
gate blocks brain launch until SSE connects. Overlay store: thread-safe
dict[str, list[overlay]] for multi-agent shared memory. Replay mode: --replay.

panel.html (~950 lines) -- PCB-style dashboard with spring-damper physics engine.
ELK.js layout computes targets, physics animates. GSAP wire animations.
OffscreenCanvas renders overlays via drawPoly() and POSTs annotated PNG back to
panel. Single-point overlays render as centered text with dark background pill.
All animation constants named at top of script block.

win32.py (~860 lines) -- Windows-only subprocess. Screen capture (BGRA->PNG via
hand-rolled encoder), mouse (click/drag/double_click/right_click/scroll),
keyboard (type_text/press_key/hotkey), cursor_pos, select_region. All positions
in NORM 1000x1000 coordinate space. Pure ctypes, zero dependencies.

brain_chess_players.py (~200 lines) -- Single-agent chess brain. Two-VLM pipeline:
  1. Chess VLM sees annotated screenshot, outputs "move e2 e4"
  2. Parser VLM converts to drag('e2', 'e4')
  3. exec() runs parser output in sandbox: {"__builtins__": {}, "drag": drag}
  4. drag() closure converts squares to NORM coords, calls bu.device()
  try/except around exec() -- bad parser output fails the round, not the process.

brain_chess_swarm.py (~250 lines) -- Multi-agent swarm brain. Two peer agents
(aggressive, positional) run in parallel threads. Each captures, fetches shared
overlays from panel overlay_store, annotates with grid + last move + all proposals,
asks VLM to propose or agree. Parser outputs propose() or drag(). propose() stores
overlays in panel store. drag() triggers execution. Debate loop up to
max_debate_rounds. Tiebreaker VLM if no consensus. Annotation IS shared memory.

brain_util.py (~190 lines) -- Brain SDK. route(), capture(), annotate(),
vlm_text(), device(), overlay(), make_vlm_request(), store_overlays(),
fetch_shared_overlays(), clear_shared_overlays(). HTTP client to panel on :1236.
NORM=1000, VLM on localhost:1235 (OpenAI-compatible). TimeoutConfig frozen
dataclass holds all timeout defaults.

## Data Flow Patterns

Capture: brain -> bu.capture() -> panel -> subprocess win32.py capture -> PNG
on stdout -> panel saves + returns base64.

Annotate: brain -> bu.annotate(image, overlays) -> panel -> write JSON + SSE
notify -> browser polls logs, sees annotate_request -> fetches JSON + image ->
OffscreenCanvas drawPoly -> POST /result -> panel unblocks waiting thread ->
returns annotated base64.

VLM: brain -> bu.vlm_text(request) -> panel -> HTTP POST to LM Studio :1235 ->
response -> panel logs + returns.

Device: brain -> bu.device(actions) -> panel -> subprocess win32.py action ->
exit code -> panel returns ok/fail.

Overlay Store: brain -> bu.store_overlays(overlays) -> panel stores in
dict[agent] = overlays. brain -> bu.fetch_shared_overlays() -> panel returns
flattened list of all agents' overlays.

## JSONL Log Format

Each line is a JSON object with fields:
- ts: float (unix timestamp)
- event: str (capture_done, vlm_forward, vlm_response, annotate_request,
  annotate_done, device_done, overlay_store_put, overlay_store_get, route, etc.)
- from: str (source component like "brain.chess", "panel", "win32", "vlm", "browser")
- to: str (destination component)
- agent: str (agent name like "chess", "aggressive", "positional", "parser")
- request_id: str (UUID linking request/response pairs)
- label: str (human-readable summary)
- error: bool (true if error event)
- finish_reason: str (VLM finish reason: "stop", "length", "error")
- duration: float (seconds)
- tokens: int (completion tokens)
- image: str (filename in images/ directory)
- fields: dict (extra fields like system_prompt, user_message, vlm_reply, etc.)

To cross-reference: match request_id across events to trace a full round-trip.
Match agent to filter by agent. Match event to filter by type. Images are in
the images/ subdirectory of the run folder, referenced by filename in the
image field.

## Prompts

System prompts are static identity/rules only. Dynamic context goes in user
messages. Chess agent says "move FROM TO". Swarm agents say "move FROM TO" to
propose or "agree FROM TO" to accept. Parser has two functions: propose() and
drag(). The word "move" or "agree" determines which function the parser generates.

## Rules for Modifications

- Only modify brain files unless explicitly told otherwise
- No tests unless explicitly requested
- No safety checks or fallbacks -- dumb plumbing philosophy
- System prompts are static identity/rules only
- Dynamic context goes in user message
- Minimal code only -- no verbose implementations
- Full typing, frozen dataclasses, no magic values, no dead code
- Python 3.13, Windows 11, latest Chrome only
```

### Using the Prompt

1. Paste the prompt above into any AI chat
2. Attach one or more project files as needed
3. The AI will understand the architecture, data flows, and conventions
4. For log analysis: paste JSONL lines and ask the AI to trace request_ids,
   identify errors, measure durations, or reconstruct the sequence of events
5. For modifications: specify which brain behavior to change -- the AI knows
   to only touch brain files unless told otherwise

### Log Analysis Examples

Given a JSONL log, an AI using this prompt can:
- Trace a full chess move: filter by request_id, follow capture -> annotate ->
  vlm_forward -> vlm_response -> device_done
- Identify VLM failures: filter event=vlm_error, check error_body in fields
- Measure round timing: sum durations between capture_done and device_done
  for the same agent
- Debug annotation timeouts: find annotate_timeout events, check if
  sse_disconnect preceded them
- Analyze swarm consensus: filter overlay_store_put events, track how many
  rounds before a drag() was executed
- Cross-reference images: use the image field to identify which screenshot
  was sent to which VLM call

---

## Benchmark Results

### Single Agent (brain_chess_players.py)

| Metric | Value |
|--------|-------|
| Parser produced valid drag() | 29/29 (100%) |
| Device actions succeeded | 25/29 (86%) |
| Error events | 0 |
| Architecture comparison | exec() 29/29 vs regex 1/9 |

### Swarm (brain_chess_swarm.py)

Not yet benchmarked. Expected: 4-12 VLM calls per move depending on consensus
speed. The system is designed to self-adapt through debate -- correctness emerges
from the interaction, not from any single agent being right.

---

## Known Limitations

- Chess VLM (Qwen 2.5 VL 3B) sometimes outputs algebraic notation instead of
  "move FROM TO" format. A stronger VLM or few-shot examples would help.
- Annotation timeout if browser tab is backgrounded or throttled.
- Single brain subprocess launched by panel. Multiple brains require the brain
  itself to manage threads/subprocesses internally (as brain_chess_swarm.py does).
- Image storage grows unboundedly during long runs. No cleanup mechanism.
- The exec() sandbox is minimal (no builtins), not a security boundary.
- Swarm agents may enter infinite disagreement loops. The max_debate_rounds
  tiebreaker prevents deadlock but the forced choice may not be optimal.
```