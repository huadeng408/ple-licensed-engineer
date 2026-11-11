# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tool-calling harness for any OpenAI-compatible model endpoint.

Connects to `MODEL_BASE_URL` using `MODEL_API_KEY` and `MODEL_NAME`. The model
works in the task container through four built-in tools (`bash`, `read_file`,
`write_file`, `edit_file`) plus the task's own MCP tools, and finishes when it
provides a complete answer without calling a tool. That answer becomes the final
message. Calls go to the Chat Completions API, or to the Responses API for
OpenAI's GPT-6 models (`RESPONSES_MODELS`).

Optional environment variables: `MAX_OUTPUT_TOKENS` (default `32000`),
`COMPACT_AT_TOKENS` (default `150000`), and `REASONING_EFFORT` (default `max`
for Claude, GLM, GPT-6 and DeepSeek-V4, `high` for other models).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import posixpath
import re
import sys
import types
from typing import Any

import openai
import taskbridge

MODEL = os.environ.get("MODEL_NAME", "")
# Models without reasoning_effort levels (Gemma, Llama) omit it unless
# REASONING_EFFORT overrides it; Claude, GLM, GPT-6 and DeepSeek-V4 default to
# max, and Gemini / gpt-oss default to high.
NO_EFFORT_MODELS = ("gemma", "llama")
MAX_EFFORT_MODELS = ("claude", "glm", "gpt-6", "deepseek-v4")
EFFORT = os.environ.get("REASONING_EFFORT") or (
    ""
    if any(m in MODEL.lower() for m in NO_EFFORT_MODELS)
    else ("max" if any(m in MODEL.lower() for m in MAX_EFFORT_MODELS) else "high")
)
# OpenAI's GPT-6 models only support function tools combined with
# reasoning_effort on the Responses API. Other models also switch to
# /v1/responses if /v1/chat/completions returns a 400 directing them there.
RESPONSES_MODELS = ("gpt-6",)
RESPONSES_ONLY = re.compile(
    r"/v1/responses|not supported.{0,80} in /v1/chat/completions"
)
# Claude reuses a cached prompt prefix only where a request marks one
# (marked()); other models cache on their own.
CACHE_MARK = "claude" in MODEL.lower()
COMPACT_AT_TOKENS = int(os.environ.get("COMPACT_AT_TOKENS", "150000"))
TOOL_RESULT_CHARS = 30_000
# MCP tools are offered as functions up to this many characters of JSON in all
DIRECT_MCP_CHARS = 40_000
# functions one request may offer (OpenAI's limit), the built-in tools included
MAX_TOOLS = 128
KEEP_RECENT_RESULTS = 12
FINISH_MARGIN_SEC = 180
RETRY_WAITS = (5, 15, 30, 60, 120)
INCUMBENT_DIR = "/tmp/_ple_incumbent_best"
MAX_SCHEMA_BYTES = 67_108_864  # 64 MiB full-file verification ceiling
MAX_VISUAL_EDGE = 1568
VISUAL_TOKEN_ESTIMATE = 1200

# How endpoints word an error saying the conversation no longer fits (xAI's:
# "maximum prompt length is N but the request contains M tokens"). OpenAI's
# error for too many tools ("'tools': array too long") does not match.
CONTEXT_FULL = re.compile(
    r"context.?(length|window|size|limit)|maximum context|too many"
    r" tokens|prompt is too long|maximum prompt length"
    r"|(input|prompt|request|messages?)\b[^.]{0,40}(too long|exceed)"
    r"|exceed[^.]{0,40}(token|context)",
    re.I,
)
# The context window and the prompt's share of it, as vLLM, OpenAI, Anthropic,
# Gemini and xAI word them in such an error.
WINDOW_RE = re.compile(
    r"maximum context length (?:is |of )?(\d+)"
    r"|context (?:window|length|size)(?: limit)?(?: is| of)? (\d+)"
    r"|tokens? > (\d+) maximum|maximum number of tokens allowed \((\d+)\)"
    r"|maximum prompt length is (\d+)",
    re.I,
)
PROMPT_RE = re.compile(
    r"(\d+) input tokens|(\d+) in the messages|prompt is too long: (\d+)"
    r"|input token count \((\d+)\)|request contains (\d+) tokens",
    re.I,
)
MIN_OUTPUT = 1024  # the fewest output tokens to ask for
# characters an unread tool result keeps, however full the context
UNREAD_KEEP = 600
# tokens left free in the window, since tokenizers count a little differently
CONTEXT_MARGIN = 1024
# How endpoints word an error refusing a parameter or value outright, unlike one
# that names a smaller maximum.
UNSUPPORTED = re.compile(
    r"unsupported|unrecogni[sz]ed|unknown|not supported|does not support|not"
    r" (allowed|permitted)"
    r"|extra (inputs|fields)|unexpected",
    re.I,
)
THINKING_SIG_ERR = re.compile(
    r"thinking[._]?(?:signature|blocks)|invalid.{0,30}signature"
    r"|reasoning_content|redacted_thinking",
    re.I,
)
VISION_ERR = re.compile(
    r"image_url|input_image|does not support.{0,30}(?:image|vision|multimodal)",
    re.I,
)
MUTATING_MCP_RE = re.compile(
    r"(?:^|_)(?:create|write|update|edit|modify|patch|put|post|set|add|insert"
    r"|append|upload|save|commit|push|send|reply|forward|draft|delete|remove"
    r"|drop|clear|reset|close|reopen|approve|reject|submit|issue|assign|move"
    r"|rename|archive|execute|run)(?:_|$)",
    re.I,
)

# The standard fields of a streamed tool call, of its function, and of a reply's
# delta. Any other field an endpoint sends goes back with the message: Gemini
# refuses a tool call without its thought signature, which comes in the call's
# extra_content.
CALL_FIELDS = {"index", "id", "type", "function"}
FUNCTION_FIELDS = {"name", "arguments"}
DELTA_FIELDS = {
    "role",
    "content",
    "tool_calls",
    "function_call",
    "refusal",
    "audio",
    "annotations",
    "reasoning_content",
    "reasoning",
}

client = openai.AsyncOpenAI(
    base_url=os.environ.get("MODEL_BASE_URL"),
    api_key=os.environ.get("MODEL_API_KEY") or "none",
    max_retries=0,
    timeout=900,
)

SYSTEM = """You are a Licensed Professional Engineer (PE) solving a physical-engineering task inside an isolated Linux \
container. Your lean engineering tool surface acts inside it: bash (starts in {workdir}), read_file, write_file, \
edit_file, pin_basis_of_design, inspect_visual_artifact, checkpoint_incumbent, verify_deliverable_schema, and \
verify_calculations. The container has no internet access.{mcp}

PLE tasks are graded by automated checks, an LLM rubric judge, or constraint-objective verifiers under:
    s = 0.4 * p + 0.6 * G * O'
where p is the fraction of general constraints and explicit Requirement-Clause Grounding credits met, G in {{0, 1}} is \
the strict Hard-Constraint Gate (G=1 ONLY if 100% of safety, code-compliance, spatial clash-free, and schema \
constraints hold; breaking even one hard constraint forces G=0 and caps the score at 0.40), and O' in [0, 1] is the \
Normalized Objective Credit relative to the naive bound O_naive and certified optimum (a naive feasible design earns \
O'=0).

Follow this mandatory 4-Phase Licensed-Engineer Workflow:
1. PHASE 1 (Recon, Visual Plan Inspection & Compaction-Immune Basis of Design):
   - Inspect {workdir}, reference drawings/specs/spreadsheets, and MCP servers (documents, spreadsheets, email, CAD).
   - Whenever reference PDFs, DXF CAD drawings, or PNG schematics exist, call `inspect_visual_artifact` to visually \
read dimensions, clearances, callouts, and spatial geometry before modeling.
   - Call `pin_basis_of_design` to lock in: (a) every required deliverable path and format (.ifc, .dxf, .xlsx, .docx, \
.pdf, .zip, .py, .json, .md), (b) all Hard Constraints (G=1 safety factors, code limits, 3D clearances), (c) the \
explicit Requirement-Clause Grounding Register (mapping each requirement to its exact statutory clause and reference \
table value), (d) the parametric O' optimization strategy, and (e) SI vs. US Customary/Imperial unit invariants.
2. PHASE 2 (Feasible G=1 Baseline & Physical Incumbent Checkpoint):
   - Never compute engineering numbers by hand. Use `verify_calculations` or `bash` with full Python (`numpy`, \
`scipy`, `sympy`, `pandas`, `ifcopenshell`, `ezdxf`, `openpyxl`, `docx`, `reportlab`) to solve governing equations and \
verify unit conversions.
   - Synthesize a 100% compliant baseline deliverable satisfying G == 1 at the exact required path(s), validate it \
with `verify_deliverable_schema` (and visually inspect generated `.pdf`/`.dxf` outputs with `inspect_visual_artifact`), \
and lock a physical backup in `/tmp/_ple_incumbent_best/` via `checkpoint_incumbent`.
3. PHASE 3 (Parametric O' > O_naive Optimization Sweep):
   - Do NOT stop at naive over-design (which scores O' = 0). Write a parametric Python optimization script \
(`scipy.optimize.milp` / `linprog` / `minimize` or discrete catalog sweep over >= 3 feasible candidate designs) \
enforcing every G=1 constraint with an explicit safety margin (e.g., DCR <= 0.995).
   - Overwrite deliverables ONLY when the optimized candidate improves O' while maintaining G == 1 and passes \
`verify_deliverable_schema`, then update `checkpoint_incumbent`.
4. PHASE 4 (Schema, MCP World State & Traceability Sign-Off):
   - Confirm every deliverable, inner `.zip` member, and required MCP side effect is complete, free of NaN/Inf/TODO \
placeholders, and cites exact statutory/reference clauses.
   - You have about {minutes} minutes in total; the time left is shown after tool outputs.

When all deliverables are verified on disk, answer without calling a tool: provide a structured engineering summary \
listing each deliverable's exact path, key computed values with units, the Requirement-Clause Grounding Register, \
and confirmation that every hard constraint (G=1) and optimization target (O') is met."""

VERIFIER_SYSTEM = """You are an Independent Licensed-Engineer Peer Reviewer auditing deliverables in a Linux container \
(bash starts in {workdir}). Do NOT create, modify, or delete any deliverable file or mutate MCP state.
CRITICAL ADVERSARIAL RULE: Treat the worker's proposed summary as UNTRUSTED CLAIMS. Workers frequently claim full \
compliance while the actual files or MCP state on disk harbor G=0 code violations, unit conversion errors, 0-byte or \
wrong-path archive members, spatial clashes, or unsubmitted MCP tickets/emails.
Audit the actual container state directly against the task instructions and Pinned Basis of Design:
1. Verify every required deliverable file exists at the exact path asked for, is non-empty, and passes full-file \
structural checks (.zip inner members, .ifc entities, .dxf ENTITIES, .xlsx/.docx OpenXML, .pdf header, .json/.csv) \
using `verify_deliverable_schema` and `inspect_visual_artifact` (when visual drawings/PDFs are relevant).
2. Independently recompute and check governing engineering inequalities, unit conversions (SI vs Imperial, e.g. \
kips<->kN, ft<->m, TR<->kW_th), safety factors, spatial clearances (`clash_count == 0`), and Hard-Constraint Gate \
(G = 1) limits from the actual deliverable files on disk using `verify_calculations`, `read_file`, or read-only `bash`.
3. When read-only MCP tools (`mcp_find`, `mcp_call`, `<server>__<tool>`) or `/app/tools/.apps_data` are present, \
inspect the live MCP/application state directly to confirm required tickets, dispositions, or sent emails actually \
exist and match the task requirements.
4. If and only if every deliverable and MCP requirement is verified directly from container files/state and meets all \
hard constraints (G=1), reply with the single word PASS and nothing else.
5. Otherwise, reply with a concise numbered list of concrete defects that must be fixed."""

MCP_DIRECT = """

The task also offers its own tools (the functions named <server>__<tool>)."""

MCP_CATALOG = """

The task also offers its own tools, listed below by server. Call mcp_find to see a tool's parameters, then mcp_call \
to run it.
{catalog}"""

SUMMARY_PROMPT = """The conversation is getting long, so the earlier turns will be removed. Write detailed engineering \
notes that let you continue without losing any precision: exact deliverable paths and formats, all Hard-Constraint Gate \
(G=1) inequalities and statutory code clause values, SI/Imperial unit conversions, every file or MCP state created or \
modified (with its current status and incumbent checkpoint status), all verified calculation values and O' optimization \
results so far, and remaining steps. Be exact with numbers and units."""


def tool(
    name: str, description: str, required: list[str], **properties: Any
) -> dict[str, Any]:
  """Builds an OpenAI function-tool definition dictionary."""
  return {
      "type": "function",
      "function": {
          "name": name,
          "description": description,
          "parameters": {
              "type": "object",
              "properties": properties,
              "required": required,
          },
      },
  }


