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

  // ---- WI-02: causality + identity + ordering ---------------------------
  //
  // Every emitted event carries an event_id (crypto.randomUUID()) plus
  // a monotonic sequence number plus, when applicable, a caused_by
  // reference to the user interaction that triggered it. A click that
  // fires React Router pushState produces TWO raw events sharing
  // interaction_id: the click (caused_by=null, source="user_click")
  // and the navigate (caused_by=click.event_id, source="history.pushState").
  //
  // We do NOT suppress raw events here -- the annotator decides what
  // to collapse based on causality. This preserves intent fidelity
  // (operator-typed URL bar navigation has caused_by=null and survives
  // through the annotator; click-driven SPA route change has caused_by
  // and gets folded into the click's effects).
  //
  // F-08a: synchronous-window attribution (replaces the prior
  // ATTRIBUTION_WINDOW_MS time-based heuristic).
  //
  // activeInteraction is set on user-initiated events (click, keydown,
  // submit, change). It lives ONLY through the synchronous call stack
  // of the originating handler -- a microtask scheduled at the end of
  // the handler clears it. Any History API call / fetch / XHR fired
  // inside the handler's synchronous execution sees the interaction
  // and attributes to it. Anything fired async (setTimeout, promise
  // .then() after a network hop) sees no active interaction and
  // attributes to nothing -- which is what we want for true
  // background work.
  //
  // The History API wrappers below use setTimeout(0) -- those are
  // async. We deliberately keep activeInteraction alive across
  // exactly one microtask (which queueMicrotask runs BEFORE
  // setTimeout(0) callbacks) so an SPA router that schedules
  // pushState synchronously OR via a queued microtask still
  // attributes correctly. setTimeout callbacks fire after the
  // microtask clear, so they see no active interaction -- this
  // is the cutoff for "really async."
  //
  // A small fallback timer (FALLBACK_WINDOW_MS) is kept ONLY as a
  // safety net for legacy code paths where the History wrappers'
  // setTimeout has already been entered. The constant is small (one
  // event-loop tick budget) and exists so attribution doesn't get
  // dropped purely because of our own wrapper's setTimeout(0); it is
  // not a 3-second guess. Remove once History wrappers post events
  // directly without setTimeout.
  var FALLBACK_WINDOW_MS = 50;
  var activeInteraction = null;
  var _seq = 0;

  function _newEventId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    // Fallback for older browsers / non-secure contexts. Same shape,
    // weaker uniqueness; sequence + monotonic_ts compensate.
    return (
      Date.now().toString(36) +
      "-" +
      Math.random().toString(36).slice(2, 10) +
      "-" +
      (++_seq).toString(36)
    );
  }

  function _now() {
    return (window.performance && window.performance.now)
      ? window.performance.now()
      : Date.now();
  }

  function _setActiveInteraction(kind, eventId) {
    activeInteraction = {
      id: eventId,
      // Each root user action opens its own interaction. Two clicks
      // produce two interactions; we do NOT chain them via a time
      // window. The interaction_id is the same as the event id so
      // either lookup works.
      interaction_id: eventId,
      kind: kind,
      ts: _now(),
    };
    // Clear at microtask boundary so consequence events scheduled by
    // the page's own handlers (which run after our capture-phase
    // listener returns) still see the interaction during that
    // microtask. Anything scheduled with setTimeout / requestAnimationFrame
    // runs AFTER the microtask clear and sees no active interaction.
    var schedule = (typeof queueMicrotask === "function")
      ? queueMicrotask
      : function (fn) { Promise.resolve().then(fn); };
    var snapshot = activeInteraction;
    schedule(function () {
      if (activeInteraction === snapshot) {
        activeInteraction = null;
      }
    });
  }

  function _isWithinWindow() {
    // F-08a: replace time-window heuristic with synchronous-window
    // attribution. activeInteraction is cleared at microtask boundary
    // by _setActiveInteraction's scheduled cleanup; if it's still
    // set, we're inside the originating handler's sync window OR
    // within FALLBACK_WINDOW_MS (the small budget reserved for the
    // History API wrappers' own setTimeout). Pure background work
    // started after the handler returned sees activeInteraction=null.
    if (!activeInteraction) return false;
    return _now() - activeInteraction.ts < FALLBACK_WINDOW_MS;
  }

  function _merge(payload, attribution) {
    // Shallow-merge attribution fields into the payload object. Used
    // by every post() call site so attribution doesn't get hand-rolled
    // (and accidentally omitted) per handler.
    var out = {};
    for (var k in payload) if (Object.prototype.hasOwnProperty.call(payload, k)) out[k] = payload[k];
    for (var j in attribution) if (Object.prototype.hasOwnProperty.call(attribution, j)) out[j] = attribution[j];
    return out;
  }

  function _attribution(source, options) {
    // Consequence-event attribution: returns { event_id,
    //   interaction_id, caused_by, sequence, source, monotonic_ts }
    //   with caused_by + interaction_id pulled from the active
    //   interaction if one is within the attribution window.
    //
    // ``options.causal=false`` opts out (informational events like
    // page_snapshot don't attribute to user actions).
    //
    // For events that ARE user actions (click, submit, key, change
    // commit), use _rootAttribution instead -- those are roots and
    // must not attribute to a prior unrelated user action.
    var eventId = _newEventId();
    var causedBy = null;
    var interactionId = null;
    if (options && options.causal === false) {
      // Informational events: no interaction linkage.
    } else if (_isWithinWindow()) {
      causedBy = activeInteraction.id;
      interactionId = activeInteraction.interaction_id;
    }
    return {
      event_id: eventId,
      interaction_id: interactionId,
      caused_by: causedBy,
      sequence: ++_seq,
      source: source || null,
      monotonic_ts: _now(),
    };
  }

  // ---- F-06: page state snapshots ---------------------------------------
  //
  // Producer for TraceEvent.page_state_before / page_state_after. Captured
  // synchronously around each user-action event so the annotator can
  // detect "this click did/didn't change the page" without re-querying
  // the DOM at replay time.
  //
  // key_dom_signature is a cheap FNV-1a hash of:
  //   - body innerText length
  //   - active element testid (or '')
  //   - first 3 visible H1/H2 text snippets
  // Different signatures imply the page state changed materially;
  // identical signatures imply the action was a no-op (or its effect
  // hasn't landed yet). Cheap enough to call inline on every event.
  function _fnv1a(s) {
    var h = 0x811c9dc5;
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = (h * 0x01000193) >>> 0;
    }
    return ("00000000" + h.toString(16)).slice(-8);
  }

  function _keyDomSignature() {
    var parts = [];
    try {
      var bodyLen = (document.body && document.body.innerText
        ? document.body.innerText.length
        : 0);
      parts.push("l" + bodyLen);
    } catch (e) { parts.push("l?"); }
    try {
      var ae = document.activeElement;
      var aeTid = (ae && ae.getAttribute && ae.getAttribute("data-testid")) || "";
      parts.push("a" + aeTid);
    } catch (e) { parts.push("a?"); }
    try {
      var headings = document.querySelectorAll("h1, h2");
      var hsnips = [];
      for (var i = 0; i < headings.length && hsnips.length < 3; i++) {
        var rect = headings[i].getBoundingClientRect();
        if (!rect || rect.width === 0 || rect.height === 0) continue;
        var text = (headings[i].innerText || headings[i].textContent || "").trim();
        hsnips.push(text.slice(0, 40));
      }
      parts.push("h" + hsnips.join("|"));
    } catch (e) { parts.push("h?"); }
    return _fnv1a(parts.join("\n"));
  }

  function _pageState() {
    return {
      url: location.href,
      title: document.title || "",
      key_dom_signature: _keyDomSignature(),
    };
  }

  function _emitWithStateSnapshot(payload, stateBefore) {
    // Stamp before-state synchronously, defer the post to the next
    // microtask so the page's own synchronous handlers run first and
    // their DOM effects are visible in page_state_after. This keeps
    // event count the same (one event per user action) while letting
    // the annotator detect "this click changed the page."
    payload.page_state_before = stateBefore;
    var schedule = (typeof queueMicrotask === "function")
      ? queueMicrotask
      : function (fn) { Promise.resolve().then(fn); };
    schedule(function () {
      try {
        payload.page_state_after = _pageState();
      } catch (e) {
        payload.page_state_after = null;
      }
      post(payload);
    });
  }

  function _rootAttribution(source) {
    // User-initiated events that START an interaction. They are roots
    // in the causality graph: caused_by=null, fresh interaction_id
    // (set equal to event_id so the interaction is identifiable by
    // either id). Two clicks 1s apart produce TWO distinct
    // interactions; the second does NOT attribute to the first.
    var eventId = _newEventId();
    return {
      event_id: eventId,
      interaction_id: eventId,
      caused_by: null,
      sequence: ++_seq,
      source: source || null,
      monotonic_ts: _now(),
    };
  }

  // ---- Quiescence watchers ------------------------------------------------
  //
  // The runner reads these globals at replay to decide when a step has
  // settled enough to act on the next one. Because they expose *real*
  // signals (last DOM mutation, in-flight fetch/XHR count) the runner
  // pays no wait cost when the page is already idle -- it only waits if
  // the page actually has activity.
  //
  // window.__cp_last_mutation_at  -- ms epoch of last DOM mutation
  // window.__cp_inflight          -- count of fetch+XHR currently open
  // window.__cp_last_request_at   -- ms epoch of most recent request
  function _installQuiescenceWatchers() {
    if (window.__cp_quiescence_installed) return;
    window.__cp_quiescence_installed = true;
    window.__cp_last_mutation_at = Date.now();
    window.__cp_inflight = 0;
    window.__cp_last_request_at = 0;

    // F-07: debounced dom_mutation burst summaries. The MutationObserver
    // batches DOM changes that arrive within ~200ms into one event so
    // WI-09 / WI-10 can wait on "this action caused real DOM activity"
    // without scanning every individual MutationRecord.
    var _mutBurst = null;
    var _mutBurstTimer = null;
    var DOM_MUTATION_BURST_MS = 200;

    function _flushMutationBurst() {
      if (!_mutBurst) return;
      var summary = _mutBurst;
      _mutBurst = null;
      _mutBurstTimer = null;
      try {
        var attr = _attribution("mutation_observer");
        post(_merge({
          kind: "dom_mutation",
          page_url: location.href,
          raw_event_kind: "dom_mutation",
          mutation_summary: summary,
        }, attr));
      } catch (e) {
        if (DEBUG) console.warn("[cp] dom_mutation emit failed", e);
      }
    }

    function bumpMutation(records) {
      window.__cp_last_mutation_at = Date.now();
      if (!_mutBurst) {
        _mutBurst = {
          added: 0,
          removed: 0,
          attribute: 0,
          character_data: 0,
          first_target_selector: null,
        };
      }
      try {
        for (var i = 0; i < records.length; i++) {
          var r = records[i];
          if (r.type === "childList") {
            _mutBurst.added += (r.addedNodes && r.addedNodes.length) || 0;
            _mutBurst.removed += (r.removedNodes && r.removedNodes.length) || 0;
          } else if (r.type === "attributes") {
            _mutBurst.attribute++;
          } else if (r.type === "characterData") {
            _mutBurst.character_data++;
          }
          if (!_mutBurst.first_target_selector && r.target && r.target.nodeType === 1) {
            try {
              _mutBurst.first_target_selector = buildCssPath(r.target);
            } catch (e) {}
          }
        }
      } catch (e) {}
      if (_mutBurstTimer) clearTimeout(_mutBurstTimer);
      _mutBurstTimer = setTimeout(_flushMutationBurst, DOM_MUTATION_BURST_MS);
    }

    function _observe() {
      var root = document.body || document.documentElement;
      if (!root) {
        // body not parsed yet; try again on the next microtask
        return setTimeout(_observe, 50);
      }
      try {
        var mo = new MutationObserver(bumpMutation);
        mo.observe(root, {
          childList: true,
          subtree: true,
          attributes: true,
          characterData: true,
        });
      } catch (e) {
        if (DEBUG) console.warn("[cp] MutationObserver failed", e);
      }
    }
    _observe();

    // fetch hook (F-07: emits network_request + network_response
    // TraceEvents alongside the existing counters)
    if (window.fetch && !window.__cp_fetch_hooked) {
      window.__cp_fetch_hooked = true;
      var _origFetch = window.fetch.bind(window);
      window.fetch = function (input, init) {
        window.__cp_inflight = (window.__cp_inflight || 0) + 1;
        window.__cp_last_request_at = Date.now();
        var url = "";
        var method = "GET";
        try {
          if (typeof input === "string") {
            url = input;
            method = (init && init.method) || "GET";
          } else if (input && input.url) {
            url = input.url;
            method = (init && init.method) || input.method || "GET";
          }
        } catch (e) {}
        var requestId = _newEventId();
        var startedAt = _now();
        // Emit the request-start event synchronously so its
        // initiator_event_id is the active interaction at THIS tick,
        // not whatever happens to be active when the response lands.
        _emitNetworkRequest(requestId, method, url, startedAt);
        var p;
        try {
          p = _origFetch.apply(this, arguments);
        } catch (e) {
          window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
          _emitNetworkResponse(requestId, method, url, startedAt, _now(), 0);
          throw e;
        }
        return p.then(
          function (r) {
            window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
            window.__cp_last_request_at = Date.now();
            try {
              _emitNetworkResponse(
                requestId, method, url, startedAt, _now(), r && r.status
              );
            } catch (e) {}
            return r;
          },
          function (e) {
            window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
            window.__cp_last_request_at = Date.now();
            try {
              _emitNetworkResponse(
                requestId, method, url, startedAt, _now(), 0
              );
            } catch (e2) {}
            throw e;
          },
        );
      };
    }

    // XHR hook (F-07: emits network_request + network_response too).
    // open() is wrapped so we capture method + url; send() emits the
    // request-start event and binds loadend to the response emit.
    if (window.XMLHttpRequest && !window.__cp_xhr_hooked) {
      window.__cp_xhr_hooked = true;
      var _origOpen = window.XMLHttpRequest.prototype.open;
      window.XMLHttpRequest.prototype.open = function (m, u) {
        try {
          this.__cp_method = (m || "GET").toUpperCase();
          this.__cp_url = u || "";
        } catch (e) {}
        return _origOpen.apply(this, arguments);
      };
      var _origSend = window.XMLHttpRequest.prototype.send;
      window.XMLHttpRequest.prototype.send = function () {
        var self = this;
        window.__cp_inflight = (window.__cp_inflight || 0) + 1;
        window.__cp_last_request_at = Date.now();
        var requestId = _newEventId();
        var startedAt = _now();
        var method = self.__cp_method || "GET";
        var url = self.__cp_url || "";
        _emitNetworkRequest(requestId, method, url, startedAt);
        function _done() {
          window.__cp_inflight = Math.max(0, window.__cp_inflight - 1);
          window.__cp_last_request_at = Date.now();
          try {
            _emitNetworkResponse(
              requestId, method, url, startedAt, _now(),
              (typeof self.status === "number" ? self.status : 0)
            );
          } catch (e) {}
        }
        self.addEventListener("loadend", _done);
        return _origSend.apply(self, arguments);
      };
    }
  }

  function _emitNetworkRequest(requestId, method, url, startedAt) {
    // Network requests are CONSEQUENCE events -- they don't open a
    // new interaction window. Attribute to the active user
    // interaction (set inside a click/submit/key/change handler).
    // For requests with no active interaction (background poll, SSE
    // reconnect), initiator_event_id is null.
    var initiator = (activeInteraction && _isWithinWindow())
      ? activeInteraction.id : null;
    var attr = _attribution("fetch_request_start");
    post(_merge({
      kind: "network_request",
      page_url: location.href,
      raw_event_kind: "fetch_request_start",
      request_id: requestId,
      method: method,
      url: url,
      started_at: startedAt,
      initiator_event_id: initiator,
    }, attr));
  }

  function _emitNetworkResponse(requestId, method, url, startedAt, finishedAt, status) {
    var initiator = (activeInteraction && _isWithinWindow())
      ? activeInteraction.id : null;
    var attr = _attribution("fetch_response");
    post(_merge({
      kind: "network_response",
      page_url: location.href,
      raw_event_kind: "fetch_response",
      request_id: requestId,
      method: method,
      url: url,
      started_at: startedAt,
      finished_at: finishedAt,
      status: status || 0,
      initiator_event_id: initiator,
    }, attr));
  }
  _installQuiescenceWatchers();

  // ---- WI-10: observed readiness watcher ---------------------------------
  //
  // Watches the page for transitions that signal a "busy" period
  // ending: aria-busy clearing, role=progressbar disappearing, button
  // disabled flipping back to enabled, button text settling. Each
  // observed transition during a user-initiated interaction window
  // becomes a dom_mutation event whose mutation_summary carries a
  // ``readiness`` field. The annotator converts these into
  // expected_signals.dom entries so replay waits for the declared
  // readiness signal instead of the conventional spinner-selector
  // list. The legacy convention (status-saving etc.) remains as a
  // last-resort fallback.
  //
  // Cheap design: a MutationObserver scoped to attributes on the WHOLE
  // document (no childList -- the quiescence watcher above already
  // tracks DOM growth). Filtering happens in the JS callback so the
  // payload only carries transitions worth annotating.
  function _installReadinessWatcher() {
    if (window.__cp_readiness_installed) return;
    window.__cp_readiness_installed = true;
    try {
      var root = document.body || document.documentElement;
      if (!root) {
        return setTimeout(_installReadinessWatcher, 100);
      }
      var mo = new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
          var r = records[i];
          if (r.type !== "attributes") continue;
          var t = r.target;
          if (!t || t.nodeType !== 1) continue;
          var name = r.attributeName;
          // aria-busy clear
          if (name === "aria-busy" && t.getAttribute("aria-busy") === "false") {
            _emitReadiness("aria_busy", t, "false");
          }
          // disabled clear: previously had disabled attribute, now doesn't
          if (
            name === "disabled"
            && !t.hasAttribute("disabled")
            && (t.tagName === "BUTTON"
                || t.tagName === "INPUT"
                || t.tagName === "TEXTAREA"
                || t.tagName === "SELECT")
          ) {
            _emitReadiness("disabled_until_enabled", t, "enabled");
          }
        }
      });
      mo.observe(root, {
        attributes: true,
        subtree: true,
        attributeFilter: ["aria-busy", "disabled"],
      });
    } catch (e) {
      if (DEBUG) console.warn("[cp] readiness watcher failed", e);
    }
  }

  function _readinessSelector(el) {
    // Build a stable selector for the observed element. Prefer
    // data-testid -> id -> tag+class.
    var tid = el.getAttribute && el.getAttribute("data-testid");
    if (tid) return "[data-testid='" + tid + "']";
    if (el.id) return "#" + cssEscape(el.id);
    var tag = (el.tagName || "").toLowerCase();
    if (el.className && typeof el.className === "string") {
      var firstClass = el.className.split(/\s+/)[0];
      if (firstClass) return tag + "." + cssEscape(firstClass);
    }
    return tag;
  }

  function _emitReadiness(kind, el, value) {
    // Only emit when there's an active user interaction -- otherwise
    // background DOM activity floods the channel. The annotator pairs
    // these with the originating user action via caused_by.
    if (!activeInteraction || !_isWithinWindow()) return;
    var attr = _attribution("readiness_observer");
    try {
      post(_merge({
        kind: "dom_mutation",
        page_url: location.href,
        raw_event_kind: "readiness_transition",
        mutation_summary: {
          readiness: {
            kind: kind,
            selector: _readinessSelector(el),
            value: value,
          },
        },
      }, attr));
    } catch (e) {
      if (DEBUG) console.warn("[cp] readiness emit failed", e);
    }
  }
  _installReadinessWatcher();

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
    var meta = _controlMetadata(el);
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
      // WI-03: enriched control metadata. control_kind + value_kind
      // drive semantic action selection; options_snapshot +
      // selected_options + min/max/step support typed-param codecs;
      // ARIA flags + disabled/readonly feed readiness-aware waits.
      control_kind: meta.control_kind,
      value_kind: meta.value_kind,
      options_snapshot: meta.options_snapshot,
      selected_options: meta.selected_options,
      aria_expanded: meta.aria_expanded,
      aria_disabled: meta.aria_disabled,
      aria_busy: meta.aria_busy,
      disabled: meta.disabled,
      readonly: meta.readonly,
      contenteditable: meta.contenteditable,
      locale_hint: meta.locale_hint,
      timezone_hint: meta.timezone_hint,
      min: meta.min,
      max: meta.max,
      step: meta.step,
      accept: meta.accept,
      multiple: meta.multiple,
    };
  }

  // ---- WI-03: control metadata extraction ----------------------------------

  function _controlMetadata(el) {
    var out = {
      control_kind: null,
      value_kind: null,
      options_snapshot: null,
      selected_options: null,
      aria_expanded: null,
      aria_disabled: null,
      aria_busy: null,
      disabled: null,
      readonly: null,
      contenteditable: null,
      locale_hint: null,
      timezone_hint: null,
      min: null,
      max: null,
      step: null,
      accept: null,
      multiple: null,
    };
    if (!el || !el.tagName) return out;

    // Locale / timezone are page-level signals. Cheap to read on every
    // fingerprint (Intl.DateTimeFormat is fast); annotator deduplicates.
    try {
      out.locale_hint = (document.documentElement &&
        document.documentElement.lang) || navigator.language || null;
    } catch (e) {}
    try {
      var dtf = Intl.DateTimeFormat().resolvedOptions();
      out.timezone_hint = dtf && dtf.timeZone ? dtf.timeZone : null;
    } catch (e) {}

    // ARIA state
    var ariaExpanded = el.getAttribute && el.getAttribute("aria-expanded");
    if (ariaExpanded !== null && ariaExpanded !== undefined) {
      out.aria_expanded = ariaExpanded === "true";
    }
    var ariaDisabled = el.getAttribute && el.getAttribute("aria-disabled");
    if (ariaDisabled !== null && ariaDisabled !== undefined) {
      out.aria_disabled = ariaDisabled === "true";
    }
    var ariaBusy = el.getAttribute && el.getAttribute("aria-busy");
    if (ariaBusy !== null && ariaBusy !== undefined) {
      out.aria_busy = ariaBusy === "true";
    }

    // contenteditable cascades up the DOM (a <p> inside a
    // <div contenteditable> is editable). Check ancestors.
    var ceNode = el;
    while (ceNode && ceNode.nodeType === 1) {
      var ce = ceNode.getAttribute && ceNode.getAttribute("contenteditable");
      if (ce !== null) {
        out.contenteditable = ce === "" || ce === "true";
        break;
      }
      ceNode = ceNode.parentElement;
    }

    var tag = el.tagName.toLowerCase();
    var role = computedRole(el);

    if (tag === "input") {
      var t = (el.getAttribute("type") || "text").toLowerCase();
      out.disabled = !!el.disabled;
      out.readonly = !!el.readOnly;
      out.min = el.getAttribute("min");
      out.max = el.getAttribute("max");
      out.step = el.getAttribute("step");
      switch (t) {
        case "checkbox":
          out.control_kind = "checkbox";
          out.value_kind = "boolean";
          break;
        case "radio":
          out.control_kind = "radio";
          out.value_kind = "boolean";
          break;
        case "date":
          out.control_kind = "date_input";
          out.value_kind = "date";
          break;
        case "time":
          out.control_kind = "time_input";
          out.value_kind = "time";
          break;
        case "datetime-local":
          out.control_kind = "datetime_input";
          out.value_kind = "datetime";
          break;
        case "month":
          out.control_kind = "month_input";
          out.value_kind = "date";
          break;
        case "week":
          out.control_kind = "week_input";
          out.value_kind = "date";
          break;
        case "color":
          out.control_kind = "color_input";
          out.value_kind = "color";
          break;
        case "range":
          out.control_kind = "range_slider";
          out.value_kind = "number";
          break;
        case "file":
          out.control_kind = "file_input";
          out.value_kind = "file";
          out.accept = el.getAttribute("accept");
          out.multiple = !!el.multiple;
          break;
        case "number":
          out.control_kind = "number_input";
          out.value_kind = "number";
          break;
        case "password":
          out.control_kind = "password_input";
          out.value_kind = "string";
          break;
        case "email":
          out.control_kind = "email_input";
          out.value_kind = "string";
          break;
        case "url":
          out.control_kind = "url_input";
          out.value_kind = "string";
          break;
        case "search":
          out.control_kind = "search_input";
          out.value_kind = "string";
          break;
        case "tel":
          out.control_kind = "tel_input";
          out.value_kind = "string";
          break;
        case "submit":
        case "button":
          out.control_kind = "button";
          out.value_kind = "none";
          break;
        default:
          out.control_kind = "text_input";
          out.value_kind = "string";
      }
    } else if (tag === "textarea") {
      out.control_kind = "textarea";
      out.value_kind = "string";
      out.disabled = !!el.disabled;
      out.readonly = !!el.readOnly;
    } else if (tag === "select") {
      out.disabled = !!el.disabled;
      out.multiple = !!el.multiple;
      out.control_kind = el.multiple ? "select_multiple" : "select_single";
      out.value_kind = el.multiple ? "list" : "string";
      // F-08b: option snapshot cap. Default is configurable via
      // PortalContext.options_snapshot_max (read by window.__cp_opts_cap
      // when teach is started); we keep a sane built-in ceiling of 500
      // to bound the JSON payload size for selects with thousands of
      // options. When the cap is hit, options_truncated=true is set so
      // the annotator knows to suggest a search/filter step (WI-25)
      // rather than treating the snapshot as exhaustive.
      var optsCap = (typeof window.__cp_opts_cap === "number" && window.__cp_opts_cap > 0)
        ? window.__cp_opts_cap : 500;
      var opts = [];
      var selected = [];
      var truncated = false;
      try {
        for (var i = 0; i < el.options.length; i++) {
          if (opts.length >= optsCap) { truncated = true; break; }
          var o = el.options[i];
          var entry = {
            value: o.value,
            label: trim(o.textContent || ""),
            selected: !!o.selected,
            disabled: !!o.disabled,
          };
          opts.push(entry);
          if (o.selected) selected.push(o.value);
        }
        out.options_snapshot = opts;
        out.selected_options = selected;
        out.options_truncated = truncated;
      } catch (e) {}
    } else if (tag === "button") {
      out.control_kind = "button";
      out.value_kind = "none";
      out.disabled = !!el.disabled;
    } else if (tag === "a") {
      out.control_kind = el.href ? "anchor" : "unknown";
      out.value_kind = "none";
    } else if (out.contenteditable) {
      out.control_kind = "contenteditable";
      out.value_kind = "html";
    } else if (role === "combobox") {
      out.control_kind = "combobox_aria";
      out.value_kind = "string";
    } else if (role === "listbox") {
      out.control_kind = "listbox_aria";
      out.value_kind = "list";
    } else if (role === "tab") {
      out.control_kind = "tab";
      out.value_kind = "none";
    } else if (role === "menuitem") {
      out.control_kind = "menuitem";
      out.value_kind = "none";
    } else if (role === "treeitem") {
      out.control_kind = "treeitem";
      out.value_kind = "none";
    } else if (role === "option") {
      out.control_kind = "option";
      out.value_kind = "none";
    } else {
      out.control_kind = role === "button" ? "button" : "unknown";
      out.value_kind = "none";
    }

    return out;
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

  // WI-14: capture target state (aria-expanded/checked/pressed,
  // disabled, selected) for a click target so the annotator can
  // distinguish toggle / open / close / no-op gestures from a plain
  // single click without re-querying the DOM at replay.
  function _targetStateSnapshot(el) {
    if (!el || !el.getAttribute) return null;
    var s = {};
    var ae = el.getAttribute("aria-expanded");
    if (ae !== null) s.aria_expanded = ae;
    var ac = el.getAttribute("aria-checked");
    if (ac !== null) s.aria_checked = ac;
    var ap = el.getAttribute("aria-pressed");
    if (ap !== null) s.aria_pressed = ap;
    var as = el.getAttribute("aria-selected");
    if (as !== null) s.aria_selected = as;
    var d = el.getAttribute("disabled");
    s.disabled = (d !== null) ? (d === "" ? "true" : d) : null;
    return s;
  }

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
      // F-06: capture page state immediately before the click runs
      // and again on the next microtask so the handler's synchronous
      // side-effects (pushState fired in the same tick) are visible
      // as "after." Async effects (XHR responses, deferred renders)
      // arrive as their own events.
      var stateBefore = _pageState();
      // WI-14: capture click detail (count), pointer type, and target
      // state before the page's handlers run. Pointer type is only set
      // on PointerEvent; mouse events don't carry it directly.
      var clickDetail = (typeof e.detail === "number" && e.detail > 0)
        ? e.detail : 1;
      var pointerType = (e.pointerType || null);
      var targetStateBefore = _targetStateSnapshot(target);
      // WI-02: click is a USER-INITIATED EVENT -- it opens a fresh
      // interaction window. Subsequent consequence events (history
      // pushState, fetch starts, mutations) attribute to this click
      // until the window closes.
      var attr = _rootAttribution("user_click");
      _setActiveInteraction("click", attr.event_id);
      var payload = _merge({
        kind: "click",
        fingerprint: fingerprint(target),
        page_url: location.href,
        raw_event_kind: "click",
        click_detail: clickDetail,
        pointer_type: pointerType,
        target_state_before: targetStateBefore,
      }, attr);
      // WI-14: schedule the after-state snapshot on the microtask the
      // same way _emitWithStateSnapshot defers page_state_after. The
      // page's own click handler runs synchronously after our capture-
      // phase listener returns, so the microtask sees the post-handler
      // attribute mutations (aria-expanded flipped, etc.).
      payload.page_state_before = stateBefore;
      var schedule = (typeof queueMicrotask === "function")
        ? queueMicrotask
        : function (fn) { Promise.resolve().then(fn); };
      schedule(function () {
        try {
          payload.page_state_after = _pageState();
        } catch (e2) {
          payload.page_state_after = null;
        }
        try {
          payload.target_state_after = _targetStateSnapshot(target);
        } catch (e3) {
          payload.target_state_after = null;
        }
        post(payload);
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
  // WI-13: before-value capture. ``valueBeforeFor`` maps element ->
  // its value AT THE MOMENT the operator started editing it (focus
  // or first input keystroke). The input_change emit attaches this as
  // ``value_before`` so the annotator can detect a CLEAR (was
  // non-empty, became empty) and route the runner to fill('') +
  // assert empty post-action. Stored as a WeakMap when available so
  // detached DOM nodes get GC'd; falls back to a plain Map on older
  // engines (the recording session is short, so leakage is bounded).
  var valueBeforeFor = (typeof WeakMap === "function") ? new WeakMap() : new Map();

  function _captureBeforeValue(el) {
    // Only record once per editing pass. If we already captured a
    // before-value for this element and haven't flushed it yet, the
    // operator is still editing -- keep the original before-value.
    if (!el) return;
    if (valueBeforeFor.has(el)) return;
    try {
      var v = el.value != null ? String(el.value) : "";
      valueBeforeFor.set(el, v);
    } catch (e) {}
  }

  function _consumeBeforeValue(el) {
    if (!el || !valueBeforeFor.has(el)) return null;
    var v = valueBeforeFor.get(el);
    try { valueBeforeFor.delete(el); } catch (e) {}
    return v;
  }

  function fireInput(el) {
    if (!el) return;
    // input_change is NOT a user-action in the causal sense -- it's
    // value-settling. It attributes to the most recent active
    // interaction (typically a focus/click) but does NOT open a new
    // interaction window. The actual "commit" is usually a subsequent
    // submit/click/blur.
    var stateBefore = _pageState();
    var beforeValue = _consumeBeforeValue(el);
    var payload = _merge({
      kind: "input_change",
      fingerprint: fingerprint(el),
      value: el.value != null ? String(el.value) : "",
      value_before: beforeValue,
      page_url: location.href,
      raw_event_kind: "input",
    }, _attribution("user_input"));
    _emitWithStateSnapshot(payload, stateBefore);
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

  // WI-13: capture before-value as early as possible -- on focus, so
  // the operator's first keystroke doesn't overwrite the recorded
  // "starting" value. focusin bubbles and fires for editable controls.
  document.addEventListener(
    "focusin",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag === "textarea") {
        _captureBeforeValue(t);
        return;
      }
      if (tag !== "input") return;
      var itype = (t.getAttribute && t.getAttribute("type") || "text").toLowerCase();
      if (!TEXTISH_TYPES[itype]) return;
      _captureBeforeValue(t);
    },
    true
  );

  document.addEventListener(
    "input",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag === "textarea") {
        // Fallback: programmatic focus without a focusin event (rare).
        _captureBeforeValue(t);
        schedulePending(t);
        return;
      }
      if (tag !== "input") return;
      var itype = (t.getAttribute && t.getAttribute("type") || "text").toLowerCase();
      if (!TEXTISH_TYPES[itype]) return;
      _captureBeforeValue(t);
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
      // WI-02: change events are USER ACTIONS (they commit a value).
      // Selecting an option in a <select>, picking a file, ticking a
      // checkbox -- each opens a fresh interaction window so that any
      // network call / DOM update fired in response attributes back.
      // F-06: snapshot before-state once for all branches below.
      var changeStateBefore = _pageState();
      if (tag === "select") {
        var attr1 = _rootAttribution("user_change");
        _setActiveInteraction("change", attr1.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: t.value != null ? String(t.value) : "",
          page_url: location.href,
          raw_event_kind: "change",
        }, attr1), changeStateBefore);
      } else if (tag === "input" && t.type === "file") {
        var fname = "";
        if (t.files && t.files[0]) fname = t.files[0].name;
        var attr2 = _rootAttribution("user_file_selected");
        _setActiveInteraction("file_selected", attr2.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "file_selected",
          fingerprint: fingerprint(t),
          file_name: fname,
          page_url: location.href,
          raw_event_kind: "change",
        }, attr2), changeStateBefore);
      } else if (tag === "input" && (t.type === "checkbox" || t.type === "radio")) {
        var attr3 = _rootAttribution("user_change");
        _setActiveInteraction("change", attr3.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: String(!!t.checked),
          page_url: location.href,
          raw_event_kind: "change",
        }, attr3), changeStateBefore);
      } else if (tag === "input" && t.type === "date") {
        var attr4 = _rootAttribution("user_change");
        _setActiveInteraction("change", attr4.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: t.value || "",
          page_url: location.href,
          raw_event_kind: "change",
        }, attr4), changeStateBefore);
      }
    },
    true
  );

  document.addEventListener(
    "submit",
    function (e) {
      // Flush any pending text input first (e.g. the last field of a form)
      flushPendingInput();
      // F-06: page state before/after the submit. Synchronous form
      // handlers (preventDefault + custom mutation) land in "after"
      // via microtask.
      var stateBefore = _pageState();
      // WI-02: submit is a USER ACTION -- opens an interaction window
      // so the POST it triggers and the navigation it may cause
      // attribute back.
      var attr = _rootAttribution("user_submit");
      _setActiveInteraction("submit", attr.event_id);
      _emitWithStateSnapshot(_merge({
        kind: "submit",
        fingerprint: fingerprint(e.target),
        page_url: location.href,
        raw_event_kind: "submit",
      }, attr), stateBefore);
    },
    true
  );

  // ---- Passive catalog: page snapshots ---------------------------------
  // After every navigation (initial + SPA route change), wait briefly
  // for the page to settle, then emit one ``page_snapshot`` event with
  // a compact summary of the current page's interactables. The Python
  // side aggregates these into a per-portal catalog.yaml that the
  // planner reads for better clarify questions and feasibility checks.
  // Debounced: only the *latest* navigation triggers a snapshot, so a
  // burst of redirects doesn't flood the channel.

  var _snapshotTimer = null;

  function _collectPageSnapshot() {
    function _trim(s, n) {
      return (s == null ? "" : String(s)).replace(/\s+/g, " ").trim().slice(0, n);
    }
    var buttons = [];
    var inputs = [];
    var selects = [];
    var links = [];

    var btns = document.querySelectorAll("button, [role='button']");
    for (var i = 0; i < btns.length && buttons.length < 80; i++) {
      var el = btns[i];
      var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
      if (!rect || rect.width === 0 || rect.height === 0) continue;
      buttons.push({
        label: _trim(el.innerText || el.textContent || el.getAttribute("aria-label"), 80),
        role: el.getAttribute("role") || "button",
        testId: el.getAttribute("data-testid") || null,
      });
    }

    var inps = document.querySelectorAll("input, textarea");
    for (var j = 0; j < inps.length && inputs.length < 80; j++) {
      var ie = inps[j];
      var t = (ie.getAttribute("type") || "text").toLowerCase();
      if (t === "hidden") continue;
      var labelEl = ie.id ? document.querySelector("label[for='" + ie.id.replace(/'/g, "\\'") + "']") : null;
      inputs.push({
        name: ie.getAttribute("name") || null,
        type: t,
        label: labelEl ? _trim(labelEl.textContent, 60) : null,
        placeholder: ie.getAttribute("placeholder") || null,
        required: ie.required || false,
        testId: ie.getAttribute("data-testid") || null,
      });
    }

    var sels = document.querySelectorAll("select");
    for (var k = 0; k < sels.length && selects.length < 40; k++) {
      var se = sels[k];
      var opts = [];
      for (var m = 0; m < se.options.length && opts.length < 40; m++) {
        var o = se.options[m];
        opts.push({ value: o.value, text: _trim(o.textContent, 80) });
      }
      var slabelEl = se.id ? document.querySelector("label[for='" + se.id.replace(/'/g, "\\'") + "']") : null;
      selects.push({
        name: se.getAttribute("name") || null,
        label: slabelEl ? _trim(slabelEl.textContent, 60) : null,
        testId: se.getAttribute("data-testid") || null,
        options: opts,
      });
    }

    var lks = document.querySelectorAll("a[href]");
    for (var n = 0; n < lks.length && links.length < 60; n++) {
      var le = lks[n];
      var href = le.getAttribute("href") || "";
      if (href.startsWith("javascript:")) continue;
      links.push({
        href: href,
        text: _trim(le.innerText || le.textContent, 80),
        testId: le.getAttribute("data-testid") || null,
      });
    }

    return {
      kind: "page_snapshot",
      page_url: location.href,
      title: document.title || "",
      buttons: buttons,
      inputs: inputs,
      selects: selects,
      links: links,
    };
  }

  function _scheduleSnapshot() {
    if (_snapshotTimer) clearTimeout(_snapshotTimer);
    _snapshotTimer = setTimeout(function () {
      _snapshotTimer = null;
      try {
        // page_snapshot is informational, not causal. It describes the
        // page after a navigation settles. ``causal: false`` means the
        // event won't attribute to whatever's in activeInteraction.
        post(_merge(
          _collectPageSnapshot(),
          _attribution("page_snapshot", { causal: false })
        ));
      } catch (e) {
        if (DEBUG) console.warn("[cp] snapshot failed", e);
      }
    }, 800);
  }

  // Navigation — initial + SPA route changes. The ``navigationSource``
  // string carries the EXACT mechanism so the annotator can distinguish
  // pushState (caused-by-click) from a manual address-bar entry (no
  // active interaction).
  //
  // F-08a: the optional ``pinnedInteraction`` lets the History API
  // wrappers capture the active interaction at the SYNCHRONOUS moment
  // pushState() was called (inside the user's click/submit handler),
  // then forward it through the wrappers' deferred setTimeout
  // without relying on a time window. If pinnedInteraction is null
  // we fall through to the live activeInteraction (which, under
  // synchronous-window semantics, is null for truly background
  // navigations).
  function postNavigate(navigationSource, pinnedInteraction) {
    var attr;
    if (pinnedInteraction) {
      // Attribute to the pinned user action regardless of whether
      // activeInteraction has since cleared.
      attr = {
        event_id: _newEventId(),
        interaction_id: pinnedInteraction.interaction_id,
        caused_by: pinnedInteraction.id,
        sequence: ++_seq,
        source: navigationSource || "navigate",
        monotonic_ts: _now(),
      };
    } else {
      attr = _attribution(navigationSource || "navigate");
    }
    post(_merge(
      {
        kind: "navigate",
        url: location.href,
        page_url: location.href,
        raw_event_kind: navigationSource || "navigate",
      },
      attr
    ));
    _scheduleSnapshot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      postNavigate("initial_load");
    });
  } else {
    postNavigate("initial_load");
  }

  // Hook History API for SPA routers. The wrapped functions name the
  // exact mechanism so navigate events carry source="history.pushState"
  // vs "history.replaceState" vs "popstate" vs "hashchange".
  //
  // F-08a: capture activeInteraction at the SYNCHRONOUS moment
  // pushState() / replaceState() is called -- that's inside the
  // user's click/submit handler. Pass the snapshot through to
  // postNavigate so the deferred setTimeout doesn't need a time
  // window to know what to attribute to. popstate / hashchange are
  // dispatched by the browser, so we use the live activeInteraction
  // (which under synchronous-window semantics is null for pure
  // back/forward; the operator's typed URL doesn't attribute).
  (function () {
    var _push = history.pushState;
    var _replace = history.replaceState;
    history.pushState = function () {
      var pinned = activeInteraction;  // snapshot synchronously
      var r = _push.apply(this, arguments);
      setTimeout(function () {
        postNavigate("history.pushState", pinned);
      }, 10);
      return r;
    };
    history.replaceState = function () {
      var pinned = activeInteraction;
      var r = _replace.apply(this, arguments);
      setTimeout(function () {
        postNavigate("history.replaceState", pinned);
      }, 10);
      return r;
    };
    window.addEventListener("popstate", function () {
      postNavigate("popstate");
    });
    window.addEventListener("hashchange", function () {
      postNavigate("hashchange");
    });
  })();

  // Enter / Escape on focused input. WI-02: a key is a USER ACTION,
  // opens a fresh interaction window so consequences (form submit,
  // navigation, fetch) attribute back.
  document.addEventListener(
    "keydown",
    function (e) {
      if (e.key !== "Enter" && e.key !== "Escape") return;
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();
      if (tag !== "input" && tag !== "textarea") return;
      var stateBefore = _pageState();
      var attr = _rootAttribution("user_keydown");
      _setActiveInteraction("key", attr.event_id);
      _emitWithStateSnapshot(_merge({
        kind: "key",
        fingerprint: fingerprint(t),
        value: e.key,
        page_url: location.href,
        raw_event_kind: "keydown",
      }, attr), stateBefore);
    },
    true
  );

  if (DEBUG) console.log("[cp] grabber installed on", location.href);
})();
