# PLE Licensed-Engineer Agent Harness (`ple-licensed-engineer`)

A multi-stage, verification-gated physical-engineering agent harness for **Physical-Engineering's Last Exam (PLE, `arXiv:2610.30537`)**, built atop Google's official `custom-openai` reference harness (`serve.py` / `taskbridge.py`) and explicitly engineered around the benchmark's dual-paradigm grading mechanics:

1. **Co-Authored Engineering Optimization & Requirement-Clause Grounding (`15/29` Dev Tasks)**:
   $$s = 0.4 \cdot p + 0.6 \cdot G \cdot O', \qquad O' = \text{clip}\left(\frac{O - O_{\text{naive}}}{1 - O_{\text{naive}}}, 0, 1\right)$$
   where $p \in [0, 1]$ is the fraction of general constraints and explicit **Requirement-Clause Grounding** credits satisfied, $G \in \{0, 1\}$ is the binary **Hard-Constraint Gate** ($G=1$ if and only if 100% of safety, code-compliance, spatial clash-free, and schema constraints hold; breaking even one hard constraint zeros out the $0.6 \cdot G \cdot O'$ term and caps the item score at $0.40$), and $O' \in [0, 1]$ is the **Normalized Objective Credit** relative to a naive feasible baseline $O_{\text{naive}}$ (which receives $O' = 0$).
2. **Real-World Professional Work-Sample Rubrics & MCP World State (`14/29` Rubric Tasks, `12/29` MCP Tasks, `12/29` ZIP Archive Tasks)**:
   Evaluated across multi-file container deliverables (`.zip`, `.ifc`, `.dxf`, `.xlsx`, `.docx`, `.pdf`, `.json`, `.csv`, `.py`, `.md`) and stateful MCP applications (`/app/tools/.apps_data/`).

---

## Architecture: 4-Phase Licensed-Engineer Extensions over `custom-openai` (`4,309 LOC` Total)

While preserving 100% of `custom-openai`'s Streamable-HTTP MCP transport (`connect_mcp()`), adaptive protocol fallback (`remedy()`), ephemeral cache breakpoint placement (`marked()`), and rolling conversation compaction (`compact()`), `ple-licensed-engineer` (`serve.py`: `2,477 LOC`, `taskbridge.py`: `1,832 LOC`) introduces four domain-specific engineering systems:

1. **Phase 1 — Deep Archive/BIM Workspace Reconnaissance (`taskbridge.workspace_recon()`) & Compaction-Immune `pin_basis_of_design`**:
   - Executes a deterministic read-only container inventory of `/filesystem` and `/app` before Turn 1, sorting files by recency (`mtime`), listing `.zip` archive members (`zipfile`), and previewing `.csv`, `.json`, `.yaml`, `.ifc` (`ISO-10303-21`), and `.dxf` headers while filtering internal MCP server packages.
   - Exposes the native **`pin_basis_of_design`** tool to lock the **Pinned Basis of Design (BoD)** (`self.pinned_bod`): (a) required deliverable paths/schemas, (b) Hard-Constraint Gate ($G=1$) inequalities, (c) the **Requirement-Clause Grounding Register** citing workspace codes/standards (`ASCE 7-22`, `ACI 318-19`, `AISC 360`, `IEEE 1584`, `NEC`, `ASHRAE 90.1`, `NUREG-1824`), (d) the **Normalized Objective Optimization Plan ($O' > O_{\text{naive}}$)**, and (e) SI/Imperial unit invariants. `self.pinned_bod` is immune to context compaction (`compact()`) and is automatically injected into Phase 4 verification.
2. **Phase 2 — Five Native Pure-Python & Container Engineering Verification Tools**:
   - **`pin_basis_of_design`**: Persists the compaction-immune Basis of Design register across long-horizon trajectories.
   - **`solve_linear_system`**: Pure-Python Gaussian elimination with partial pivoting ($A\mathbf{x} = \mathbf{b}$ up to $64 \times 64$) returning exact nodal unknowns and infinity-norm residual $\|A\mathbf{x}-\mathbf{b}\|_\infty \le 10^{-12}$.
   - **`check_engineering_invariants`**: Vectorized AST-sandboxed physical formula and code inequality evaluator ($g_i(\mathbf{x}) \le 0$) returning exact safety margins and `G_gate = 1` vs. `0` status.
   - **`verify_deliverable_schema`**: Deep container deliverable validator checking `.zip` member integrity (`testzip()` + recursive inner `.xlsx`/`.docx`/`.ifc`/`.dxf` inspection), `.ifc` `ISO-10303-21` headers + null `$` geometry + dangling `#id` entity references, `.dxf` `SECTION`/`ENTITIES`/`EOF` structure, OpenXML worksheet/document streams, and `NaN`/`Inf`/`TODO` defects.
   - **`verify_calculations`**: Executes full Python 3 engineering solver scripts (`numpy`, `scipy`, `sympy`, `pandas`, `ifcopenshell`, `openpyxl`, `python-docx`, `reportlab`) inside `/tmp/_ple_verify_calc.py`.
3. **Phase 3 — Automated Deep Container Deliverable & Archive Gate (`taskbridge.audit_deliverables()`)**:
   - Runs `AUDIT_SCRIPT` inside the task container prior to completion, recursively auditing `submission.zip` archives, `.ifc` dangling entity references, `.dxf` `ENTITIES` blocks, OpenXML `xl/worksheets/sheet*.xml` and `word/document.xml` streams, PDF `%PDF-` headers, and JSON non-finite numbers.
4. **Phase 4 — Token-Isolated, MCP- & Solver-Equipped Fresh-Context Peer Review (`run_verification_gate()`)**:
   - Reserves `VERIFY_SHARE = 0.22` of the 360-minute (`21,600s`) task deadline budget and spawns an independent Verifier Agent in a clean context window containing only the original task prompt, the Pinned Basis of Design (`self.pinned_bod`), the Phase 3 container audit report, and the worker's proposed summary while granted full read-only access to `bash`, `read_file`, all 12/12 MCP world servers, and the native engineering verification tools (`solve_linear_system`, `check_engineering_invariants`, `verify_deliverable_schema`, `verify_calculations`).
   - Replies `PASS` or returns a numbered defect list for up to `MAX_FIX_ROUNDS = 2` targeted repair iterations.

---

## Local Verification & Evaluation

```bash
# 1. Validate harness against official PLE kit specification
PYTHONPATH=/path/to/ple python -m kit validate /path/to/ple-licensed-engineer
# Output: ok: ple-licensed-engineer, google-cloud/claude-opus-5-5 (Claude Opus 5.5); egress none

# 2. Run the 32-case multi-discipline fault-injection benchmark
python run_verification_benchmark.py
```

## License

Apache License 2.0 (see [LICENSE](LICENSE)).