TOOLS = [
    tool(
        "bash",
        "Run a bash command in the task container and return its exit code and"
        " output. Each call starts a new shell in the working directory: only"
        " files persist between calls.",
        ["command"],
        command={"type": "string"},
        timeout_sec={
            "type": "integer",
            "description": (
                "Stop the command after this many seconds (default 120)."
            ),
        },
    ),
    tool(
        "read_file",
        "Read a text file from the task container.",
        ["path"],
        path={"type": "string"},
    ),
    tool(
        "write_file",
        "Create or overwrite a file in the task container; parent directories"
        " are created.",
        ["path", "content"],
        path={"type": "string"},
        content={"type": "string"},
        append={
            "type": "boolean",
            "description": (
                "Add to the end of the file instead (write a large file in"
                " parts)."
            ),
        },
    ),
    tool(
        "edit_file",
        "Replace exact text in a file. old_text must occur exactly once unless"
        " replace_all is true.",
        ["path", "old_text", "new_text"],
        path={"type": "string"},
        old_text={"type": "string"},
        new_text={"type": "string"},
        replace_all={"type": "boolean"},
    ),
    tool(
        "pin_basis_of_design",
        "Record and pin the Phase 1 Basis of Design (BoD) Register so that"
        " required deliverable paths, Hard-Constraint Gate (G=1) limits,"
        " Requirement-Clause Grounding Register citations, Normalized Objective"
        " Credit (O') optimization targets, and SI/Imperial unit invariants"
        " remain permanently anchored across context compactions.",
        ["deliverables", "hard_constraints_g"],
        deliverables={
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact deliverable file paths and required formats.",
        },
        hard_constraints_g={
            "type": "array",
            "items": {"type": "string"},
            "description": "Hard-Constraint Gate (G=1) inequalities and safety limits.",
        },
        clause_grounding_register={
            "type": "array",
            "items": {"type": "string"},
            "description": "Statutory code clauses and reference document values.",
        },
        optimization_plan_o_prime={
            "type": "string",
            "description": "Parametric solver strategy to optimize O' beyond naive feasibility O_naive.",
        },
        unit_invariants={
            "type": "array",
            "items": {"type": "string"},
            "description": "Canonical SI/Imperial unit conversion invariants.",
        },
    ),
    tool(
        "inspect_visual_artifact",
        "Render a visual engineering artifact (.pdf page, .dxf CAD drawing, or"
        " .png/.jpg/.webp image) from the task container into a high-resolution"
        " image attached to the conversation for visual plan/layout inspection.",
        ["path"],
        path={
            "type": "string",
            "description": "Container path to the .pdf, .dxf, .png, .jpg, or .webp file.",
        },
        page={
            "type": "integer",
            "description": "1-based page number to render for PDF documents (default 1).",
        },
        dpi={
            "type": "integer",
            "description": "Target rendering DPI for PDF pages (default 150).",
        },
    ),
    tool(
        "checkpoint_incumbent",
        "Verify that every listed deliverable file passes schema validation and"
        " save a physical G=1 backup snapshot into `/tmp/_ple_incumbent_best/`"
        " so aggressive O' optimization edits automatically roll back if a"
        " late edit corrupts or deletes a deliverable.",
        ["paths"],
        paths={
            "type": "array",
            "items": {"type": "string"},
            "description": "Deliverable file paths to verify and snapshot.",
        },
        objective_value={
            "type": "number",
            "description": "Optional objective metric value of this feasible G=1 candidate.",
        },
        note={
            "type": "string",
            "description": "Brief engineering note describing this feasible checkpoint.",
        },
    ),
    tool(
        "verify_deliverable_schema",
        "Perform full-file (up to 64 MiB) structural and schema verification on"
        " a deliverable file (.zip, .ifc, .dxf, .xlsx, .docx, .pdf, .json,"
        " .csv, .md, .py) inside the container, including recursive inspection"
        " of submission.zip members, quote-safe IFC #id entity references, DXF"
        " ENTITIES sections, OpenXML worksheets, and NaN/Inf checks.",
        ["file_path"],
        file_path={
            "type": "string",
            "description": "Container path to the deliverable file or archive.",
        },
        expected_format={
            "type": "string",
            "description": "Expected extension or format (e.g. 'zip', 'ifc', 'dxf', 'xlsx', 'docx', 'json', 'csv', 'pdf').",
        },
        required_keys_or_columns={
            "type": "array",
            "items": {"type": "string"},
            "description": "Required JSON keys, CSV columns, or archive member filenames.",
        },
        check_zip_members={
            "type": "boolean",
            "description": "Whether to recursively validate files inside a .zip archive (default true).",
        },
    ),
    tool(
        "verify_calculations",
        "Run a standalone Python 3 engineering verification or parametric"
        " optimization script (using numpy, scipy, sympy, pandas, openpyxl,"
        " ifcopenshell, ezdxf, etc.) in the task container and return its"
        " stdout/stderr and exit code.",
        ["script"],
        script={
            "type": "string",
            "description": "Complete Python 3 script source code to execute.",
        },
        summary={
            "type": "string",
            "description": "Brief summary of the equations, sweep, or constraints checked.",
        },
    ),
]
MCP_TOOLS = [
    tool(
        "mcp_find",
        "Show the description and parameters of the task's own tools that match"
        " a query.",
        ["query"],
        query={
            "type": "string",
            "description": "A tool name, or words describing what you need.",
        },
    ),
    tool(
        "mcp_call",
        "Run one of the task's own tools.",
        ["tool", "arguments"],
        tool={"type": "string", "description": "The tool's name as listed."},
        arguments={"type": "object"},
    ),
]


def is_readonly_mcp_tool(
    tool_name: str, spec: dict[str, Any] | None = None
) -> bool:
  """Returns whether an MCP tool is safe for read-only Phase 4 verification."""
  annotations = (spec or {}).get("annotations")
  if isinstance(annotations, dict):
    if annotations.get("destructiveHint") is True:
      return False
    if annotations.get("readOnlyHint") is True and not MUTATING_MCP_RE.search(
        tool_name.split("__", 1)[-1]
    ):
      return True
  unprefixed = tool_name.split("__", 1)[-1]
  if MUTATING_MCP_RE.search(unprefixed):
    return False
  return True


def _render_dxf_bytes_to_png(raw_bytes: bytes) -> tuple[bytes, int, int]:
  """Renders ASCII DXF entities (`LINE`, `LWPOLYLINE`, `CIRCLE`, `ARC`, `TEXT`) to PNG."""
  from PIL import Image, ImageDraw  # pylint: disable=g-import-not-at-top

  text = raw_bytes.decode("utf-8", "replace")
  raw_lines = [ln.strip() for ln in text.splitlines()]
  pairs: list[tuple[int, str]] = []
  idx = 0
  while idx + 1 < len(raw_lines):
    try:
      code = int(raw_lines[idx])
      pairs.append((code, raw_lines[idx + 1]))
      idx += 2
    except ValueError:
      idx += 1

  in_entities = False
  entities: list[dict[str, Any]] = []
  cur: dict[str, Any] | None = None
  i = 0
  while i < len(pairs):
    code, val = pairs[i]
    uval = val.upper()
    if code == 0 and uval == "SECTION" and i + 1 < len(pairs):
      if pairs[i + 1][0] == 2 and pairs[i + 1][1].upper() == "ENTITIES":
        in_entities = True
        i += 2
        continue
    if in_entities and code == 0 and uval == "ENDSEC":
      if cur:
        entities.append(cur)
        cur = None
      break
    if in_entities:
      if code == 0:
        if cur:
          entities.append(cur)
        cur = {"type": uval, "pts": [], "props": {}}
      elif cur is not None:
        if cur["type"] == "LWPOLYLINE" and code == 10:
          cur["pts"].append([float(val), 0.0])
        elif cur["type"] == "LWPOLYLINE" and code == 20 and cur["pts"]:
          cur["pts"][-1][1] = float(val)
        elif code in (10, 11, 20, 21, 40, 50, 51):
          try:
            cur["props"][code] = float(val)
          except ValueError:
            pass
        elif code in (1, 8, 70):
          cur["props"][code] = val
    i += 1
  if cur:
    entities.append(cur)

  xs: list[float] = []
  ys: list[float] = []
  for ent in entities:
    p = ent["props"]
    et = ent["type"]
    if et == "LINE" and 10 in p and 20 in p and 11 in p and 21 in p:
      xs.extend([p[10], p[11]])
      ys.extend([p[20], p[21]])
    elif et == "LWPOLYLINE":
      for px, py in ent["pts"]:
        xs.append(px)
        ys.append(py)
    elif et in ("CIRCLE", "ARC") and 10 in p and 20 in p:
      r = abs(float(p.get(40, 1.0)))
      xs.extend([p[10] - r, p[10] + r])
      ys.extend([p[20] - r, p[20] + r])
    elif et in ("TEXT", "MTEXT", "INSERT") and 10 in p and 20 in p:
      xs.append(p[10])
      ys.append(p[20])

  w, h = 1280, 960
  pad = 72
  img = Image.new("RGB", (w, h), (248, 250, 252))
  draw = ImageDraw.Draw(img)
  draw.rectangle([16, 16, w - 16, h - 16], outline=(148, 163, 184), width=2)

  if not xs or not ys:
    draw.text(
        (40, 40),
        f"DXF CAD Preview ({len(entities)} entities parsed; no 2D bounds)",
        fill=(15, 23, 42),
    )
  else:
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    dx = max(xmax - xmin, 1e-6)
    dy = max(ymax - ymin, 1e-6)
    scale = min((w - 2 * pad) / dx, (h - 2 * pad) / dy)

    def to_px(x_val: float, y_val: float) -> tuple[float, float]:
      px = pad + (x_val - xmin) * scale
      py = h - pad - (y_val - ymin) * scale
      return (px, py)

    palette = [
        (30, 64, 175),
        (15, 118, 110),
        (180, 83, 9),
        (190, 18, 60),
        (109, 40, 217),
    ]
    layers: dict[str, tuple[int, int, int]] = {}
    for ent in entities:
      p = ent["props"]
      et = ent["type"]
      layer = str(p.get(8, "0"))
      if layer not in layers:
        layers[layer] = palette[len(layers) % len(palette)]
      color = layers[layer]
      if et == "LINE" and 10 in p and 20 in p and 11 in p and 21 in p:
        draw.line([to_px(p[10], p[20]), to_px(p[11], p[21])], fill=color, width=2)
      elif et == "LWPOLYLINE" and len(ent["pts"]) >= 2:
        pts_px = [to_px(px, py) for px, py in ent["pts"]]
        closed = str(p.get(70, "0")).strip() in ("1", "129")
        if closed:
          pts_px.append(pts_px[0])
        draw.line(pts_px, fill=color, width=2)
      elif et == "CIRCLE" and 10 in p and 20 in p and 40 in p:
        cx, cy = to_px(p[10], p[20])
        rad = max(1.0, abs(p[40]) * scale)
        draw.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], outline=color, width=2)
      elif et == "ARC" and 10 in p and 20 in p and 40 in p:
        cx, cy = to_px(p[10], p[20])
        rad = max(1.0, abs(p[40]) * scale)
        a0 = float(p.get(50, 0.0))
        a1 = float(p.get(51, 360.0))
        pts_arc = []
        span = (a1 - a0) % 360.0 or 360.0
        for step_k in range(25):
          ang = math.radians(a0 + span * step_k / 24.0)
          pts_arc.append(
              to_px(p[10] + abs(p[40]) * math.cos(ang), p[20] + abs(p[40]) * math.sin(ang))
          )
        draw.line(pts_arc, fill=color, width=2)
      elif et in ("TEXT", "MTEXT") and 10 in p and 20 in p:
        tx, ty = to_px(p[10], p[20])
        label = str(p.get(1, ""))[:48]
        if label:
          draw.text((tx + 2, ty - 10), label, fill=(15, 23, 42))

    draw.text(
        (28, 24),
        (
            f"DXF CAD Layout | Entities={len(entities)} | Layers={list(layers)[:6]} | "
            f"Bounds X=[{xmin:.2f}, {xmax:.2f}], Y=[{ymin:.2f}, {ymax:.2f}]"
        ),
        fill=(15, 23, 42),
    )

  out_buf = io.BytesIO()
  img.save(out_buf, format="PNG", optimize=True)
  return out_buf.getvalue(), w, h


def render_visual_bytes_to_png(
    raw_bytes: bytes, ext: str, page: int = 1, dpi: int = 150
) -> tuple[bytes, int, int, dict[str, Any]]:
  """Renders image, PDF page, or DXF bytes into bounded PNG bytes (`<= 1568 px`)."""
  from PIL import Image  # pylint: disable=g-import-not-at-top

  ext_low = ext.lower().lstrip(".")
  meta: dict[str, Any] = {"format": ext_low}

  if ext_low == "dxf":
    png_b, w, h = _render_dxf_bytes_to_png(raw_bytes)
    meta["renderer"] = "pil_dxf_rasterizer"
    return png_b, w, h, meta

  if ext_low == "pdf":
    import pypdfium2 as pdfium  # pylint: disable=g-import-not-at-top

    doc = pdfium.PdfDocument(raw_bytes)
    n_pages = len(doc)
    if n_pages <= 0:
      raise ValueError("PDF document contains 0 pages")
    page_idx = min(max(1, int(page)), n_pages) - 1
    scale = max(72, min(int(dpi), 300)) / 72.0
    pil_img = doc[page_idx].render(scale=scale).to_pil().convert("RGB")
    doc.close()
    meta["page_rendered"] = page_idx + 1
    meta["total_pages"] = n_pages
    meta["renderer"] = "pypdfium2"
  else:
    pil_img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    meta["renderer"] = "pillow"

  w, h = pil_img.size
  if max(w, h) > MAX_VISUAL_EDGE:
    ratio = MAX_VISUAL_EDGE / float(max(w, h))
    w = max(1, int(round(w * ratio)))
    h = max(1, int(round(h * ratio)))
    pil_img = pil_img.resize((w, h), Image.Resampling.LANCZOS)

  out_buf = io.BytesIO()
  pil_img.save(out_buf, format="PNG", optimize=True)
  return out_buf.getvalue(), w, h, meta


