# Agent-browser UI verification — Angular sample portal (2026-06-02)

Drove the full transfer-artwork flow on http://localhost:5189/transfer-artwork via the `agent-browser` CLI (no CurationPilot runner involved — pure portal regression check).

| # | Action | Result |
|---|--------|--------|
| 1 | Login (operator/test123) | ✅ overlay dismissed |
| 2 | mat-select Year = 2024 | ✅ display="2024" |
| 3 | mat-select Make = Samsung | ✅ display="Samsung" |
| 4 | mat-select Model = 24_BOMRB_8K (cascading) | ✅ display="24_BOMRB_8K" |
| 5 | ng-multiselect-dropdown Country → search "canada" → click Canada | ✅ chip = "Canada x" |
| 6 | Show Data button | ✅ left list populated: SAM-F000000..005 |
| 7 | Check SAM-F000000 + SAM-F000001 → Transfer Right | ✅ right list = [SAM-F000000, SAM-F000001] |

Final screenshot: `final.png`. Portal supports every interaction the runner depends on; no portal-side regression.
