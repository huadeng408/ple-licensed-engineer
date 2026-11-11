# PLE Licensed-Engineer Agent Harness (`ple-licensed-engineer`)

A lean, multimodal, checkpoint-protected physical-engineering agent harness for **Physical-Engineering's Last Exam (PLE, `arXiv:2610.30537`)**, built atop Google's official `custom-openai` reference harness (`serve.py` / `taskbridge.py`) and engineered around the benchmark's dual-paradigm grading mechanics:

1. **Co-Authored Engineering Optimization & Requirement-Clause Grounding (`15/29` Dev Tasks)**:
   $$s = 0.4 \cdot p + 0.6 \cdot G \cdot O', \qquad O' = \text{clip}\left(\frac{O - O_{\text{naive}}}{1 - O_{\text{naive}}}, 0, 1\right)$$
   where $p \in [0, 1]$ is the fraction of general constraints and explicit **Requirement-Clause Grounding** credits satisfied, $G \in \{0, 1\}$ is the binary **Hard-Constraint Gate** ($G=1$ if and only if 100% of safety, code-compliance, spatial clash-free, and schema constraints hold; breaking even one hard constraint forces $G=0$ and caps the item score at $0.40$), and $O' \in [0, 1]$ is the **Normalized Objective Credit** relative to a naive feasible baseline $O_{\text{naive}}$ (which receives $O' = 0$).
2. **Real-World Professional Work-Sample Rubrics & MCP World State (`14/29` Rubric Tasks, `12/29` MCP Tasks, `12/29` ZIP Archive Tasks)**:
   Evaluated across multi-file container deliverables (`.zip`, `.ifc`, `.dxf`, `.xlsx`, `.docx`, `.pdf`, `.json`, `.csv`, `.py`, `.md`) and stateful MCP applications (`/app/tools/.apps_data/`).

---

## Architecture: Lean Multimodal Checkpoint Harness (`ADR-0011`)

While preserving 100% of `custom-openai`'s Streamable-HTTP MCP transport (`connect_mcp()`), adaptive protocol fallback (`remedy()`), Responses API streaming (`respond()`), and strict container isolation (`egress: none`), `ple-licensed-engineer` introduces five domain-specific systems:

1. **Phase 1 — Multimodal Plan Inspection (`inspect_visual_artifact`), Deep Archive/BIM Recon (`taskbridge.workspace_recon()`), & Compaction-Immune `pin_basis_of_design`**:
   - Executes a deterministic read-only container inventory of `/filesystem` and `/app` before Turn 1, sorting files by recency (`mtime`), listing all `.zip` archive members, and previewing `.csv`, `.json`, `.yaml`, `.ifc` (`ISO-10303-21`), and `.dxf` headers while excluding internal MCP server packages and `/tmp/_ple_incumbent_best/`.
   - Renders container `.pdf` engineering drawings (`pypdfium2`), `.dxf` CAD layouts (2D entity rasterizer over `LINE`, `LWPOLYLINE`, `CIRCLE`, `ARC`, `TEXT`), and `.png`/`.jpg` schematics into bounded (`<= 1568 px`) PNG attachments via **`inspect_visual_artifact`**, with automatic text-only fallback if an endpoint does not accept vision inputs.
   - Locks the **Pinned Basis of Design (BoD)** (`self.pinned_bod`) via **`pin_basis_of_design`**, anchoring required deliverable paths, $G=1$ inequalities, the **Requirement-Clause Grounding Register** (`ASCE 7-22`, `ACI 318-19`, `AISC 360`, `IEEE 1584`, `NEC`, `ASHRAE 90.1`, `NUREG-1824`), parametric $O'$ sweep targets, and SI/Imperial unit invariants across context compactions.
2. **Phase 2 & 3 — Lean `Terminus-2` Tool Surface, Full-File 64 MiB CAD/BIM Schema Verification, & Physical $G=1$ Incumbent Checkpoints (`checkpoint_incumbent`)**:
   - Delegates numerical optimization and linear algebra directly to full container Python solvers (`scipy.optimize`, `numpy`, `sympy`, `ifcopenshell`, `ezdxf`, `openpyxl`) via **`verify_calculations`** and **`bash`**, avoiding toy JSON matrix tool bottlenecks.
   - Performs full-file (`<= 64 MiB`, zero 256 KB truncation) structural verification in **`verify_deliverable_schema`** and **`taskbridge.audit_deliverables()`**, including STEP quote-safe `.ifc` `#id` entity reference validation, `.dxf` `SECTION`/`ENTITIES`/`EOF` checks, recursive `.zip` inner-member inspection, and direct file-backed OpenXML worksheet/document validation.
   - Snapshots verified $G=1$ baseline and improved candidate deliverables into `/tmp/_ple_incumbent_best/` via **`checkpoint_incumbent`** (and automatically on `verify_deliverable_schema`), rolling back automatically in `finish()` or upon fix-round exhaustion if a late $O'$ edit truncates or corrupts a deliverable.
3. **Phase 4 — Adversarial Deceptive-Summary Peer Review with Read-Only MCP Filtering (`run_verification_gate()`)**:
   - Spawns an isolated Fresh-Context Verifier equipped with `bash`, `read_file`, `inspect_visual_artifact`, `verify_deliverable_schema`, `verify_calculations`, and **read-only filtered** MCP tools (`is_readonly_mcp_tool()` blocks mutating email/ticket/database side effects during verification while preserving full world-state read visibility).
4. **Signed-Thinking Sanitization & Immutable-Prefix Cache-Aligned Compaction**:
   - Strips unsigned `reasoning_content` / `thinking_blocks` on `length`-truncated turns before appending to history and recovers automatically on HTTP `400` signature errors (`drop_unsigned_thinking_history()`).
   - Preserves `messages[:2]` (`system` + initial `user` recon prompt) 100% byte-identical during `compact()`, maintaining strict `system -> user -> assistant -> user -> assistant` alternation and KV prefix-cache alignment.

---

## Official Open-Weight Pareto Target & Local Validation

`submission.yaml` targets the official Google-sponsored open-weight model **`google-cloud/gemma-4-26b`** (`Gemma 4 26B A4B`) for the Stage 2 Low-Cost **Performance-Cost Pareto Frontier**, while staying 100% model-agnostic across all 8 official PLE models via `MODEL_NAME` / `MODEL_BASE_URL`.

```bash
# 1. Validate harness against official PLE kit specification
PYTHONPATH=/path/to/ple python -m kit validate /path/to/ple-licensed-engineer
# Output: ok: ple-licensed-engineer, google-cloud/gemma-4-26b (Gemma 4 26B A4B); egress none

# 2. Run deterministic unit tests & 32-case deceptive-summary verification benchmark
pytest test_serve_seam.py -v
python run_verification_benchmark.py
```

## License

Apache License 2.0 (see [LICENSE](LICENSE)).