def clip(text: str, limit: int = TOOL_RESULT_CHARS) -> str:
  """Truncates `text` in the middle if it exceeds `limit` characters."""
  if len(text) <= limit:
    return text
  head = limit * 2 // 3
  return (
      f"{text[:head]}\n[... {len(text) - limit} characters cut"
      f" ...]\n{text[-(limit - head):]}"
  )


def parse_arguments(call: dict[str, Any]) -> dict[str, Any] | str:
  """Parses a tool call's arguments into a dict or returns an error string."""

  def reject(constant):
    raise ValueError(f"{constant} is not valid JSON")

  try:
    args = json.loads(call["arguments"] or "{}", parse_constant=reject)
    if not isinstance(args, dict):
      raise ValueError("the arguments must be a JSON object")
    call["arguments"] = (
        json.dumps(args, ensure_ascii=False)
        .encode("utf-8", "replace")
        .decode("utf-8")
    )
    return args
  except ValueError as exc:
    call["arguments"] = "{}"
    return (
        f"error: the arguments are not a valid JSON object ({exc}); send the"
        " call again"
    )


def unique_name(name: str, taken: Any) -> str:
  """Truncates `name` to 64 chars and appends `_2`, `_3`, ... if in `taken`."""
  candidate, n = name[:64], 1
  while candidate in taken:
    n += 1
    candidate = f"{name[:63 - len(str(n))]}_{n}"
  return candidate


def visible(text: str) -> str:
  """Strips `<think>...</think>` or `<thinking>...</thinking>` reasoning blocks from `text`."""
  return re.sub(
      r"<think(?:ing)?>.*?(</think(?:ing)?>|$)", "", text or "", flags=re.S
  ).strip()


def extra_fields(part: Any, known: set[str]) -> dict[str, Any]:
  """Extracts non-null extra fields from a streamed SDK object `part`."""
  fields = getattr(part, "model_extra", None) or {}
  return {k: v for k, v in fields.items() if k not in known and v is not None}


def merged(old: Any, new: Any, key: str = "") -> Any:
  """Merges a single delta's extra field value `new` into `old`."""
  if key == "thinking_blocks" and isinstance(new, list):
    return thinking_blocks(old if isinstance(old, list) else [], new)
  if isinstance(old, dict) and isinstance(new, dict):
    return {**old, **{k: merged(old.get(k), v, k) for k, v in new.items()}}
  if isinstance(old, list) and isinstance(new, list):
    return old + new
  return new


def thinking_blocks(
    blocks: list[dict[str, Any]], new: list[Any]
) -> list[dict[str, Any]]:
  """Accumulates streamed Claude `thinking_blocks` deltas."""
  out = [dict(b) for b in blocks]
  for b in new:
    if not isinstance(b, dict):
      continue
    last = out[-1] if out else None
    if (
        last is None
        or last.get("signature")
        or last.get("data")
        or b.get("data")
        or last.get("type") != b.get("type")
    ):
      out.append(dict(b))
      continue
    last["thinking"] = (last.get("thinking") or "") + (b.get("thinking") or "")
    if b.get("signature"):
      last["signature"] = b["signature"]
  return out


def signed(block: Any) -> bool:
  """Returns whether `block` has a `signature` or redacted `data` field."""
  return isinstance(block, dict) and bool(
      block.get("signature") or block.get("data")
  )


def drop_thinking(
    message: dict[str, Any], unless: Any = lambda block: False
) -> dict[str, Any]:
  """Filters `thinking_blocks` in `message`, keeping blocks matching `unless`."""
  if isinstance(message.get("thinking_blocks"), list):
    message["thinking_blocks"] = [
        b for b in message["thinking_blocks"] if unless(b)
    ]
  fields = message.get("provider_specific_fields")
  if isinstance(fields, dict) and isinstance(
      fields.get("thinking_blocks"), list
  ):
    message["provider_specific_fields"] = {
        **fields,
        "thinking_blocks": [b for b in fields["thinking_blocks"] if unless(b)],
    }
  return message


def _strip_internal_keys(message: dict[str, Any]) -> dict[str, Any]:
  """Returns a shallow copy of `message` with internal `_`-prefixed keys removed."""
  if not any(k.startswith("_") for k in message):
    return message
  return {k: v for k, v in message.items() if not k.startswith("_")}


def marked(
    messages: list[dict[str, Any]], archive_idx: int | None = None
) -> list[dict[str, Any]]:
  """Returns wire-safe Chat Completions messages with optional Claude cache marks."""
  clean = [_strip_internal_keys(m) for m in messages]
  if not CACHE_MARK or not clean:
    return clean
  out = list(clean)
  targets = {0, len(out) - 1}
  if archive_idx is not None and 0 < archive_idx < len(out):
    targets.add(archive_idx)
  for raw_idx in sorted(targets):
    idx = raw_idx
    while idx > 0 and not out[idx].get("content"):
      idx -= 1
    m = out[idx]
    content = m.get("content")
    if isinstance(content, str) and content:
      out[idx] = {
          **m,
          "content": [{
              "type": "text",
              "text": content,
              "cache_control": {"type": "ephemeral"},
          }],
      }
    elif isinstance(content, list) and content:
      blocks = [dict(b) if isinstance(b, dict) else b for b in content]
      for b_idx in range(len(blocks) - 1, -1, -1):
        if isinstance(blocks[b_idx], dict) and blocks[b_idx].get("type") == "text":
          blocks[b_idx] = {
              **blocks[b_idx],
              "cache_control": {"type": "ephemeral"},
          }
          break
      out[idx] = {**m, "content": blocks}
  return out


def _convert_content_for_responses(content: Any) -> Any:
  """Converts Chat Completions multimodal content blocks to Responses API format."""
  if not isinstance(content, list):
    return content
  converted = []
  for part in content:
    if not isinstance(part, dict):
      continue
    ptype = part.get("type")
    if ptype == "text":
      converted.append({"type": "input_text", "text": str(part.get("text", ""))})
    elif ptype == "image_url":
      img_val = part.get("image_url")
      url = img_val.get("url", "") if isinstance(img_val, dict) else str(img_val or "")
      if url:
        converted.append({"type": "input_image", "image_url": url})
    elif ptype in ("input_text", "input_image"):
      converted.append(dict(part))
  return converted


def response_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Converts Chat Completions messages into Responses API input items."""
  items = []
  for raw_m in messages:
    m = _strip_internal_keys(raw_m)
    if m["role"] == "tool":
      items.append({
          "type": "function_call_output",
          "call_id": m["tool_call_id"],
          "output": m["content"],
      })
    elif m["role"] != "assistant":
      items.append({
          "role": m["role"],
          "content": _convert_content_for_responses(m["content"]),
      })
    elif "output_items" in m:
      calls = iter(m.get("tool_calls") or [])
      for item in m["output_items"]:
        call = None
        if item.get("type") == "function_call":
          call = next(calls, None)
        if call:
          item = {
              **item,
              "call_id": call["id"],
              "name": call["function"]["name"],
              "arguments": call["function"]["arguments"],
          }
        items.append(item)
    else:
      if m.get("content"):
        items.append({"role": "assistant", "content": m["content"]})
      items += [
          {
              "type": "function_call",
              "call_id": c["id"],
              "name": c["function"]["name"],
              "arguments": c["function"]["arguments"],
          }
          for c in m.get("tool_calls") or []
      ]
  return items


def chat_usage(usage: Any) -> Any:
  """Returns Responses API `usage` in the shape `record` and `report` read.

  Args:
    usage: A Responses API usage object, or None.

  Returns:
    An object with `prompt_tokens`, `completion_tokens` and
    `prompt_tokens_details.cached_tokens`, as Chat Completions reports them, or
    None.
  """
  if usage is None:
    return None
  details = getattr(usage, "input_tokens_details", None)
  return types.SimpleNamespace(
      prompt_tokens=usage.input_tokens or 0,
      completion_tokens=usage.output_tokens or 0,
      prompt_tokens_details=types.SimpleNamespace(
          cached_tokens=getattr(details, "cached_tokens", 0) or 0
      ),
  )


class MCPServer(taskbridge.MCPClient):
  """Client wrapper for a single task MCP server and its discovered tools."""

  def __init__(self, session: taskbridge.Session, spec: dict[str, Any]):
    super().__init__(session, spec["url"], client="custom-harness")
    self.name = spec["name"]
    self.tools: list[dict[str, Any]] = []

  async def start(self) -> None:
    """Initializes the MCP session and fetches the server's tool list."""
    await self.initialize()
    self.tools = await self.list_tools()

  async def call(self, name: str, arguments: dict[str, Any]) -> str:
    """Calls tool `name` on the MCP server and formats the result as text.

    Args:
      name: Unprefixed MCP tool name on this server.
      arguments: Tool input arguments dictionary.

    Returns:
      Formatted text output (prefixed with `error: ` if `isError` is set).
    """
    result = await self.call_tool(name, arguments)
    parts = []
    content = (
        result.get("content") if isinstance(result.get("content"), list) else []
    )
    for item in content:
      kind = item.get("type") if isinstance(item, dict) else None
      if kind == "text":
        parts.append(str(item.get("text", "")))
      elif kind == "resource":
        parts.append(json.dumps(item.get("resource"))[:TOOL_RESULT_CHARS])
      elif kind == "resource_link":  # its uri, name and mimeType
        parts.append(json.dumps(item)[:TOOL_RESULT_CHARS])
      else:
        parts.append(f"[{kind or 'unknown'} content not shown]")
    if not parts and result.get("structuredContent") is not None:
      parts.append(json.dumps(result["structuredContent"]))
    text = "\n".join(parts) or "(no content)"
    return f"error: {text}" if result.get("isError") else text


class Reply:
  """Accumulates a streamed chat-completion response and its tool calls."""

  def __init__(self):
    self.text, self.reasoning, self.calls, self.usage, self.finish = (
        "",
        "",
        {},
        None,
        None,
    )
    self.extra = {}  # fields beyond DELTA_FIELDS, sent back with the message
    self._by_index, self._by_id, self._last = {}, {}, None

  def add_call(self, part: Any) -> None:
    """Adds one streamed tool-call delta to the accumulated calls.

    Groups deltas by `index`, or by `id` when an endpoint omits or reuses
    `index`. Deltas with neither continue the most recent call unless
    `_another_call` detects a new call boundary. Preserves provider-specific
    extra fields on both the call and its `function`.

    Args:
      part: Streamed tool-call delta object from a completion chunk.
    """
    index, call_id = (
        getattr(part, "index", None),
        getattr(part, "id", None) or "",
    )
    function = getattr(part, "function", None)
    name = (getattr(function, "name", None) or "") if function else ""
    key = self._by_index.get(index) if index is not None else None
    if (
        key is not None
        and call_id
        and self.calls[key]["id"] not in ("", call_id)
    ):
      key = None
    if (
        key is not None
        and not call_id
        and name
        and self._another_call(self.calls[key], name)
    ):
      key = None
    if key is None and call_id:
      key = self._by_id.get(call_id)
    if (
        key is None
        and index is None
        and not call_id
        and self._last is not None
        and not (name and self._another_call(self.calls[self._last], name))
    ):
      key = self._last
    if key is None:
      key = len(self.calls)
      self.calls[key] = {
          "id": "",
          "name": "",
          "arguments": "",
          "extra": {},
          "function_extra": {},
      }
    if index is not None:
      self._by_index[index] = key
    if call_id:
      self._by_id[call_id] = key
    call = self.calls[key]
    call["id"] = call["id"] or call_id
    call["name"] = call["name"] or name
    call["arguments"] += (
        (getattr(function, "arguments", None) or "") if function else ""
    )
    call["extra"] = merged(call["extra"], extra_fields(part, CALL_FIELDS))
    if function:
      call["function_extra"] = merged(
          call["function_extra"], extra_fields(function, FUNCTION_FIELDS)
      )
    self._last = key

  @staticmethod
  def _another_call(call: dict[str, Any], name: str) -> bool:
    """Checks whether a nameless-ID delta for `name` starts a new tool call.

    Returns `True` when `call` already has a function name and either `name`
    differs or `call["arguments"]` is already a complete JSON object.

    Args:
      call: Currently accumulating tool-call dictionary.
      name: Function name on the incoming delta.

    Returns:
      `True` if the delta begins a separate tool call, else `False`.
    """
    if not call["name"]:
      return False
    if name != call["name"]:
      return True
    try:
      return isinstance(json.loads(call["arguments"]), dict)
    except ValueError:
      return False

  def message(self) -> dict[str, Any]:
    """Builds the assistant message dict for the conversation history.

    Preserves provider-specific fields while stripping unsigned thinking blocks
    and truncated/unsigned `reasoning_content` that Claude or strict proxies
    reject with HTTP 400.

    Returns:
      The formatted assistant message dictionary.
    """
    had_thinking_blocks = isinstance(self.extra.get("thinking_blocks"), list) or (
        isinstance(self.extra.get("provider_specific_fields"), dict)
        and isinstance(
            self.extra["provider_specific_fields"].get("thinking_blocks"), list
        )
    )
    message = {**self.extra, "role": "assistant", "content": self.text or None}
    if self.calls:
      message["tool_calls"] = [
          {
              **c["extra"],
              "id": c["id"],
              "type": "function",
              "function": {
                  **c["function_extra"],
                  "name": c["name"],
                  "arguments": c["arguments"],
              },
          }
          for c in self.calls.values()
      ]
    if self.reasoning:
      message["reasoning_content"] = self.reasoning
    message = drop_thinking(message, unless=signed)
    kept_signed = bool(message.get("thinking_blocks")) or bool(
        isinstance(message.get("provider_specific_fields"), dict)
        and message["provider_specific_fields"].get("thinking_blocks")
    )
    if "reasoning_content" in message and (
        self.finish == "length"
        or ("claude" in MODEL.lower() and not kept_signed)
        or (had_thinking_blocks and not kept_signed)
    ):
      message.pop("reasoning_content", None)
    if message.get("thinking_blocks") == []:
      message.pop("thinking_blocks", None)
    if (
        isinstance(message.get("provider_specific_fields"), dict)
        and message["provider_specific_fields"].get("thinking_blocks") == []
    ):
      psf = dict(message["provider_specific_fields"])
      psf.pop("thinking_blocks", None)
      if psf:
        message["provider_specific_fields"] = psf
      else:
        message.pop("provider_specific_fields", None)
    if self.finish == "length" and isinstance(message.get("output_items"), list):
      message["output_items"] = [
          item
          for item in message["output_items"]
          if not (
              isinstance(item, dict)
              and item.get("type") == "reasoning"
              and not item.get("encrypted_content")
          )
      ]
    return message


