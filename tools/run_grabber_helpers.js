/* Test bridge: load pilot/overlay/grabber.js into a jsdom Window and
 * run a small set of helper assertions against a real HTML string.
 *
 * Used by tests/agent/test_angular_label_capture.py. The Python test
 * spawns this via Node and asserts the JSON it prints to stdout.
 *
 * Inputs (env / argv):
 *   argv[2]   path to an HTML fixture (page-filled.html or a fragment)
 *   argv[3]   "probe" -- runs the canned probe set
 *
 * Output: a single JSON line with {probe_name: result, ...}.
 *
 * Required deps: jsdom -- install once via
 *   npm install --no-save --prefix tmp/jsdom-deps jsdom
 */
"use strict";
const fs = require("fs");
const path = require("path");
const { JSDOM } = require(path.join(
  __dirname, "..", "tmp", "jsdom-deps", "node_modules", "jsdom"));

function loadGrabberInto(window) {
  const grabberPath = path.join(
    __dirname, "..", "pilot", "overlay", "grabber.js");
  const src = fs.readFileSync(grabberPath, "utf8");
  // Flag the IIFE test bridge BEFORE evaluating grabber.js.
  window.__cp_test_bridge = true;
  // Provide a no-op crypto.randomUUID for the WI-02 event id generator.
  if (!window.crypto) window.crypto = {};
  if (!window.crypto.randomUUID) {
    window.crypto.randomUUID = () => "uuid-" + Math.random().toString(36).slice(2, 10);
  }
  // jsdom Intl.DateTimeFormat doesn't expose resolvedOptions().timeZone
  // consistently; provide a stub so _controlMetadata doesn't throw.
  try { window.Intl.DateTimeFormat().resolvedOptions(); }
  catch (e) {
    window.Intl.DateTimeFormat = function () {
      return { resolvedOptions: () => ({ timeZone: "UTC" }) };
    };
  }
  // queueMicrotask exists in jsdom; if not, polyfill.
  if (typeof window.queueMicrotask !== "function") {
    window.queueMicrotask = (fn) => Promise.resolve().then(fn);
  }
  // Evaluate grabber.js in the window context.
  window.eval(src);
  return window.__cp_helpers;
}

