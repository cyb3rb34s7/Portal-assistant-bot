# Country multi-select (server-side search) — agent-browser verification

Portal: http://localhost:5188  Asset: A-9001 (draft)  Picker: `multiselect-country`
Picker is server-backed: popover list comes from `GET /api/countries?q=<query>`
(~300ms artificial delay = spinner). Full list = 54 countries, alphabetical,
Zimbabwe (id=`zw`) last. Viewport innerHeight ~569px.

## Test 1 — popover renders, Zimbabwe below the fold
- Logged in (operator/secret), opened /asset/A-9001.
- `multiselect-country-toggle` present.
- Clicked toggle, waited for `[data-testid='multiselect-country-popover']`.
- eval result: `{popover:true, optionCount:54, popoverBottom:571, zwExists:true,
  zwTop:2007, zwBelowPopoverFold:true}`
- => popover renders all 54 options; Zimbabwe at y=2007 is far below the
  popover fold (bottom 571). PASS. (screenshot-1779902080634.png)

## Test 2 — server-side search path (case B analog)
- Filled `multiselect-country-search` with "zim".
- Waited for `[data-testid='multiselect-country-checkbox-zw']` to become VISIBLE
  (this is the runner's gate — no fixed sleep; it rides out the 300ms server fetch).
- eval result: `{filteredCount:1, labels:["Zimbabwe"], zwVisible:true,
  zwInViewport:true, zwTop:373}`
- Clicked `multiselect-country-checkbox-zw`; waited for chip.
- eval result: `{chipPresent:true, chipText:"Zimbabwe"}`
- Network log confirms `GET /api/countries?q=zim 200` fired and was waited out.
- => Typing the LABEL "zim" narrowed the server list to exactly Zimbabwe, which
  surfaced into the viewport; click produced a Zimbabwe chip. PASS.
  (screenshot-1779902107287.png)

## Test 3 — scroll-into-view path (case A analog, no search)
- Cleared the search (Ctrl+A / Delete) -> waited for full 54-item list to return.
- eval before scroll: `{zwTop:2016, innerHeight:569, zwInViewport:false}`
  (Zimbabwe off-viewport).
- `scrollIntoView({block:'center'})` on the zw checkbox (mirrors the runner's
  `scroll_into_view_if_needed()`).
- eval after scroll: `{zwTop:278, zwInViewportAfterScroll:true}`.
- Clicked checkbox; chip appeared: `{chipPresent:true, chipText:"Zimbabwe"}`.
- => Off-viewport target reached via scroll-into-view then click. PASS.
  (screenshot-1779902240143.png)

## Conclusion
The PORTAL supports both reach mechanisms the runner uses:
  - search-to-narrow with a visibility-gated wait that rides out the server
    spinner (case B), and
  - scroll-into-view for an off-viewport option in the un-filtered list (case A).
The RUNNER's strategy selection + visibility wait + label-fill + clear-between-items
is proven separately by unit tests (tests/agent/test_set_selection_reach.py).