class OutOfTime(Exception):  # pylint: disable=g-bad-exception-name
  """Raised when retrying a failed model call would exceed the time budget."""


class CutOff(asyncio.TimeoutError):  # pylint: disable=g-bad-exception-name
  """Raised when a streamed reply is cut off by its per-call deadline."""


class StreamBadRequest(openai.OpenAIError):  # pylint: disable=g-bad-exception-name
  """Raised when an SSE stream reports a non-retryable bad request error."""


# `type` or `code` values of an in-stream error that mark a bad request
BAD_REQUEST = frozenset({
    "invalid_request_error",
    "context_length_exceeded",
    "invalid_value",
    "invalid_prompt",
})


def stream_error(error: Any) -> openai.OpenAIError:
  """Classifies an in-stream error object as a bad request or a truncated stream."""
  def field(name: str) -> Any:
    if isinstance(error, dict):
      return error.get(name)
    return getattr(error, name, None)

  message = str(field("message") or "")
  named = {field("type"), field("code")} & BAD_REQUEST
  if named or CONTEXT_FULL.search(message):
    return StreamBadRequest(message)
  return StreamCut(f"the stream reported an error: {message}")


class StreamCut(openai.OpenAIError):  # pylint: disable=g-bad-exception-name
  """Raised when an SSE stream ends before reporting a finish reason."""


