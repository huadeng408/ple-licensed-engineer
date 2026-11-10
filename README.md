# PLE Licensed-Engineer Agent Harness (`ple-licensed-engineer`)

A multi-stage, verification-gated physical-engineering agent harness for **Physical-Engineering's Last Exam (PLE)**, built atop Google's official `custom-openai` reference harness (`serve.py` / `taskbridge.py`) and tailored around the benchmark's exact constraint-and-objective grading structure:

$$s = 0.4 \cdot p + 0.6 \cdot G \cdot O'$$

where $p \in [0, 1]$ is the fraction of general constraints met, $G \in \{0, 1\}$ is the strict **Hard-Constraint Gate** ($G=1$ if and only if 100% of safety, code-compliance, spatial clash-free, and schema constraints hold; breaking even one hard constraint zeros out the $0.6 \cdot G \cdot O'$ term and caps the item score at $0.40$), and $O' = \text{clip}\left(\frac{O - O_{\text{naive}}}{1 - O_{\text{naive}}}, 0, 1\right)$ measures optimization quality relative to the certified optimum.

---

## Architecture: 4-Phase Licensed-Engineer Extensions over `custom-openai`

While inheriting `custom-openai`'s Streamable-HTTP MCP transport (`connect_mcp()`), adaptive protocol fallback (`remedy()`), ephemeral cache breakpoint placement (`marked()`), and rolling conversation compaction (`compact()`), `ple-licensed-engineer` introduces four domain-specific extensions:

1. **Phase 1 — Automated Workspace Reconnaissance (`taskbridge.workspace_recon()`) & $G$-First Basis of Design Register**:
   - Executes a deterministic read-only container inventory of `session.workdir`, `/app/files`, `/app/output`, `/filesystem`, and `/app/tools/.apps_data` (excluding internal `/app/tools` MCP server packages) before the first LLM turn and injects the discovered file/tool map directly into the initial prompt, eliminating exploratory `bash`/`ls` turns.
   - Enumerates every required deliverable path, target format (`.ifc`, `.xlsx`, `.docx`, `.pdf`, `.zip`, `.py`, `.md`), Hard Constraints ($G$), Optimization Objective ($O'$), and SI vs. US Customary unit conversions.
2. **Phase 2 — Strictly Feasible Numerical Solver (`verify_calculations`)**:
   - Exposes a dedicated `verify_calculations` tool alongside `bash`, `read_file`, `write_file`, `edit_file`, and MCP tools (`<server>__<tool>`, `mcp_find`, `mcp_call`).
   - Executes deterministic Python verification scripts (`numpy`, `scipy`, `sympy`, `pandas`, `ifcopenshell`, `openpyxl`, `python-docx`, `reportlab`) inside `/tmp/_ple_verify_calc.py` with safety margin $\epsilon = 10^{-4}$ before writing deliverables.
3. **Phase 3 — Tier-Prioritized Container Deliverable & Archive Gate (`taskbridge.audit_deliverables()`)**:
   - Runs `taskbridge.audit_deliverables()` inside the task container, prioritizing `/output/`, `/submission/`, and `.apps_data` deliverables at Tier 0 by modification time so target packages such as `/filesystem/.../output/submission.zip` are never truncated behind pre-existing reference files.
   - Verifies non-zero byte sizes, ZIP archive integrity (`zipfile.testzip()` and zero-byte member detection), IFC4 `ISO-10303-21` headers and non-null `$` geometry attributes, OpenXML (`.xlsx`/`.docx`) container validity, and PDF headers.
4. **Phase 4 — MCP-Aware, Token-Isolated Fresh-Context Peer Review (`run_verification_gate()`)**:
   - Reserves `VERIFY_SHARE = 0.22` of the task deadline budget and spawns an independent Verifier Agent in a clean context window containing only the task prompt, the Phase 3 container audit report, and the worker's proposed summary while preserving `self.prompt_tokens` and `self.max_output` across context swaps and checking `compact()` immediately after injecting repair feedback.
   - Retains all read-only, solver, and MCP inspection tools (`bash`, `read_file`, `verify_calculations`, `mcp_find`, `mcp_call`, and `<server>__<tool>`), forces a deterministic final verdict if all 6 verification turns invoke tools, and triggers up to `MAX_FIX_ROUNDS = 2` targeted repair iterations when defects are found.

---

## Local Verification & Evaluation

```bash
# 1. Validate submission.yaml and harness layout
python -m kit validate ../competitions/ple/ple-harness

# 2. Run kit check against ple-harness
python -m kit check ../competitions/ple/ple-harness

# 3. Run dev-set evaluation (Stage 2 / GCP credit grant)
python eval/run.py ../competitions/ple/ple-harness
```

## License

Apache License 2.0 (see [LICENSE](LICENSE)).
