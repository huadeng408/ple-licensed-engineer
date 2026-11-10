# PLE Licensed-Engineer Agent Harness (`ple-licensed-engineer`)

A multi-stage, verification-gated physical-engineering agent harness for **Physical-Engineering's Last Exam (PLE)**, built atop Google's official `custom-openai` reference harness (`serve.py` / `taskbridge.py`) and tailored around the benchmark's exact constraint-and-objective grading structure:

$$s = 0.4 \cdot p + 0.6 \cdot G \cdot O'$$

where $p \in [0, 1]$ is the fraction of general constraints met, $G \in \{0, 1\}$ is the strict **Hard-Constraint Gate** ($G=1$ if and only if 100% of safety, code-compliance, spatial clash-free, and schema constraints hold; breaking even one hard constraint zeros out the $0.6 \cdot G \cdot O'$ term and caps the item score at $0.40$), and $O' = \text{clip}\left(\frac{O - O_{\text{naive}}}{1 - O_{\text{naive}}}, 0, 1\right)$ measures optimization quality relative to the certified optimum.

---

## Architecture: 4-Phase Licensed-Engineer Extensions over `custom-openai`

While inheriting `custom-openai`'s Streamable-HTTP MCP transport (`connect_mcp()`), adaptive protocol fallback (`remedy()`), ephemeral cache breakpoint placement (`marked()`), and rolling conversation compaction (`compact()`), `ple-licensed-engineer` introduces four domain-specific extensions:

1. **Phase 1 — Automated Workspace Reconnaissance (`taskbridge.workspace_recon()`) & $G$-First Basis of Design Register**:
   - Executes a deterministic read-only container inventory of `/app` and `/filesystem` before the first LLM turn and injects the discovered file/tool map directly into the initial prompt, eliminating exploratory `bash`/`ls` turns.
   - Enumerates every required deliverable path, target format (`.ifc`, `.xlsx`, `.docx`, `.pdf`, `.zip`, `.py`, `.md`), Hard Constraints ($G$), Optimization Objective ($O'$), and SI vs. US Customary unit conversions.
2. **Phase 2 — Strictly Feasible Numerical Solver (`verify_calculations`)**:
   - Exposes a dedicated `verify_calculations` tool alongside `bash`, `read_file`, `write_file`, `edit_file`, and MCP tools (`<server>__<tool>`, `mcp_find`, `mcp_call`).
   - Executes deterministic Python verification scripts (`numpy`, `scipy`, `sympy`, `pandas`, `ifcopenshell`, `openpyxl`, `python-docx`, `reportlab`) inside `/tmp/_ple_verify_calc.py` with safety margin $\epsilon = 10^{-4}$ before writing deliverables.
3. **Phase 3 — Automated Container Deliverable & Archive Gate (`taskbridge.audit_deliverables()`)**:
   - Runs `taskbridge.audit_deliverables()` inside the task container to verify that all candidate deliverables exist, are non-empty, pass ZIP integrity (`zipfile.testzip()`), and carry valid IFC (`ISO-10303-21`) or Office headers.
4. **Phase 4 — Token-Isolated Fresh-Context Peer Review (`run_verification_gate()`)**:
   - Reserves `VERIFY_SHARE = 0.22` of the task deadline budget and spawns an independent Verifier Agent in a clean context window containing only the task prompt, the Phase 3 container audit report, and the worker's proposed summary while preserving `self.prompt_tokens` across context swaps.
   - Re-checks key numbers, unit conversions, and hard constraints ($G=1$). Replies `PASS` or returns a numbered defect list for up to `MAX_FIX_ROUNDS = 2` targeted repair iterations.

---

## Local Verification & Evaluation

```bash
# 1. Start the local model proxy (listening on 0.0.0.0:4000)
python ../local_model_proxy.py &
export MODEL_PROXY_URL=http://localhost:4000
export MODEL_PROXY_KEY=ple-local-key

# 2. Run kit check against ple-harness
uv run python -m kit check ../competitions/ple/ple-harness

# 3. Run dev-set evaluation
uv run python eval/run.py ../competitions/ple/ple-harness
```

## License

Apache License 2.0 (see [LICENSE](LICENSE)).