function run(htmlPath) {
  const html = fs.readFileSync(htmlPath, "utf8");
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "http://localhost/transfer-artwork" });
  const window = dom.window;
  const document = window.document;
  const helpers = loadGrabberInto(window);
  if (!helpers) {
    console.error("__cp_helpers not exposed; bridge failed");
    process.exit(2);
  }

  const out = {};

  // ----- Target Model mat-select (mirrors trace.jsonl click #4) -----
  // page-filled.html line 402: <mat-select id="mat-select-1" role="listbox">
  // The captured click target in the trace was the inner div at css_path:
  //   mat-select#mat-select-1 > div > div:nth-of-type(1)
  // We mimic that by starting from the inner div and asserting the
  // resolver lifts us up to <mat-select>.
  const matSelect1 = document.getElementById("mat-select-1");
  out.has_mat_select_1 = !!matSelect1;
  if (matSelect1) {
    // The exact div the user clicked is .mat-select-trigger > div (the
    // first inner div). Pick that as the raw click target.
    const innerDiv = matSelect1.querySelector(".mat-select-trigger > div");
    out.inner_click_tag = innerDiv ? innerDiv.tagName.toLowerCase() : null;
    const resolved = helpers.resolveSemanticTarget(innerDiv);
    out.resolved_tag = resolved ? resolved.tagName.toLowerCase() : null;
    out.resolved_id = resolved ? resolved.id : null;
    out.target_model_acc_name = helpers.getAccessibleName(matSelect1).trim();
    out.target_model_current_value = helpers.currentDisplayValue(matSelect1);
    const fp = helpers.fingerprint(matSelect1);
    out.target_model_fp_acc_name = fp.accessible_name;
    out.target_model_fp_current_value = fp.current_value;
    out.target_model_fp_tag = fp.tag;
    out.target_model_fp_role = fp.role;
  }

  // ----- Source Year mat-select (page-filled.html line 306, id mat-select-0) -----
  const matSelect0 = document.getElementById("mat-select-0");
  out.has_mat_select_0 = !!matSelect0;
  if (matSelect0) {
    out.source_year_acc_name = helpers.getAccessibleName(matSelect0).trim();
    out.source_year_current_value = helpers.currentDisplayValue(matSelect0);
  }

  // ----- Country/Region ng-multiselect-dropdown (page-filled.html line 637) -----
  // We can't query by class trivially; pick the second ng-multiselect-dropdown
  // (page-filled.html has Filter first at 463, then Country/Region at 637).
  const multiSelects = document.querySelectorAll("ng-multiselect-dropdown");
  out.multi_select_count = multiSelects.length;
  if (multiSelects.length >= 2) {
    const country = multiSelects[1];
    out.country_acc_name = helpers.getAccessibleName(country).trim();
    out.country_current_value = helpers.currentDisplayValue(country);
    // Cross-check: the FIRST multiselect ("Filter:") must NOT leak its
    // label into the Country/Region capture.
    const filt = multiSelects[0];
    out.filter_acc_name = helpers.getAccessibleName(filt).trim();
    out.filter_current_value = helpers.currentDisplayValue(filt);
  }

  // ----- Search input detection (B5) -----
  // The Source Artwork search input at line 869 has placeholder="Search".
  const srchInput = document.getElementById("mat-input-0");
  out.has_search_input = !!srchInput;
  if (srchInput) {
    out.search_input_is_search_like = helpers.isSearchLikeInput(srchInput);
  }
  // Inside the ng-multiselect-dropdown there's an input
  // aria-label="multiselect-search" + placeholder="Search". Find the one
  // inside the second multiselect.
  if (multiSelects.length >= 2) {
    const msInput = multiSelects[1].querySelector(
      "input[aria-label='multiselect-search']");
    out.has_country_search_input = !!msInput;
    if (msInput) {
      out.country_search_is_search_like = helpers.isSearchLikeInput(msInput);
    }
  }

  // ----- mat-checkbox label discovery + current_value -----
  // page-filled.html line 1005: <mat-checkbox id="mat-checkbox-2">
  // (this checkbox is the FIRST checked row in the source list).
  const cb2 = document.getElementById("mat-checkbox-2");
  out.has_mat_checkbox_2 = !!cb2;
  if (cb2) {
    out.mat_checkbox_2_current_value = helpers.currentDisplayValue(cb2);
  }

  // ----- mat-option panel collection (B4) -----
  // page-filled.html as a static snapshot doesn't have an open
  // cdk-overlay-pane, so simulate one with a single mat-option and
  // assert _collectMatSelectOptions picks it up.
  const overlayContainer = document.createElement("div");
  overlayContainer.className = "cdk-overlay-container";
  const pane = document.createElement("div");
  pane.className = "cdk-overlay-pane";
  pane.id = "cdk-overlay-fake";
  pane.innerHTML =
    '<mat-option id="mat-option-fake-1" class="mat-option" role="option">'
    + '<span class="mat-option-text">2024</span></mat-option>'
    + '<mat-option id="mat-option-fake-2" class="mat-option" role="option">'
    + '<span class="mat-option-text">2023</span></mat-option>';
  overlayContainer.appendChild(pane);
  document.body.appendChild(overlayContainer);
  if (matSelect1) {
    matSelect1.setAttribute("aria-owns", "cdk-overlay-fake");
    const opts = helpers.collectMatSelectOptions(matSelect1);
    out.collected_options = opts;
  }

  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) {
  const htmlPath = process.argv[2];
  if (!htmlPath) {
    console.error("usage: node run_grabber_helpers.js <html-fixture>");
    process.exit(2);
  }
  try {
    run(htmlPath);
  } catch (e) {
    console.error("bridge error:", e && e.stack || e);
    process.exit(1);
  }
}
