/* CurationPilot grabber — framework-agnostic passive listener.
 *
 * Injected into every portal page via CDP
 * (Page.addScriptToEvaluateOnNewDocument). Runs in the page's main world.
 * Zero visible UI in listen mode — operator uses portal normally.
 *
 * Captures: click / change / submit / file-selected / navigation / key
 * Filters out: mousemove, scroll, hover, accidental key noise
 * Debounces: text input (single "change" event when user pauses typing)
 *
 * Posts to window.__pilotCapture(payload), which is a CDP Runtime.addBinding
 * exposing a Python-side callback. If the binding isn't present (e.g. when
 * loaded outside a pilot session), events are buffered to window.__pilotBuffer
 * and silently discarded on page unload.
 */

(function () {
  "use strict";
  if (window.__cp_grab_installed) return;
  window.__cp_grab_installed = true;

  var DEBUG = !!window.__cp_debug;
  var INPUT_DEBOUNCE_MS = 400;

  // ---- Transport -----------------------------------------------------------

  function post(payload) {
    try {
      var serialized = JSON.stringify(payload);
    } catch (e) {
      if (DEBUG) console.warn("[cp] serialize failed", e, payload);
      return;
    }
    if (typeof window.__pilotCapture === "function") {
      try {
        // expose_binding returns a Promise; we don't await it here, but
        // we do catch synchronous throws. Silent swallow is intentional
        // for production, but surfaces when window.__cp_debug is true.
        var ret = window.__pilotCapture(serialized);
        if (ret && typeof ret.catch === "function") {
          ret.catch(function (err) {
            if (DEBUG) console.warn("[cp] capture promise rejected", err);
          });
        }
      } catch (e) {
        if (DEBUG) console.warn("[cp] capture threw", e);
      }
    } else {
      (window.__pilotBuffer = window.__pilotBuffer || []).push(serialized);
    }
  }

  // ---- Fingerprinting ------------------------------------------------------

  function trim(s) {
    return (s == null ? "" : String(s)).replace(/\s+/g, " ").trim();
  }

  function getAccessibleName(el) {
    if (!el) return "";
    var explicit = el.getAttribute && el.getAttribute("aria-label");
    if (explicit) return trim(explicit);
    var labelledBy = el.getAttribute && el.getAttribute("aria-labelledby");
    if (labelledBy) {
      var ids = labelledBy.split(/\s+/);
      var parts = [];
      for (var i = 0; i < ids.length; i++) {
        var ref = document.getElementById(ids[i]);
        if (ref) parts.push(trim(ref.textContent));
      }
      if (parts.length) return parts.join(" ");
    }
    if (el.id) {
      var lab = document.querySelector("label[for='" + cssEscape(el.id) + "']");
      if (lab) return trim(lab.textContent);
    }
    var ancestorLabel = el.closest && el.closest("label");
    if (ancestorLabel) return trim(ancestorLabel.textContent);
    if (el.alt) return trim(el.alt);
    if (el.title) return trim(el.title);
    if (el.placeholder) return trim(el.placeholder);
    return trim(el.innerText || el.textContent || "").slice(0, 120);
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function computedRole(el) {
    if (!el) return null;
    var explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit;
    var tag = (el.tagName || "").toLowerCase();
    var implicit = {
      a: el.getAttribute && el.getAttribute("href") ? "link" : null,
      button: "button",
      input: (function () {
        var t = (el.getAttribute && el.getAttribute("type")) || "text";
        if (t === "submit" || t === "button") return "button";
        if (t === "checkbox") return "checkbox";
        if (t === "radio") return "radio";
        return "textbox";
      })(),
      textarea: "textbox",
      select: "combobox",
      option: "option",
      nav: "navigation",
      header: "banner",
      footer: "contentinfo",
      main: "main",
      dialog: "dialog",
    }[tag];
    return implicit || null;
  }

  function buildCssPath(el) {
    if (!el || !el.nodeType) return "";
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && parts.length < 8) {
      var seg = node.nodeName.toLowerCase();
      if (node.id) {
        seg += "#" + cssEscape(node.id);
        parts.unshift(seg);
        break;
      }
      var tid = node.getAttribute && node.getAttribute("data-testid");
      if (tid) {
        seg += "[data-testid='" + tid + "']";
        parts.unshift(seg);
        break;
      }
      var parent = node.parentElement;
      if (parent) {
        var sameTag = [].filter.call(parent.children, function (c) {
          return c.nodeName === node.nodeName;
        });
        if (sameTag.length > 1) {
          var idx = sameTag.indexOf(node) + 1;
          seg += ":nth-of-type(" + idx + ")";
        }
      }
      parts.unshift(seg);
      node = parent;
    }
    return parts.join(" > ");
  }

  function buildXPath(el) {
    if (!el || !el.nodeType) return "";
    var parts = [];
    var node = el;
    while (node && node.nodeType === 1 && parts.length < 10) {
      var seg = node.nodeName.toLowerCase();
      var parent = node.parentElement;
      if (parent) {
        var sameTag = [].filter.call(parent.children, function (c) {
          return c.nodeName === node.nodeName;
        });
        if (sameTag.length > 1) {
          seg += "[" + (sameTag.indexOf(node) + 1) + "]";
        }
      }
      parts.unshift(seg);
      node = parent;
    }
    return "/" + parts.join("/");
  }

  function findLandmark(el) {
    var cur = el;
    while (cur && cur.nodeType === 1) {
      var role = computedRole(cur);
      if (role === "dialog" || role === "navigation" || role === "main") {
        return trim(getAccessibleName(cur)) || role;
      }
      var tag = (cur.tagName || "").toLowerCase();
      if (tag === "section" || tag === "form" || tag === "dialog") {
        var n = trim(cur.getAttribute("aria-label") || "") ||
                trim((cur.querySelector("h1,h2,h3") || {}).textContent || "");
        if (n) return n;
      }
      var tid = cur.getAttribute && cur.getAttribute("data-testid");
      if (tid && /page-|panel-|modal/.test(tid)) return tid;
      cur = cur.parentElement;
    }
    return null;
  }

  function ancestorChain(el) {
    var out = [];
    var cur = el && el.parentElement;
    var depth = 0;
    while (cur && cur.nodeType === 1 && depth < 5) {
      out.push({
        tag: (cur.tagName || "").toLowerCase(),
        id: cur.id || null,
        testId: (cur.getAttribute && cur.getAttribute("data-testid")) || null,
        role: computedRole(cur),
        className: (cur.className && typeof cur.className === "string") ? cur.className : null,
      });
      cur = cur.parentElement;
      depth++;
    }
    return out;
  }

  function fingerprint(el) {
    if (!el || el.nodeType !== 1) return null;
    var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    return {
      test_id: (el.getAttribute && el.getAttribute("data-testid")) || null,
      element_id: el.id || null,
      name: (el.getAttribute && el.getAttribute("name")) || null,
      aria_label: (el.getAttribute && el.getAttribute("aria-label")) || null,
      role: computedRole(el),
      accessible_name: trim(getAccessibleName(el)) || null,
      text: trim(el.innerText || el.textContent || "").slice(0, 200) || null,
      placeholder: (el.getAttribute && el.getAttribute("placeholder")) || null,
      tag: (el.tagName || "").toLowerCase(),
      input_type: (el.tagName === "INPUT" && (el.getAttribute("type") || "text")) || null,
      css_path: buildCssPath(el),
      xpath: buildXPath(el),
      ancestor_chain: ancestorChain(el),
      landmark: findLandmark(el),
      bbox: rect
        ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        : null,
      frame_path: [],
      in_shadow_root: !!(el.getRootNode && el.getRootNode().host),
    };
  }

  // ---- Interaction detection ----------------------------------------------

  function isInteractable(el) {
    if (!el || el.nodeType !== 1) return false;
    var tag = (el.tagName || "").toLowerCase();
    if (["button", "a", "input", "select", "textarea", "option", "label"].indexOf(tag) !== -1) return true;
    var role = computedRole(el);
    if (role && ["button", "link", "tab", "menuitem", "option", "checkbox", "radio", "textbox"].indexOf(role) !== -1) return true;
    if (el.onclick) return true;
    // Styled div-as-button — has cursor:pointer
    try {
      var cs = window.getComputedStyle(el);
      if (cs && cs.cursor === "pointer") return true;
    } catch (e) {}
    return false;
  }

  function closestInteractable(el) {
    var cur = el;
    var depth = 0;
    while (cur && cur.nodeType === 1 && depth < 5) {
      if (isInteractable(cur)) return cur;
      cur = cur.parentElement;
      depth++;
    }
    return el;
  }

  // ---- Event hooks ---------------------------------------------------------

  document.addEventListener(
    "click",
    function (e) {
      var target = closestInteractable(e.target);
      if (!target) return;
      // Flush any pending text input debounce BEFORE the click is
      // recorded, so order is fill→click, not click→fill.
      if (pendingInputEl && pendingInputEl !== target) flushPendingInput();
      // Clicks on form inputs fire "change" on the input — dedupe
      var tag = (target.tagName || "").toLowerCase();
      if (
        tag === "input" &&
        ["checkbox", "radio"].indexOf(target.type) === -1 &&
        target.type !== "button" &&
        target.type !== "submit" &&
        target.type !== "file"
      ) {
        return;
      }
      post({
        kind: "click",
        fingerprint: fingerprint(target),
        page_url: location.href,
      });
    },
    true
  );

  // Debounced input capture — one event per field-value-settle.
  // We track a single pending input at a time. Moving to a different
  // element (click, focus, another field input) flushes the pending one
  // FIRST so the captured value is the committed value, not a later
  // reset value (e.g. after a form submit resets state).
  var pendingInputEl = null;
  var pendingInputTimer = null;

  function fireInput(el) {
    if (!el) return;
    post({
      kind: "input_change",
      fingerprint: fingerprint(el),
      value: el.value != null ? String(el.value) : "",
      page_url: location.href,
    });
  }

  function flushPendingInput() {
    if (pendingInputEl) {
      if (pendingInputTimer) clearTimeout(pendingInputTimer);
      fireInput(pendingInputEl);
      pendingInputEl = null;
      pendingInputTimer = null;
    }
  }

  function schedulePending(el) {
    if (pendingInputEl && pendingInputEl !== el) {
      // different element — flush the old one before tracking the new
      if (pendingInputTimer) clearTimeout(pendingInputTimer);
      fireInput(pendingInputEl);
    }
    pendingInputEl = el;
    if (pendingInputTimer) clearTimeout(pendingInputTimer);
    pendingInputTimer = setTimeout(function () {
      if (pendingInputEl) {
        fireInput(pendingInputEl);
        pendingInputEl = null;
        pendingInputTimer = null;
      }
    }, INPUT_DEBOUNCE_MS);
  }

  // Only text-ish input types use the debounced input listener. Other
  // types (date, color, range, checkbox, radio, file) emit a single
  // `change` event that the change handler captures directly — catching
  // both here would produce duplicates.
  var TEXTISH_TYPES = {
    text: 1,
    "": 1,
    number: 1,
    password: 1,
    email: 1,
    url: 1,
    search: 1,
    tel: 1,
  };

  document.addEventListener(
    "input",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag === "textarea") {
        schedulePending(t);
        return;
      }
      if (tag !== "input") return;
      var itype = (t.getAttribute && t.getAttribute("type") || "text").toLowerCase();
      if (!TEXTISH_TYPES[itype]) return;
      schedulePending(t);
    },
    true
  );

  document.addEventListener(
    "blur",
    function (e) {
      var t = e.target;
      if (t && t === pendingInputEl) flushPendingInput();
    },
    true
  );

  document.addEventListener(
    "change",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag === "select") {
        post({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: t.value != null ? String(t.value) : "",
          page_url: location.href,
        });
      } else if (tag === "input" && t.type === "file") {
        var fname = "";
        if (t.files && t.files[0]) fname = t.files[0].name;
        post({
          kind: "file_selected",
          fingerprint: fingerprint(t),
          file_name: fname,
          page_url: location.href,
        });
      } else if (tag === "input" && (t.type === "checkbox" || t.type === "radio")) {
        post({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: String(!!t.checked),
          page_url: location.href,
        });
      } else if (tag === "input" && t.type === "date") {
        post({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: t.value || "",
          page_url: location.href,
        });
      }
    },
    true
  );

  document.addEventListener(
    "submit",
    function (e) {
      // Flush any pending text input first (e.g. the last field of a form)
      flushPendingInput();
      post({
        kind: "submit",
        fingerprint: fingerprint(e.target),
        page_url: location.href,
      });
    },
    true
  );

  // Navigation — initial + SPA route changes
  function postNavigate() {
    post({ kind: "navigate", url: location.href, page_url: location.href });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", postNavigate);
  } else {
    postNavigate();
  }

  // Hook History API for SPA routers
  (function () {
    var _push = history.pushState;
    var _replace = history.replaceState;
    history.pushState = function () {
      var r = _push.apply(this, arguments);
      setTimeout(postNavigate, 10);
      return r;
    };
    history.replaceState = function () {
      var r = _replace.apply(this, arguments);
      setTimeout(postNavigate, 10);
      return r;
    };
    window.addEventListener("popstate", postNavigate);
    window.addEventListener("hashchange", postNavigate);
  })();

  // Enter / Escape on focused input
  document.addEventListener(
    "keydown",
    function (e) {
      if (e.key !== "Enter" && e.key !== "Escape") return;
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag !== "input" && tag !== "textarea") return;
      post({
        kind: "key",
        fingerprint: fingerprint(t),
        value: e.key,
        page_url: location.href,
      });
    },
    true
  );

  if (DEBUG) console.log("[cp] grabber installed on", location.href);
})();
