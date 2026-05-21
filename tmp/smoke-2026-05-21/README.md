# 2026-05-21 sample portal smoke verification

Smoke check that the sample portal at `sample_portal/` still works as a
portal after the structural fix sprint. This is NOT a replay test of a
recorded skill -- it's a sanity check that the recording target itself
hasn't regressed.

Steps performed:

1. `npm run dev` in `sample_portal/` -- vite server on port 5188.
   Stdout in `portal-stdout.log`.
2. `agent-browser open http://localhost:5188/catalog` -- portal
   redirected to `/login` (auth gate).
3. Snapshot 2 (`02-login-snapshot.txt`) confirmed login form
   (username + password fields, disabled "Sign in" button).
4. `agent-browser fill [data-testid='input-username'] smoke-operator`
   + `fill input-password pw` + `click btn-login`. Logged in
   (Login.jsx says any non-empty creds work in sample mode).
5. URL after login: `http://localhost:5188/catalog` (snapshot 7).
6. `agent-browser fill input-catalog-search A-9003` +
   `click btn-catalog-search`. Search results page rendered ONE row
   ("Spring Cup Preview") after the sample portal's deliberate
   debounce + backend lag (sample_portal/mock_backend.js).
7. `agent-browser click btn-open-A-9003`. URL navigates to
   `/asset/A-9003` (snapshot 13).
8. `agent-browser fill input-asset-title "Spring Cup Preview - Smoke 2026-05-22"`
   + `click btn-save`. Save button cycles from disabled "Saving..." back
   to enabled "Save" -- save completed.
9. Final screenshot at `18-final-asset-page.png` captures the asset
   page with the new title rendered into the form.
10. Snapshot 17 (`17-after-save-snapshot.txt`) confirms the title
    textbox value matches the new operator input.

## Outcome

PASS. The sample portal regression-free:

- catalog -> search -> open works (the click-then-navigate flow
  WI-08 modeled);
- asset detail title fill + save works (the fill_submit + assert_after
  flow WI-15 + WI-27 modeled);
- save status spinner cycles (the WI-10 readiness signal).

## Artifacts

| File | What |
|---|---|
| `01-initial-url.txt` | URL right after open (portal redirected to /login) |
| `02-login-snapshot.txt` | Login page accessibility tree |
| `03-fill-username.txt` .. `05-click-login.txt` | Login command outputs |
| `06-after-login-url.txt` | URL after login (catalog) |
| `07-after-login-snapshot.txt` | Catalog page tree |
| `08-fill-search.txt` .. `09-click-search.txt` | Search command outputs |
| `10-search-results-snapshot.txt` | Snapshot during the sample portal's deliberate search lag |
| `11-results-snapshot.txt` | Snapshot with 1 result row |
| `12-click-open.txt` | Click-open command output |
| `13-asset-url.txt` | URL on asset detail (/asset/A-9003) |
| `14-asset-snapshot.txt` | Asset detail tree pre-edit |
| `15-fill-title.txt` .. `16-click-save.txt` | Title edit + save commands |
| `17-after-save-snapshot.txt` | Asset detail tree post-save (new title visible) |
| `18-final-asset-page.png` | Screenshot of final state |
| `19-verify-api.json` | `/api/assets/A-9003` API probe (returns `unauthorized` -- session cookie lives in the browser, not in curl; the form snapshot in step 17 is the authoritative evidence) |
| `portal-stdout.log` | vite + mock_backend stdout |
| `portal-root.html` | The `/` HTML fetched via curl before the smoke started |

## Notes

- The Replay UI / FastAPI server was NOT started during this smoke
  (`python -m pilot serve`) -- only the portal under test. The smoke
  exercises the portal directly via `agent-browser`, not via the
  curationpilot runner.
- The Idempotency-Key shim is INSTALLED only by the runner (not by the
  portal itself), so it's not exercised here. Its end-to-end behavior
  is covered by `tests/agent/test_idempotency.py`.