class Agent:
  """Manages the conversation, tool execution, and context compaction loop."""

  def __init__(self, session: taskbridge.Session):
    self.session = session
    self.tools = list(TOOLS)
    self.mcp: dict[str, tuple[MCPServer, dict[str, Any]]] = {}
    self.messages: list[dict[str, Any]] = []
    self.prompt_tokens = 0
    self.output_ceiling = int(os.environ.get("MAX_OUTPUT_TOKENS", "32000"))
    self.context_window: int | None = None  # learned from a context-full error
    self.max_output = self.output_ceiling  # the limit the last call asked for
    self.token_limit = "max_completion_tokens"
    self.effort = {"reasoning_effort": EFFORT} if EFFORT else {}
    self.responses = MODEL.lower().startswith(RESPONSES_MODELS)
    self.finish_margin = FINISH_MARGIN_SEC
    self.fix_rounds_done = 0
    self.max_fix_rounds = int(os.environ.get("MAX_FIX_ROUNDS", "2"))
    self.verify_share = float(os.environ.get("VERIFY_SHARE", "0.22"))
    self.mcp_prompt = ""
    self.pinned_bod = ""
    self._archive_breakpoint_idx: int | None = None
    self._in_verifier_gate = False
    self._pending_visual_messages: list[dict[str, Any]] = []
    self._vision_disabled = False
    self._has_incumbent_checkpoint = False

  def append_user(
      self, content: str | list[dict[str, Any]], visual: bool = False
  ) -> None:
    """Appends a user message, merging adjacent `role == 'user'` messages."""
    if self.messages and self.messages[-1].get("role") == "user":
      prev = self.messages[-1]
      old_c = prev.get("content")
      if isinstance(old_c, str) and isinstance(content, str):
        prev["content"] = f"{old_c}\n\n{content}" if old_c else content
      else:
        left_blocks = (
            [{"type": "text", "text": old_c}]
            if isinstance(old_c, str) and old_c
            else (list(old_c) if isinstance(old_c, list) else [])
        )
        right_blocks = (
            [{"type": "text", "text": content}]
            if isinstance(content, str) and content
            else (list(content) if isinstance(content, list) else [])
        )
        prev["content"] = left_blocks + right_blocks
      if visual:
        prev["_visual_inspection"] = True
      return
    msg: dict[str, Any] = {"role": "user", "content": content}
    if visual:
      msg["_visual_inspection"] = True
    self.messages.append(msg)

  def _flush_visual_messages(self) -> None:
    """Flushes queued visual inspection blocks after a tool-output batch."""
    if not self._pending_visual_messages:
      return
    pending = list(self._pending_visual_messages)
    self._pending_visual_messages.clear()
    for vmsg in pending:
      if self._vision_disabled and isinstance(vmsg.get("content"), list):
        texts = [
            str(b.get("text", ""))
            for b in vmsg["content"]
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        self.append_user("\n".join(texts), visual=False)
        self.prompt_tokens += sum(len(t) for t in texts) // 4 + 16
      else:
        self.append_user(vmsg["content"], visual=True)
        self.prompt_tokens += VISUAL_TOKEN_ESTIMATE

  async def run(self) -> str:
    """Runs the agent tool-calling loop until the task is complete."""
    mcp = await self.connect_mcp()
    self.mcp_prompt = mcp
    left = self.session.time_left()
    self.finish_margin = (
        FINISH_MARGIN_SEC
        if left is None
        else min(FINISH_MARGIN_SEC, max(left, 0) / 4)
    )
    user_prompt = self.session.instruction
    if not self._is_quick_probe_task():
      recon_report = await taskbridge.workspace_recon(self.session)
      if recon_report:
        user_prompt = (
            f"{self.session.instruction}\n\n"
            "## Automated Phase 1 Workspace Reconnaissance\n"
            f"```text\n{recon_report}\n```"
        )
    self.messages = [
        {
            "role": "system",
            "content": SYSTEM.format(
                workdir=self.session.workdir,
                mcp=mcp,
                minutes=int(left / 60) if left else "unlimited",
            ),
        },
        {"role": "user", "content": user_prompt},
    ]
    nudges, head = 0, ""
    while True:
      left = self.session.time_left()
      if left is not None and left < self.finish_margin:
        return await self.finish("Time is almost up.")
      try:
        reply = await self.complete(head=head)
      except OutOfTime:
        return await self.finish(
            "Time is almost up, and the model is not answering."
        )
      except CutOff:
        return await self.finish("Time is almost up.")
      arguments = [parse_arguments(call) for call in reply.calls.values()]
      if reply.text or reply.calls:
        self.messages.append(reply.message())
      text = visible(head + reply.text)
      if text:
        self.session.final_message = text
      if not reply.calls:
        if visible(reply.text) and reply.finish == "length":
          nudges, head = 0, head + reply.text
          self.append_user(
              f"Your reply was cut off at the {self.max_output}-token"
              " output limit. Continue where it stopped, and put long"
              " content in files with write_file rather than in a reply."
          )
          continue
        if visible(reply.text) or nudges >= 2:
          gate_feedback = await self.run_verification_gate(
              text or self.session.final_message
          )
          if gate_feedback:
            nudges, head = 0, ""
            self.append_user(gate_feedback)
            self.prompt_tokens += len(gate_feedback) // 4 + 16
            if self.prompt_tokens > self.compact_at():
              await self.compact()
            continue
          await self.restore_incumbent_if_corrupted()
          return text or self.session.final_message
        nudges += 1
        self.append_user(
            "Continue the task; when it is done, answer with "
            "your summary of the deliverables."
        )
        continue
      nudges, head = 0, ""
      for call, args in zip(reply.calls.values(), arguments):
        result = (
            await self.run_tool(call["name"], args)
            if isinstance(args, dict)
            else args
        )
        if reply.finish == "length" and isinstance(args, str):
          result += (
              f" Your reply hit the {self.max_output}-token output limit; write"
              " large files in parts."
          )
        self.messages.append(
            {"role": "tool", "tool_call_id": call["id"], "content": result}
        )
        self.prompt_tokens += len(result) // 4 + 16
      left = self.session.time_left()
      if left is not None:
        self.messages[-1]["content"] += f"\n[{int(left // 60)} minutes left]"
      self._flush_visual_messages()
      if self.prompt_tokens > self.compact_at():
        await self.compact()

  async def finish(self, why: str) -> str:
    """Restores any corrupted incumbent deliverable and requests a final summary."""
    restored = await self.restore_incumbent_if_corrupted()
    rollback_note = (
        f" (Rolled back corrupted files to verified G=1 incumbent: {restored}.)"
        if restored
        else ""
    )
    self.append_user(
        f"{why}{rollback_note} Stop working now and answer with your final "
        "summary: what you delivered, where, and the key results."
    )
    left = self.session.time_left()
    reserve = (
        0 if left is None else left - max(5, taskbridge.time_share(left, 30))
    )
    try:
      text = visible(
          (
              await self.complete(tool_choice="none", reserve=reserve, head="")
          ).text
      )
    except (CutOff, openai.OpenAIError, OutOfTime):
      text = ""
    return text or self.session.final_message

  async def connect_mcp(self) -> str:
    """Connects to the task's MCP servers and registers their tools."""
    for spec in self.session.mcp_servers:
      server = MCPServer(self.session, spec)
      try:
        await server.start()
      except (taskbridge.TaskError, RuntimeError, ValueError) as exc:
        await self.session.log(f"MCP server {server.name} unavailable: {exc}")
        continue
      for t in server.tools:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", f"{server.name}__{t['name']}")
        self.mcp[unique_name(name, self.mcp)] = (server, t)
      await self.session.log(
          f"MCP server {server.name}: {len(server.tools)} tools"
      )
    if not self.mcp:
      return ""
    functions = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": (t.get("description") or t["name"])[:1024],
                "parameters": (
                    {
                        k: v
                        for k, v in (t.get("inputSchema") or {}).items()
                        if k != "$schema"
                    }
                    or {"type": "object", "properties": {}}
                ),
            },
        }
        for name, (_, t) in self.mcp.items()
    ]
    if (
        len(json.dumps(functions)) <= DIRECT_MCP_CHARS
        and len(self.tools) + len(functions) <= MAX_TOOLS
    ):
      self.tools += functions
      return MCP_DIRECT
    self.tools += MCP_TOOLS
    servers: dict[str, list[str]] = {}
    for name, (server, _) in self.mcp.items():
      servers.setdefault(server.name, []).append(name)
    return MCP_CATALOG.format(
        catalog="\n".join(
            f"{s}: {', '.join(names)}" for s, names in servers.items()
        )
    )

  def _verifier_mcp_prompt(self) -> str:
    """Builds a read-only MCP system-prompt suffix for the Phase 4 Verifier."""
    if not self.mcp:
      return ""
    readonly_items = {
        name: (srv, t)
        for name, (srv, t) in self.mcp.items()
        if is_readonly_mcp_tool(name, t)
    }
    if not readonly_items:
      return ""
    if any(t["function"]["name"] == "mcp_find" for t in self.tools):
      servers: dict[str, list[str]] = {}
      for name, (server, _) in readonly_items.items():
        s_name = getattr(server, "name", None) or name.split("__", 1)[0]
        servers.setdefault(s_name, []).append(name)
      return MCP_CATALOG.format(
          catalog="\n".join(
              f"{s}: {', '.join(names)}" for s, names in servers.items()
          )
      )
    return MCP_DIRECT

  def find_mcp(self, query: str) -> str:
    """Finds MCP tools matching `query` and formats their schemas."""
    words = [w for w in re.split(r"[^a-z0-9]+", query.lower()) if w]
    pool = [
        item
        for item in self.mcp.items()
        if (not self._in_verifier_gate or is_readonly_mcp_tool(item[0], item[1][1]))
    ]

    def score(item):
      name, (_, t) = item
      text = f"{name} {t.get('description') or ''}".lower()
      return (
          (query == name) * 100
          + sum(w in name.lower() for w in words) * 3
          + sum(w in text for w in words)
      )

    best = [
        item
        for item in sorted(pool, key=score, reverse=True)[:8]
        if score(item) > 0
    ]
    if not best:
      return (
          f"no read-only tool matches {query!r}"
          if self._in_verifier_gate
          else (
              f"no tool matches {query!r}; the names are listed in the system"
              " prompt"
          )
      )
    found = [
        f"{name}: {(t.get('description') or '')[:400]}\nparameters:"
        f" {json.dumps(t.get('inputSchema') or {})}"
        for name, (_, t) in best
    ]
    while len(found) > 1 and len("\n\n".join(found)) > TOOL_RESULT_CHARS:
      found.pop()
    return "\n\n".join(found)

  async def complete(
      self,
      tool_choice: str = "auto",
      reserve: float | None = None,
      head: str | None = None,
  ) -> Reply:
    """Streams one model completion with retries and context-limit recovery."""
    reserve = self.finish_margin if reserve is None else reserve
    waits = iter(RETRY_WAITS)
    while True:
      try:
        reply = await self.stream(self.messages, tool_choice, reserve, head)
        if reply.finish is None:
          raise StreamCut("the stream ended without a finish reason")
        await self.record(reply)
        return reply
      except openai.APIStatusError as exc:
        if exc.status_code == 400 and await self.remedy(str(exc)):
          continue
        if not (exc.status_code in (408, 409, 429) or exc.status_code >= 500):
          raise
        failure = exc
      except StreamBadRequest as exc:
        if await self.remedy(str(exc)):
          continue
        raise
      except (openai.APIError, StreamCut) as exc:
        failure = exc
      wait = next(waits, None)
      if wait is None:
        raise failure
      left = self.session.time_left()
      if left is not None and left - wait < reserve:
        await self.session.log(
            f"model call failed ({str(failure)[:300]}); no time left to wait"
            f" {wait}s"
        )
        raise OutOfTime(str(failure)[:300]) from failure
      await self.session.log(
          f"model call failed ({str(failure)[:300]}); again in {wait}s"
      )
      await asyncio.sleep(wait)

  async def remedy(self, error: str) -> bool:
    """Adjusts request parameters for the next call after a bad request."""
    if CONTEXT_FULL.search(error):
      sent, measured = self.max_output, self.learn_context(error)
      if (
          measured
          and self.room() >= MIN_OUTPUT
          and self.output_limit() < sent
      ):
        return True
      return bool(
          await self.compact(force=True)
          or (not measured and self.output_limit() < sent)
          or self.cut_unread()
      )
    return (
        self.drop_unsigned_thinking_history(error)
        or self.drop_visual_inputs(error)
        or self.use_responses(error)
        or self.use_max_tokens(error)
        or self.drop_effort(error)
        or self.shrink_output(error)
    )

  def drop_unsigned_thinking_history(self, error: str) -> bool:
    """Strips rejected `thinking_blocks` / `reasoning_content` on HTTP 400."""
    if not THINKING_SIG_ERR.search(error):
      return False
    changed = False
    strip_all_blocks = bool(re.search(r"signature", error, re.I))
    for m in self.messages:
      if m.get("role") != "assistant":
        continue
      for field_name in ("reasoning_content", "reasoning"):
        if field_name in m:
          m.pop(field_name, None)
          changed = True
      if "thinking_blocks" in m:
        kept = [] if strip_all_blocks else [b for b in m["thinking_blocks"] if signed(b)]
        if kept != m.get("thinking_blocks"):
          changed = True
        if kept:
          m["thinking_blocks"] = kept
        else:
          m.pop("thinking_blocks", None)
      psf = m.get("provider_specific_fields")
      if isinstance(psf, dict) and "thinking_blocks" in psf:
        kept_psf = (
            []
            if strip_all_blocks
            else [b for b in psf["thinking_blocks"] if signed(b)]
        )
        if kept_psf != psf.get("thinking_blocks"):
          changed = True
        new_psf = dict(psf)
        if kept_psf:
          new_psf["thinking_blocks"] = kept_psf
        else:
          new_psf.pop("thinking_blocks", None)
        if new_psf:
          m["provider_specific_fields"] = new_psf
        else:
          m.pop("provider_specific_fields", None)
    return changed

  def drop_visual_inputs(self, error: str) -> bool:
    """Strips multimodal `image_url` blocks if an endpoint rejects vision."""
    if not VISION_ERR.search(error):
      return False
    changed = False
    for m in self.messages:
      content = m.get("content")
      if isinstance(content, list):
        kept_texts = [
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        if len(kept_texts) != len(content):
          m["content"] = (
              "\n".join(kept_texts)
              + "\n[visual image omitted: endpoint is text-only]"
          ).strip()
          m.pop("_visual_inspection", None)
          changed = True
    if changed:
      self._vision_disabled = True
    return changed

  def use_responses(self, error: str) -> bool:
    """Switches to the Responses API if a Chat Completions 400 points there."""
    if self.responses or not RESPONSES_ONLY.search(error):
      return False
    self.responses = True
    return True

  def use_max_tokens(self, error: str) -> bool:
    """Switches to `max_tokens` if `error` rejects `max_completion_tokens`."""
    if (
        self.token_limit != "max_completion_tokens"
        or "max_completion_tokens" not in error
        or not UNSUPPORTED.search(error)
    ):
      return False
    self.token_limit = "max_tokens"
    return True

  def drop_effort(self, error: str) -> bool:
    """Stops sending `reasoning_effort` once an endpoint refuses it."""
    if not self.effort or not re.search(r"reasoning[._]effort", error):
      return False
    self.effort = {}
    return True

  def shrink_output(self, error: str) -> bool:
    """Lowers `output_ceiling` when `error` indicates a smaller output limit."""
    if UNSUPPORTED.search(error):
      return False
    limit = re.search(r"(\d+)\s*\(exclusive\)", error)
    stated = re.search(
        r"at most (\d+)|(\d+), which is the maximum|maximum (?:allowed"
        r" )?(?:number of )?(?:output|"
        r"completion) tokens (?:is |of )?(\d+)",
        error,
        re.I,
    )
    if limit:
      new = int(limit.group(1)) - 1
    elif stated:
      new = int(next(g for g in stated.groups() if g))
    elif re.search(
        r"max_?(completion_)?tokens|maxOutputTokens|output tokens", error, re.I
    ):
      new = self.max_output // 2
    else:
      return False
    if new >= self.max_output or new < MIN_OUTPUT:
      return False
    self.output_ceiling = new
    return True

  def learn_context(self, error: str) -> bool:
    """Extracts context window and prompt token counts from `error`."""
    window, prompt = WINDOW_RE.search(error), PROMPT_RE.search(error)
    if window:
      self.context_window = int(next(g for g in window.groups() if g))
    if prompt:
      self.prompt_tokens = int(next(g for g in prompt.groups() if g))
    return bool(prompt and self.context_window)

  def room(self) -> int | None:
    """Returns remaining output token capacity in the known context window."""
    return (
        self.context_window - self.prompt_tokens - CONTEXT_MARGIN
        if self.context_window
        else None
    )

  def output_limit(self) -> int:
    """Computes the max output tokens to request on the next completion call."""
    room = self.room()
    return (
        self.output_ceiling
        if room is None
        else max(MIN_OUTPUT, min(self.output_ceiling, room))
    )

  def compact_at(self) -> int:
    """Returns the prompt token threshold at which to trigger compaction."""
    if not self.context_window:
      return COMPACT_AT_TOKENS
    room = min(self.output_ceiling, self.context_window // 2)
    return max(
        self.context_window // 4,
        min(COMPACT_AT_TOKENS, self.context_window - room - CONTEXT_MARGIN),
    )

  def estimate_prompt_tokens(self) -> int:
    """Estimates prompt token count while charging realistic image token budgets."""
    total = 0
    for m in self.messages:
      content = m.get("content")
      if isinstance(content, list):
        for part in content:
          if isinstance(part, dict) and part.get("type") == "image_url":
            total += VISUAL_TOKEN_ESTIMATE
          else:
            total += len(json.dumps(part)) // 4
        rest = {
            k: v
            for k, v in m.items()
            if k not in ("output_items", "content") and not k.startswith("_")
        }
        total += len(json.dumps(rest)) // 4
      else:
        total += (
            len(
                json.dumps({
                    k: v
                    for k, v in m.items()
                    if k != "output_items" and not k.startswith("_")
                })
            )
            // 4
        )
    return total

  async def stream(
      self,
      messages: list[dict[str, Any]],
      tool_choice: str,
      reserve: float,
      head: str | None = None,
  ) -> Reply:
    """Streams a single chat completion call up to `reserve` seconds before end."""
    left = self.session.time_left()
    self.max_output = self.output_limit()
    reply = Reply()

    async def read():
      if self.responses:
        await self.respond(reply, messages, tool_choice)
        return
      stream = await client.chat.completions.create(
          model=MODEL,
          messages=marked(messages, self._archive_breakpoint_idx),
          tools=self.tools,
          tool_choice=tool_choice,
          stream=True,
          stream_options={"include_usage": True},
          **self.effort,
          **{self.token_limit: self.max_output},
      )
      async for chunk in stream:
        reply.usage = chunk.usage or reply.usage
        if not chunk.choices:
          continue
        delta = chunk.choices[0].delta
        reply.finish = chunk.choices[0].finish_reason or reply.finish
        reply.text += delta.content or ""
        reply.reasoning += (
            getattr(delta, "reasoning_content", None)
            or getattr(delta, "reasoning", None)
            or ""
        )
        reply.extra = merged(reply.extra, extra_fields(delta, DELTA_FIELDS))
        for part in delta.tool_calls or []:
          reply.add_call(part)

    try:
      await asyncio.wait_for(read(), None if left is None else left - reserve)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
      if head is not None:
        self.session.final_message = (
            visible(head + reply.text) or self.session.final_message
        )
      if isinstance(exc, asyncio.CancelledError):
        raise
      raise CutOff() from None
    except openai.APIError as exc:
      said = {
          "message": getattr(exc, "message", None) or str(exc),
          "type": getattr(exc, "type", None),
          "code": getattr(exc, "code", None),
      }
      if isinstance(exc, openai.APIStatusError) or isinstance(
          stream_error(said), StreamCut
      ):
        raise
      raise StreamBadRequest(said["message"]) from exc
    for n, call in reply.calls.items():
      call["id"] = call["id"] or f"call_{n}_{len(self.messages)}"
      call["name"] = call["name"].split("<|")[0].strip()
    return reply

  async def respond(
      self, reply: Reply, messages: list[dict[str, Any]], tool_choice: str
  ) -> None:
    """Streams a single Responses API call into `reply`."""
    effort = self.effort.get("reasoning_effort")
    stream = await client.responses.create(
        model=MODEL,
        input=response_input(messages),
        tools=[
            {"type": "function", **t["function"], "strict": False}
            for t in self.tools
        ],
        tool_choice=tool_choice,
        stream=True,
        store=False,
        include=["reasoning.encrypted_content"],
        max_output_tokens=self.max_output,
        **({"reasoning": {"effort": effort}} if effort else {}),
    )
    async for event in stream:
      kind = getattr(event, "type", "")
      if kind == "response.output_text.delta":
        reply.text += event.delta or ""
      elif kind in ("error", "response.failed"):
        raise stream_error(
            getattr(getattr(event, "response", None), "error", None) or event
        )
      elif kind in ("response.completed", "response.incomplete"):
        done = event.response
        items = [i.model_dump(exclude_none=True) for i in done.output or []]
        reply.extra = {"output_items": items}
        reply.text = "".join(
            part.get("text", "")
            for item in items
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        )
        for item in items:
          if item.get("type") == "function_call":
            reply.calls[len(reply.calls)] = {
                "id": item.get("call_id") or "",
                "name": item.get("name") or "",
                "arguments": item.get("arguments") or "",
                "extra": {},
                "function_extra": {},
            }
        reply.usage = chat_usage(done.usage)
        reason = getattr(done.incomplete_details, "reason", None)
        if kind == "response.completed":
          reply.finish = "tool_calls" if reply.calls else "stop"
        else:
          reply.finish = reason if reason == "content_filter" else "length"

  async def record(self, reply: Reply) -> None:
    """Updates `prompt_tokens` from `reply` and reports token usage."""
    if reply.usage is None:
      self.prompt_tokens = self.estimate_prompt_tokens()
      return
    self.prompt_tokens = reply.usage.prompt_tokens or 0
    await self.report(reply)

  async def report(self, reply: Reply) -> None:
    """Reports token usage from `reply` to the evaluator session if present."""
    usage = reply.usage
    if usage is None:
      return
    cached = (
        getattr(
            getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0
        )
        or 0
    )
    await self.session.usage(
        MODEL, usage.prompt_tokens or 0, usage.completion_tokens or 0, cached
    )

  async def run_tool(self, name: str, args: dict[str, Any]) -> str:
    """Dispatches a tool call by `name` and returns its clipped text output."""
    await self.session.log(f"{name} {json.dumps(args)[:400]}")
    try:
      if name == "mcp_call":
        name, args = str(args.get("tool", "")), args.get("arguments") or {}
        args = json.loads(args) if isinstance(args, str) else args
        if name not in self.mcp:
          return f"error: no tool named {name!r}; use mcp_find"
      if name in self.mcp:
        server, t = self.mcp[name]
        if self._in_verifier_gate and not is_readonly_mcp_tool(name, t):
          return (
              f"error: mutating MCP tool {name!r} is blocked inside the"
              " read-only verification gate"
          )
        return clip(await server.call(t["name"], args))
      if name == "mcp_find":
        return self.find_mcp(str(args.get("query", "")))
      if self._in_verifier_gate and name in (
          "write_file",
          "edit_file",
          "pin_basis_of_design",
          "checkpoint_incumbent",
      ):
        return (
            f"error: mutating tool {name!r} is blocked inside the read-only"
            " verification gate"
        )
      handler = {
          "bash": self.bash,
          "read_file": self.read_file,
          "write_file": self.write_file,
          "edit_file": self.edit_file,
          "pin_basis_of_design": self.pin_basis_of_design,
          "inspect_visual_artifact": self.inspect_visual_artifact,
          "checkpoint_incumbent": self.checkpoint_incumbent,
          "verify_deliverable_schema": self.verify_deliverable_schema,
          "verify_calculations": self.verify_calculations,
      }.get(name)
      return (
          clip(await handler(**args))
          if handler
          else f"error: there is no tool named {name}"
      )
    except taskbridge.TaskError as exc:
      return f"error: {exc}"
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
      return f"error: {type(exc).__name__}: {exc}"

  def path(self, path: str) -> str:
    """Resolves `path` relative to `self.session.workdir` if not absolute.

    Args:
      path: Absolute or workdir-relative file path.

    Returns:
      The resolved container path string.
    """
    return (
        path
        if path.startswith("/")
        else posixpath.join(self.session.workdir, path)
    )

  async def bash(self, command: str, timeout_sec: int = 120) -> str:
    """Executes `command` with bash in the task container.

    Args:
      command: Shell command string to run.
      timeout_sec: Timeout in seconds, cut to end before the time kept for the
        final summary.

    Returns:
      Formatted exit code and command output string.
    """
    cap = int(self.session.limits.get("exec_timeout_sec") or 600)
    left = self.session.time_left()
    if left is not None:
      cap = min(cap, max(1, int(left - self.finish_margin)))
    result = await self.session.exec(
        command, taskbridge.positive_int(timeout_sec, 120, cap)
    )
    note = " (timed out)" if result.get("timed_out") else ""
    return (
        "exit code"
        f" {result['exit_code']}{note}\n{result['output'] or '(no output)'}"
    )

  async def read_file(self, path: str) -> str:
    """Reads a UTF-8 text file from `path` in the task container.

    Args:
      path: File path inside the task container.

    Returns:
      Decoded file contents, or a notice if the file is binary or empty.
    """
    data = await self.session.read_file(self.path(path))
    if b"\0" in data[:8192]:
      return (
          f"{path} is a binary file ({len(data)} bytes); inspect it with bash"
          " (python3, unzip -l, ...)"
      )
    return data.decode("utf-8", "replace") or "(empty file)"

  async def write_file(
      self, path: str, content: str, append: bool = False
  ) -> str:
    """Writes or appends `content` to `path` in the task container.

    Args:
      path: Destination file path inside the task container.
      content: Text content to write.
      append: Whether to append to the file instead of overwriting.

    Returns:
      Confirmation string with the resulting file size in bytes.
    """
    size = await self.session.write_file(
        self.path(path), content, append=bool(append)
    )
    return f"{path} now holds {size} bytes"

  async def edit_file(
      self, path: str, old_text: str, new_text: str, replace_all: bool = False
  ) -> str:
    """Replaces `old_text` with `new_text` in `path` inside the task container.

    Args:
      path: Target file path inside the task container.
      old_text: Substring to search for.
      new_text: Replacement string.
      replace_all: Whether to replace all occurrences instead of requiring one.

    Returns:
      Confirmation string or an error message if `old_text` is missing or
      ambiguous.
    """
    text = (await self.session.read_file(self.path(path))).decode("utf-8")
    count = text.count(old_text) if old_text else 0
    if count == 0:
      return f"error: old_text was not found in {path}"
    if count > 1 and not replace_all:
      return (
          f"error: old_text occurs {count} times in {path}; add context to make"
          " it unique, or set replace_all"
      )
    await self.session.write_file(
        self.path(path),
        text.replace(old_text, new_text, -1 if replace_all else 1),
    )
    return f"replaced {count if replace_all else 1} occurrence(s) in {path}"

  async def pin_basis_of_design(
      self,
      deliverables: list[str],
      hard_constraints_g: list[str],
      clause_grounding_register: list[str] | None = None,
      optimization_plan_o_prime: str = "",
      unit_invariants: list[str] | None = None,
  ) -> str:
    """Records and pins the Phase 1 Basis of Design (BoD) Register on `self.pinned_bod`."""
    lines = ["## Pinned Basis of Design (BoD) & Requirement-Clause Grounding Register"]
    lines.append("### 1. Required Deliverables & Exact Paths")
    for item in deliverables or []:
      lines.append(f"- {item}")
    lines.append("### 2. Hard-Constraint Gate (G = 1) Inequalities")
    for item in hard_constraints_g or []:
      lines.append(f"- {item}")
    if clause_grounding_register:
      lines.append("### 3. Requirement-Clause Grounding Register (Clause -> Reference Value)")
      for item in clause_grounding_register:
        lines.append(f"- {item}")
    if optimization_plan_o_prime:
      lines.append(f"### 4. Normalized Objective Credit (O') Plan\n- {optimization_plan_o_prime}")
    if unit_invariants:
      lines.append("### 5. Canonical SI / Imperial Unit Invariants")
      for item in unit_invariants:
        lines.append(f"- {item}")
    self.pinned_bod = "\n".join(lines)
    return (
        "Pinned Basis of Design registered and locked against context compaction"
        f" ({len(deliverables or [])} deliverables, {len(hard_constraints_g or [])}"
        " hard G-constraints).\n" + self.pinned_bod
    )

  async def inspect_visual_artifact(
      self, path: str, page: int = 1, dpi: int = 150
  ) -> str:
    """Renders a `.pdf`, `.dxf`, or image artifact to PNG and attaches it for vision."""
    target = self.path(path)
    ext = os.path.splitext(target)[1].lower().lstrip(".")
    supported = {
        "pdf",
        "dxf",
        "png",
        "jpg",
        "jpeg",
        "webp",
        "bmp",
        "gif",
        "tif",
        "tiff",
    }
    if ext not in supported:
      return (
          f"error: unsupported visual extension .{ext or '(none)'} for {target};"
          f" expected one of {sorted(supported)}"
      )
    page_num = max(1, int(page or 1))
    dpi_num = max(72, min(int(dpi or 150), 300))
    try:
      raw_bytes = await self.session.read_file(target)
    except taskbridge.TaskError as exc:
      if ext == "pdf" and "over the 10 MB" in str(exc):
        tmp_prefix = "/tmp/_ple_visual_page"
        cmd = (
            f"pdftoppm -png -r {dpi_num} -f {page_num} -l {page_num}"
            f" -singlefile {json.dumps(target)} {tmp_prefix}"
        )
        res = await self.session.exec(cmd, timeout_sec=60)
        if res.get("exit_code") != 0:
          return f"error: large PDF render failed: {res.get('output', '')[:400]}"
        raw_bytes = await self.session.read_file(f"{tmp_prefix}.png")
        ext = "png"
      else:
        raise
    png_bytes, w, h, meta = render_visual_bytes_to_png(
        raw_bytes, ext, page=page_num, dpi=dpi_num
    )
    b64 = base64.b64encode(png_bytes).decode("ascii")
    caption = (
        f"[Visual Plan Inspection: {target} | {w}x{h} px"
        + (
            f" | page {meta['page_rendered']}/{meta['total_pages']}"
            if "page_rendered" in meta
            else ""
        )
        + f" | renderer={meta.get('renderer')}]"
    )
    self._pending_visual_messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": caption},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            },
        ],
    })
    return json.dumps(
        {
            "status": (
                "ATTACHED_TEXT_FALLBACK"
                if self._vision_disabled
                else "ATTACHED_VISUAL_PNG"
            ),
            "path": target,
            "width": w,
            "height": h,
            "png_bytes": len(png_bytes),
            **meta,
        },
        indent=2,
    )

  async def _snapshot_verified_paths(
      self,
      verified_paths: list[str],
      objective_value: float | None = None,
      note: str = "",
  ) -> dict[str, Any]:
    """Copies verified G=1 deliverables into `/tmp/_ple_incumbent_best/`."""
    snap_script = f"""
import os, json, shutil, time, re

inc_dir = {json.dumps(INCUMBENT_DIR)}
files_root = os.path.join(inc_dir, "files")
manifest_path = os.path.join(inc_dir, "manifest.json")
paths = {json.dumps(verified_paths)}
obj_val = {json.dumps(objective_value)}
note = {json.dumps(note or "")}

os.makedirs(files_root, exist_ok=True)
manifest = {{"updated_at": time.time(), "objective_value": obj_val, "note": note, "files": {{}}}}
if os.path.exists(manifest_path):
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            old_m = json.load(fh)
        if isinstance(old_m, dict) and isinstance(old_m.get("files"), dict):
            manifest["files"].update(old_m["files"])
        if obj_val is None and old_m.get("objective_value") is not None:
            manifest["objective_value"] = old_m["objective_value"]
    except Exception:
        pass

saved = []
for fp in paths:
    if not os.path.isfile(fp) or os.path.getsize(fp) == 0:
        continue
    rel = re.sub(r"^[A-Za-z]:", "", fp).lstrip("/\\\\")
    bkp = os.path.join(files_root, rel)
    os.makedirs(os.path.dirname(bkp), exist_ok=True)
    if os.path.abspath(fp) != os.path.abspath(bkp):
        shutil.copy2(fp, bkp)
    sz = os.path.getsize(bkp)
    manifest["files"][fp] = {{
        "backup_path": bkp,
        "size_bytes": sz,
        "saved_at": time.time(),
    }}
    saved.append({{"path": fp, "size_bytes": sz}})

apps_data = "/app/tools/.apps_data"
if os.path.isdir(apps_data):
    apps_bkp = os.path.join(inc_dir, "apps_data")
    if os.path.exists(apps_bkp):
        shutil.rmtree(apps_bkp, ignore_errors=True)
    shutil.copytree(apps_data, apps_bkp)
    manifest["apps_data_backup"] = apps_bkp

with open(manifest_path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, indent=2)

print(json.dumps({{
    "checkpoint_dir": inc_dir,
    "saved_files": saved,
    "total_tracked_files": len(manifest["files"]),
    "objective_value": manifest["objective_value"],
}}))
"""
    res = await self.session.exec(
        f"python3 -c {json.dumps(snap_script)}", timeout_sec=60
    )
    out = (res.get("output") or "").strip()
    try:
      parsed = json.loads(out)
      if parsed.get("total_tracked_files", 0) > 0:
        self._has_incumbent_checkpoint = True
      return parsed
    except ValueError:
      return {"raw_output": out}

  async def checkpoint_incumbent(
      self,
      paths: list[str],
      objective_value: float | None = None,
      note: str = "",
  ) -> str:
    """Validates `paths` and snapshots a physical G=1 backup in `/tmp/_ple_incumbent_best/`."""
    targets = [self.path(str(p)) for p in (paths or []) if str(p).strip()]
    if not targets:
      return "error: paths must contain at least one deliverable file path"
    invalid: list[dict[str, Any]] = []
    for target in targets:
      rep = await self._run_schema_audit(target)
      if not rep.get("valid"):
        invalid.append({
            "file_path": target,
            "defects": rep.get("defects", ["SCHEMA_VALIDATION_FAILED"]),
        })
    if invalid:
      return json.dumps(
          {
              "status": "REJECTED_INVALID_CANDIDATE",
              "reason": (
                  "Refusing to overwrite G=1 incumbent checkpoint because one"
                  " or more candidate files failed structural validation."
              ),
              "invalid_files": invalid,
          },
          indent=2,
      )
    snap = await self._snapshot_verified_paths(
        targets, objective_value=objective_value, note=note
    )
    return json.dumps({"status": "CHECKPOINT_SAVED", **snap}, indent=2)

  async def restore_incumbent_if_corrupted(self) -> list[str]:
    """Restores checkpointed G=1 deliverables if current disk files are missing or corrupted."""
    if not self._has_incumbent_checkpoint:
      return []
    restore_script = f"""
import os, json, shutil, zipfile, re

inc_dir = {json.dumps(INCUMBENT_DIR)}
manifest_path = os.path.join(inc_dir, "manifest.json")
if not os.path.isfile(manifest_path):
    print("[]")
    raise SystemExit(0)

with open(manifest_path, "r", encoding="utf-8") as fh:
    manifest = json.load(fh)

def is_corrupted(fp):
    if not os.path.isfile(fp) or os.path.getsize(fp) == 0:
        return True
    low = fp.lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(fp, "r") as zf:
                if zf.testzip() is not None:
                    return True
                infos = [zi for zi in zf.infolist() if not zi.is_dir()]
                if not infos or any(zi.file_size == 0 for zi in infos):
                    return True
        elif low.endswith((".xlsx", ".docx")):
            with zipfile.ZipFile(fp, "r") as zf:
                names = set(zf.namelist())
                if "[Content_Types].xml" not in names:
                    return True
        elif low.endswith(".pdf"):
            with open(fp, "rb") as f:
                if not f.read(8).startswith(b"%PDF-"):
                    return True
        elif low.endswith(".json"):
            with open(fp, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            if re.search(r"\\b(?:NaN|Infinity|-Infinity)\\b", raw):
                return True
            json.loads(raw)
        elif low.endswith(".ifc"):
            with open(fp, "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            clean = re.sub(r"'(?:''|[^'])*'", "''", raw)
            if "ISO-10303-21" not in raw[:512] or "IFCPROJECT" not in clean.upper():
                return True
            defs = set(re.findall(r"#(\\d+)\\s*=", clean))
            refs = set(re.findall(r"#(\\d+)", " ".join(re.findall(r"=\\s*[^;]+;", clean))))
            if refs - defs:
                return True
        elif low.endswith(".dxf"):
            with open(fp, "rb") as f:
                up = f.read().decode("utf-8", "replace").upper()
            if "SECTION" not in up or "ENTITIES" not in up or "EOF" not in up:
                return True
    except Exception:
        return True
    return False

restored = []
for fp, meta in (manifest.get("files") or {{}}).items():
    bkp = meta.get("backup_path") or ""
    if bkp and os.path.isfile(bkp) and os.path.getsize(bkp) > 0 and is_corrupted(fp):
        os.makedirs(os.path.dirname(fp) or "/", exist_ok=True)
        shutil.copy2(bkp, fp)
        restored.append(fp)

print(json.dumps(restored))
"""
    try:
      res = await self.session.exec(
          f"python3 -c {json.dumps(restore_script)}", timeout_sec=45
      )
      restored = json.loads((res.get("output") or "[]").strip())
      if isinstance(restored, list) and restored:
        await self.session.log(
            f"Restored corrupted deliverables from G=1 incumbent checkpoint: {restored}"
        )
        return [str(p) for p in restored]
    except Exception as exc:  # pylint: disable=broad-exception-caught
      await self.session.log(f"Incumbent rollback check warning: {exc}")
    return []

  async def _run_schema_audit(
      self,
      target: str,
      expected_format: str = "",
      required_keys_or_columns: list[str] | None = None,
      check_zip_members: bool = True,
  ) -> dict[str, Any]:
    """Executes full-file (up to 64 MiB) quote-safe structural schema verification."""
    req_list = list(required_keys_or_columns or [])
    fmt = (expected_format or os.path.splitext(target)[1].lstrip(".")).lower()
    check_script = f"""
import os, sys, io, json, csv, zipfile, re

fp = {json.dumps(target)}
fmt = {json.dumps(fmt)}
req = {json.dumps(req_list)}
chk_zip = {bool(check_zip_members)}
max_bytes = {int(MAX_SCHEMA_BYTES)}

report = {{"file_path": fp, "exists": os.path.exists(fp), "size_bytes": 0, "valid": False, "defects": [], "details": {{}}}}
if not report["exists"]:
    report["defects"].append("FILE_NOT_FOUND")
    print(json.dumps(report, indent=2))
    sys.exit(0)

sz = os.path.getsize(fp)
report["size_bytes"] = sz
if sz == 0:
    report["defects"].append("EMPTY_ZERO_BYTE_FILE")
    print(json.dumps(report, indent=2))
    sys.exit(0)

def check_ifc(txt, path_for_lib=None):
    errs = []
    if "ISO-10303-21" not in txt[:512]:
        errs.append("INVALID_IFC_HEADER")
    clean = re.sub(r"'(?:''|[^'])*'", "''", txt)
    if "IFCPROJECT" not in clean.upper():
        errs.append("MISSING_IFCPROJECT")
    if re.search(r"IFC(?:CARTESIANPOINT|POLYLINE|DIRECTION|EXTRUDEDAREASOLID)\\s*\\([^;]*\\$\\s*\\)", clean, re.I):
        errs.append("DEGENERATE_IFC_NULL_GEOMETRY")
    defs = set(re.findall(r"#(\\d+)\\s*=", clean))
    rhs = re.findall(r"=\\s*[^;]+;", clean)
    refs = set(re.findall(r"#(\\d+)", " ".join(rhs)))
    dangling = sorted(refs - defs, key=lambda x: int(x))
    if dangling:
        errs.append(f"DANGLING_IFC_REFS(#{{',#'.join(dangling[:6])}})")
    if path_for_lib and not errs:
        try:
            import ifcopenshell
            ifc_obj = ifcopenshell.open(path_for_lib)
            report["details"]["ifc_schema"] = str(getattr(ifc_obj, "schema", ""))
        except ImportError:
            pass
        except Exception as exc:
            errs.append(f"IFCOPENSHELL_PARSE_ERROR({{exc}})")
    return errs

def check_dxf(txt, path_for_lib=None):
    up = txt.upper()
    errs = []
    if "SECTION" not in up or "EOF" not in up:
        errs.append("INVALID_OR_TRUNCATED_DXF")
    if "ENTITIES" not in up:
        errs.append("MISSING_DXF_ENTITIES_SECTION")
    if path_for_lib and not errs:
        try:
            import ezdxf
            doc = ezdxf.readfile(path_for_lib)
            report["details"]["dxf_entity_count"] = len(list(doc.modelspace()))
        except ImportError:
            pass
        except Exception as exc:
            errs.append(f"EZDXF_PARSE_ERROR({{exc}})")
    return errs

def check_openxml(src, kind):
    errs = []
    try:
        with zipfile.ZipFile(src, "r") as z:
            names = set(z.namelist())
            if "[Content_Types].xml" not in names:
                errs.append("MISSING_CONTENT_TYPES_XML")
            if kind == "xlsx":
                sheets = [n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")]
                if not sheets:
                    errs.append("MISSING_XLSX_WORKSHEET_XML")
            elif kind == "docx":
                if "word/document.xml" not in names:
                    errs.append("MISSING_DOCX_DOCUMENT_XML")
    except Exception as e:
        errs.append(f"INVALID_OPENXML_ZIP({{e}})")
    return errs

try:
    if fmt == "zip" or fp.lower().endswith(".zip"):
        with zipfile.ZipFile(fp, "r") as zf:
            bad = zf.testzip()
            if bad:
                report["defects"].append(f"CORRUPT_ZIP_MEMBER({{bad}})")
            infos = [zi for zi in zf.infolist() if not zi.is_dir()]
            members = [zi.filename for zi in infos]
            report["details"]["members"] = members
            if not members:
                report["defects"].append("EMPTY_ZIP_ARCHIVE")
            for zi in infos:
                if zi.file_size == 0:
                    report["defects"].append(f"ZERO_BYTE_ZIP_MEMBER({{zi.filename}})")
                elif chk_zip and zi.file_size <= max_bytes:
                    mb = zf.read(zi.filename)
                    ml = zi.filename.lower()
                    if ml.endswith(".xlsx"):
                        for d in check_openxml(io.BytesIO(mb), "xlsx"):
                            report["defects"].append(f"{{zi.filename}}:{{d}}")
                    elif ml.endswith(".docx"):
                        for d in check_openxml(io.BytesIO(mb), "docx"):
                            report["defects"].append(f"{{zi.filename}}:{{d}}")
                    elif ml.endswith(".ifc"):
                        for d in check_ifc(mb.decode("utf-8", "replace")):
                            report["defects"].append(f"{{zi.filename}}:{{d}}")
                    elif ml.endswith(".dxf"):
                        for d in check_dxf(mb.decode("utf-8", "replace")):
                            report["defects"].append(f"{{zi.filename}}:{{d}}")
            for r_item in req:
                if not any(r_item == m or m.endswith("/" + r_item) for m in members):
                    report["defects"].append(f"MISSING_REQUIRED_ARCHIVE_MEMBER({{r_item}})")
    elif fmt == "ifc" or fp.lower().endswith(".ifc"):
        if sz <= max_bytes:
            with open(fp, "rb") as fh:
                txt = fh.read().decode("utf-8", "replace")
            report["defects"].extend(check_ifc(txt, path_for_lib=fp))
        else:
            report["details"]["skipped_full_read_over_64mb"] = sz
    elif fmt == "dxf" or fp.lower().endswith(".dxf"):
        if sz <= max_bytes:
            with open(fp, "rb") as fh:
                txt = fh.read().decode("utf-8", "replace")
            report["defects"].extend(check_dxf(txt, path_for_lib=fp))
        else:
            report["details"]["skipped_full_read_over_64mb"] = sz
    elif fmt in ("xlsx", "docx") or fp.lower().endswith((".xlsx", ".docx")):
        kind = "xlsx" if (fmt == "xlsx" or fp.lower().endswith(".xlsx")) else "docx"
        report["defects"].extend(check_openxml(fp, kind))
    elif fmt == "pdf" or fp.lower().endswith(".pdf"):
        with open(fp, "rb") as fh:
            if not fh.read(8).startswith(b"%PDF-"):
                report["defects"].append("INVALID_PDF_HEADER")
    elif fmt == "json" or fp.lower().endswith(".json"):
        with open(fp, "rb") as fh:
            raw = fh.read(max_bytes).decode("utf-8", "replace")
        if re.search(r"\\b(?:NaN|Infinity|-Infinity)\\b", raw):
            report["defects"].append("JSON_CONTAINS_NAN_OR_INF")
        obj = json.loads(raw)
        if isinstance(obj, dict):
            report["details"]["keys"] = list(obj.keys())
            for k in req:
                if k not in obj or obj[k] is None or obj[k] == "":
                    report["defects"].append(f"MISSING_OR_EMPTY_JSON_KEY({{k}})")
    elif fmt == "csv" or fp.lower().endswith(".csv"):
        with open(fp, "r", encoding="utf-8", errors="replace", newline="") as fh:
            rows = list(csv.reader(fh))
        if not rows:
            report["defects"].append("EMPTY_CSV")
        else:
            headers = [h.strip() for h in rows[0]]
            report["details"]["columns"] = headers
            report["details"]["row_count"] = max(0, len(rows) - 1)
            if len(rows) < 2:
                report["defects"].append("CSV_HAS_HEADER_ONLY_ZERO_DATA_ROWS")
            for c in req:
                if c not in headers:
                    report["defects"].append(f"MISSING_CSV_COLUMN({{c}})")
    else:
        with open(fp, "rb") as fh:
            raw = fh.read(max_bytes).decode("utf-8", "replace")
        for k in req:
            if k not in raw:
                report["defects"].append(f"MISSING_REQUIRED_CONTENT({{k}})")
except Exception as exc:
    report["defects"].append(f"SCHEMA_CHECK_EXCEPTION({{exc}})")

report["valid"] = (len(report["defects"]) == 0)
print(json.dumps(report, indent=2))
"""
    res = await self.session.exec(
        f"python3 -c {json.dumps(check_script)}", timeout_sec=60
    )
    out = (res.get("output") or "").strip()
    try:
      return json.loads(out)
    except ValueError:
      return {
          "file_path": target,
          "valid": False,
          "defects": [f"NON_JSON_SCHEMA_OUTPUT({out[:240]})"],
      }

  async def verify_deliverable_schema(
      self,
      file_path: str,
      expected_format: str = "",
      required_keys_or_columns: list[str] | None = None,
      check_zip_members: bool = True,
  ) -> str:
    """Performs full-file structural verification and auto-snapshots valid deliverables."""
    target = self.path(file_path)
    report = await self._run_schema_audit(
        target,
        expected_format=expected_format,
        required_keys_or_columns=required_keys_or_columns,
        check_zip_members=check_zip_members,
    )
    if (
        not self._in_verifier_gate
        and report.get("valid") is True
        and not target.startswith("/tmp/")
    ):
      snap = await self._snapshot_verified_paths(
          [target], note="auto-checkpoint on verify_deliverable_schema"
      )
      report.setdefault("details", {})["incumbent_checkpoint"] = snap.get(
          "checkpoint_dir", INCUMBENT_DIR
      )
    return json.dumps(report, indent=2)

  async def verify_calculations(
      self, script: str, summary: str = ""
  ) -> str:
    """Writes `script` to `/tmp/_ple_verify_calc.py` and executes it with python3."""
    script_path = "/tmp/_ple_verify_calc.py"
    await self.session.write_file(script_path, script)
    header = f"[Solver Check: {summary}]\n" if summary else ""
    out = await self.bash(f"python3 {script_path}", timeout_sec=120)
    return header + out

  def _is_quick_probe_task(self) -> bool:
    """Returns True for short `kit check` smoke/MCP/cancel tasks so they finish immediately."""
    inst = self.session.instruction.strip()
    if len(inst) < 360 and ("hello.txt" in inst or "secret_word" in inst or "mcp.txt" in inst):
      return True
    budget = self.session.limits.get("agent_timeout_sec")
    if isinstance(budget, (int, float)) and budget <= 650:
      return True
    return False

  async def run_verification_gate(self, summary: str) -> str | None:
    """Executes Phase 3 (Full-File Deliverable Audit) and Phase 4 (Read-Only Peer Review)."""
    if self._is_quick_probe_task():
      return None
    if self.fix_rounds_done >= self.max_fix_rounds:
      await self.restore_incumbent_if_corrupted()
      return None
    left = self.session.time_left()
    total_budget = float(self.session.limits.get("agent_timeout_sec") or 0)
    min_verify_sec = (
        max(self.finish_margin + 180, min(600.0, total_budget * self.verify_share))
        if total_budget > 0
        else max(self.finish_margin + 180, 300.0)
    )
    if left is not None and left < min_verify_sec:
      await self.restore_incumbent_if_corrupted()
      return None
    self.fix_rounds_done += 1
    await self.session.log(
        f"Phase 3/4 Verification Gate (round {self.fix_rounds_done}/{self.max_fix_rounds})"
    )
    audit_report = await taskbridge.audit_deliverables(self.session)
    bod_block = f"{self.pinned_bod}\n\n" if self.pinned_bod else ""
    review_prompt = (
        f"## Original Engineering Task\n{self.session.instruction}\n\n"
        f"{bod_block}"
        f"## Automated Container Deliverable Audit\n{audit_report}\n\n"
        "## Worker's Proposed Final Summary (UNTRUSTED CLAIMS — VERIFY AGAINST"
        f" DISK & MCP STATE)\n{summary}\n\n"
        "Inspect the deliverables, drawings, calculations, and read-only MCP "
        "state directly using your tools. Do NOT trust claims in the worker's "
        "summary without checking the actual files/state on disk. If every "
        "required deliverable exists at the exact path, is non-empty, and "
        "satisfies all hard constraints (G=1), reply with the single word PASS. "
        "Otherwise list the specific defects to fix."
    )
    saved_state = (
        self.messages,
        self.tools,
        self.prompt_tokens,
        self.max_output,
        self.output_ceiling,
        self.context_window,
        self.token_limit,
        dict(self.effort),
        self.responses,
        self._archive_breakpoint_idx,
        self._in_verifier_gate,
        list(self._pending_visual_messages),
    )
    has_readonly_mcp = any(
        is_readonly_mcp_tool(n, sp[1]) for n, sp in self.mcp.items()
    )
    allowed_builtin = {
        "bash",
        "read_file",
        "inspect_visual_artifact",
        "verify_deliverable_schema",
        "verify_calculations",
    }
    if has_readonly_mcp:
      allowed_builtin.update({"mcp_find", "mcp_call"})
    try:
      self._in_verifier_gate = True
      self._archive_breakpoint_idx = None
      self._pending_visual_messages = []
      self.tools = [
          t
          for t in saved_state[1]
          if t["function"]["name"] in allowed_builtin
          or (
              t["function"]["name"] in self.mcp
              and is_readonly_mcp_tool(
                  t["function"]["name"], self.mcp[t["function"]["name"]][1]
              )
          )
      ]
      verifier_msgs = [
          {
              "role": "system",
              "content": (
                  VERIFIER_SYSTEM.format(workdir=self.session.workdir)
                  + self._verifier_mcp_prompt()
              ),
          },
          {"role": "user", "content": review_prompt},
      ]
      self.messages = verifier_msgs
      self.prompt_tokens = self.estimate_prompt_tokens()
      for step in range(6):
        v_left = self.session.time_left()
        if v_left is not None and v_left < self.finish_margin + 120:
          return None
        v_reply = await self.complete(
            tool_choice="auto", reserve=self.finish_margin + 90
        )
        v_args = [parse_arguments(c) for c in v_reply.calls.values()]
        if v_reply.text or v_reply.calls:
          self.messages.append(v_reply.message())
        if not v_reply.calls:
          verdict = visible(v_reply.text).strip()
          if not verdict and v_reply.finish == "length" and step < 5:
            self.append_user(
                "Your previous turn hit the output token limit before emitting "
                "a tool call or verdict. State your next tool call or your "
                "concise final verdict (PASS or FAIL: <defects>) immediately."
            )
            continue
          await self.session.log(f"Verifier verdict: {verdict[:600]}")
          cleaned_verdict = verdict.strip("*_. \r\n\t").upper()
          if cleaned_verdict == "PASS" or (
              not cleaned_verdict and v_reply.finish != "length"
          ):
            return None
          if not cleaned_verdict and v_reply.finish == "length":
            verdict = "FAIL: Verifier output truncated at max_output_tokens before PASS could be confirmed."
          return (
              "## Phase 4 Independent Licensed-Engineer Verification Found Gaps\n"
              f"{verdict}\n\n"
              f"Container Deliverable Audit:\n{audit_report}\n\n"
              "Fix every gap above, verify the updated deliverable files on disk, "
              "and then provide your updated final engineering summary."
          )
        for call, args in zip(v_reply.calls.values(), v_args):
          res = (
              await self.run_tool(call["name"], args)
              if isinstance(args, dict)
              else args
          )
          self.messages.append(
              {"role": "tool", "tool_call_id": call["id"], "content": res}
          )
          self.prompt_tokens += len(res) // 4 + 16
        self._flush_visual_messages()
        if step == 5:
          v_left = self.session.time_left()
          if v_left is not None and v_left < self.finish_margin + 90:
            return None
          self.append_user(
              "Tool budget reached. Based on the evidence gathered above, "
              "provide your immediate final verdict now: reply with the "
              "single word PASS if all deliverables and hard constraints "
              "(G=1) hold, or FAIL: followed by the numbered list of "
              "concrete defects."
          )
          final_v_reply = await self.complete(
              tool_choice="none", reserve=self.finish_margin + 60
          )
          verdict = visible(final_v_reply.text).strip()
          await self.session.log(f"Verifier forced verdict: {verdict[:600]}")
          if verdict.strip("*_. \r\n\t").upper() in ("", "PASS"):
            return None
          return (
              "## Phase 4 Independent Licensed-Engineer Verification Found Gaps\n"
              f"{verdict}\n\n"
              f"Container Deliverable Audit:\n{audit_report}\n\n"
              "Fix every gap above, verify the updated deliverable files on disk, "
              "and then provide your updated final engineering summary."
          )
    except (OutOfTime, CutOff):
      raise
    except Exception as exc:  # pylint: disable=broad-exception-caught
      await self.session.log(f"Verification gate skipped on error: {exc}")
    finally:
      (
          self.messages,
          self.tools,
          self.prompt_tokens,
          self.max_output,
          self.output_ceiling,
          self.context_window,
          self.token_limit,
          self.effort,
          self.responses,
          self._archive_breakpoint_idx,
          self._in_verifier_gate,
          self._pending_visual_messages,
      ) = saved_state
    return None

  def unread(self) -> list[dict[str, Any]]:
    """Returns tool result messages appended after the last assistant turn."""
    last = max(
        (n for n, m in enumerate(self.messages) if m["role"] == "assistant"),
        default=0,
    )
    return [m for m in self.messages[last:] if m["role"] == "tool"]

  async def compact(self, force: bool = False) -> bool:
    """Cache-Aligned Compaction preserving an immutable prefix (`messages[:2]`)."""
    unread_msgs = self.unread()
    unread = {id(m) for m in unread_msgs}
    if sum(
        len(m["content"])
        for m in unread_msgs
        if isinstance(m.get("content"), str)
    ) > 3 * TOOL_RESULT_CHARS:
      self.cut_unread()

    # Evict older base64 visual attachments beyond the immutable prefix while
    # keeping the most recent 4 messages intact for active visual inspection.
    protected_prefix = max(2, (self._archive_breakpoint_idx or 1) + 1)
    visual_evicted = 0
    recent_floor = max(protected_prefix, len(self.messages) - 4)
    for idx in range(protected_prefix, recent_floor):
      m = self.messages[idx]
      content = m.get("content")
      if isinstance(content, list) and any(
          isinstance(b, dict) and b.get("type") == "image_url" for b in content
      ):
        kept_text = "\n".join(
            str(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        self.messages[idx] = {
            "role": m["role"],
            "content": (
                f"{kept_text}\n[archived visual PNG evicted to preserve context"
                " budget; call inspect_visual_artifact again if needed]"
            ).strip(),
        }
        visual_evicted += 1

    if visual_evicted:
      await self.session.log(
          f"context: evicted {visual_evicted} older visual attachment(s)"
      )
      self.prompt_tokens = self.estimate_prompt_tokens()
      if (
          not force
          or not self.context_window
          or self.prompt_tokens <= self.compact_at()
      ):
        return True

    # Align `cut` onto an `assistant` boundary so the kept suffix starts with
    # `assistant` right after our compacted `user` archive turn (strict role
    # alternation: system -> user -> assistant -> user -> assistant).
    cut = len(self.messages) - 6
    while cut > 3 and self.messages[cut].get("role") != "assistant":
      cut -= 1

    if cut >= 8 and self.messages[cut].get("role") == "assistant":
      try:
        notes = await self.stream(
            self.messages[:cut]
            + [{"role": "user", "content": SUMMARY_PROMPT}],
            "none",
            self.finish_margin,
        )
        await self.report(notes)
        text = visible(notes.text) if notes.finish is not None else ""
        if text:
          if notes.finish == "length":
            text += "\n[the notes were cut off at the output limit]"
          pinned_prefix = f"{self.pinned_bod}\n\n" if self.pinned_bod else ""
          ack_turn = {
              "role": "assistant",
              "content": (
                  "Acknowledged. Archiving verified Phase 1-3 engineering state"
                  " and Basis of Design register to maintain KV-cache prefix"
                  " alignment."
              ),
          }
          archive_turn = {
              "role": "user",
              "content": (
                  pinned_prefix
                  + "Your notes on the work so far (earlier turns were"
                  " archived to save context):\n"
                  + text
              ),
          }
          self.messages = (
              self.messages[:2]
              + [ack_turn, archive_turn]
              + self.messages[cut:]
          )
          self._archive_breakpoint_idx = 3
          await self.session.log(
              f"context: cache-aligned archive of {cut - 2} messages"
          )
          self.prompt_tokens = self.estimate_prompt_tokens()
          return True
      except (openai.APIError, StreamCut, asyncio.TimeoutError) as exc:
        await self.session.log(
            f"context: notes failed ({str(exc)[:200] or 'out of time'})"
        )

    # Emergency fallback when too few turns exist (`cut < 8`) or notes failed:
    # truncate older read tool outputs strictly after `protected_prefix` so the
    # cached prefix `messages[:protected_prefix]` remains 100% byte-identical.
    results = [
        (n, m)
        for n, m in enumerate(self.messages)
        if n >= protected_prefix
        and m.get("role") == "tool"
        and isinstance(m.get("content"), str)
    ]
    cuts, size = [], max(
        self.estimate_prompt_tokens(), self.prompt_tokens if force else 0
    )
    candidates = results if force else results[:-KEEP_RECENT_RESULTS]
    for n, m in candidates:
      if force and self.context_window and size <= self.compact_at():
        break
      if len(m["content"]) > 600 and id(m) not in unread:
        size -= (len(m["content"]) - 400) // 4
        self.messages[n] = {
            **m,
            "content": (
                m["content"][:400]
                + "\n[... removed to save context; run the tool again if needed"
                " ...]"
            ),
        }
        cuts.append(n)
    if cuts:
      await self.session.log(f"context: cut {len(cuts)} old tool results")
      self.prompt_tokens = self.estimate_prompt_tokens()
      return True
    return bool(visual_evicted)

  def cut_unread(self) -> bool:
    """Truncates unread tool results as a last resort when context is full."""
    unread = [
        m
        for m in self.unread()
        if isinstance(m.get("content"), str)
        and len(m["content"]) > 2 * UNREAD_KEEP
    ]
    if not unread:
      return False
    share = None
    if self.context_window:
      rest = (
          self.estimate_prompt_tokens()
          - sum(len(m["content"]) for m in unread) // 4
      )
      share = (self.compact_at() - rest) * 4 // len(unread)
    for m in unread:
      size = len(m["content"])
      keep = size // 2 if share is None or share >= size - 100 else share
      m["content"] = clip(m["content"], max(UNREAD_KEEP, keep))
    self.prompt_tokens = self.estimate_prompt_tokens()
    return True


async def solve(session: taskbridge.Session) -> str:
  """Runs `Agent` on `session` and returns the final answer.

  Args:
    session: Active task session.

  Returns:
    The final summary string returned by the agent.
  """
  print(
      f"task: workdir {session.workdir}, model {MODEL},"
      f" {len(session.mcp_servers)} MCP server(s)",
      file=sys.stderr,
  )
  return await Agent(session).run()


if __name__ == "__main__":
  taskbridge.run(solve)
