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
  // Followup #3: text-input debounce surfaced through
  // PortalContext.wait_policy.input_debounce_ms. Pushed by the runner
  // (and optionally by teach) onto window.__cp_input_debounce_ms.
  // Literal 400 remains as last-resort fallback so legacy code paths
  // still work when the global isn't set.
  var INPUT_DEBOUNCE_MS = (typeof window.__cp_input_debounce_ms === "number"
    && window.__cp_input_debounce_ms > 0)
    ? window.__cp_input_debounce_ms : 400;
  // 2026-06-02 B5: server-search inputs (placeholder="Search",
  // ng-multiselect-dropdown's filter, mat-autocomplete with a debounced
  // ?q= fetch) need a LONGER window than a plain form field. The real
  // Frame TV portal's trace.jsonl recorded BOTH "cana" and "canada" as
  // separate input_change events because the 400 ms debounce fired
  // mid-typing. Bumping the search-input window to 600 ms produces a
  // single trailing-edge emission with the FINAL value. Operator can
  // override via window.__cp_search_debounce_ms; we cap at >= base.
  var SEARCH_INPUT_DEBOUNCE_MS = (function () {
    var override = window.__cp_search_debounce_ms;
    if (typeof override === "number" && override > 0) {
      return Math.max(override, INPUT_DEBOUNCE_MS);
    }
    return Math.max(600, INPUT_DEBOUNCE_MS);
  })();

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
  // Followup #3: surface through PortalContext.wait_policy
  // .attribution_fallback_window_ms. Pushed by the runner onto
  // window.__cp_attribution_fallback_ms. Literal 50 remains as last-
  // resort fallback (one event-loop tick budget); the docstring on
  // wait_policy explains why this exists even after F-08a.
  var FALLBACK_WINDOW_MS = (typeof window.__cp_attribution_fallback_ms === "number"
    && window.__cp_attribution_fallback_ms >= 0)
    ? window.__cp_attribution_fallback_ms : 50;
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
    // Followup #3: surface through PortalContext.wait_policy
    // .dom_mutation_burst_ms. Pushed by the runner onto
    // window.__cp_dom_mutation_burst_ms. Literal 200 remains as last-
    // resort fallback.
    var DOM_MUTATION_BURST_MS = (typeof window.__cp_dom_mutation_burst_ms === "number"
      && window.__cp_dom_mutation_burst_ms > 0)
      ? window.__cp_dom_mutation_burst_ms : 200;

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

  // WI-47: WebSocket / EventSource (SSE) hook. We hook the
  // constructors so the URL of every channel is observed, then wrap
  // each instance's ``onmessage`` / ``addEventListener('message',...)``
  // so push frames become TraceEvents kind=``network_request`` with
  // method=``WS`` / ``SSE`` and the frame body summary in
  // ``mutation_summary``. The annotator pairs an operator-triggered
  // job with the subsequent push frame; the runner waits on the
  // matching frame instead of polling DOM. Hooking is idempotent
  // (uses __cp_ws_hooked / __cp_es_hooked sentinels).
  (function _installPushHooks() {
    function _summarizeFrame(data) {
      try {
        if (data == null) return null;
        if (typeof data === "string") {
          return { body_summary: data.slice(0, 512) };
        }
        if (data instanceof ArrayBuffer) {
          return { body_summary: "[binary " + data.byteLength + "B]" };
        }
        if (data && typeof data === "object") {
          return { body_summary: JSON.stringify(data).slice(0, 512) };
        }
        return { body_summary: String(data).slice(0, 512) };
      } catch (e) {
        return null;
      }
    }
    if (typeof window.WebSocket === "function" && !window.__cp_ws_hooked) {
      window.__cp_ws_hooked = true;
      var OrigWS = window.WebSocket;
      window.WebSocket = function (url, protocols) {
        var ws = protocols !== undefined
          ? new OrigWS(url, protocols) : new OrigWS(url);
        var channelUrl = String(url || "");
        try {
          var openAttr = _attribution("ws_open");
          var requestId = _newEventId();
          post(_merge({
            kind: "network_request",
            page_url: location.href,
            raw_event_kind: "ws_open",
            request_id: requestId,
            method: "WS",
            url: channelUrl,
            started_at: _now(),
            initiator_event_id: (activeInteraction && _isWithinWindow())
              ? activeInteraction.id : null,
          }, openAttr));
        } catch (e) {
          if (DEBUG) console.warn("[cp] ws open emit failed", e);
        }
        ws.addEventListener("message", function (ev) {
          try {
            var attr = _attribution("ws_message");
            var summary = _summarizeFrame(ev && ev.data);
            post(_merge({
              kind: "network_request",
              page_url: location.href,
              raw_event_kind: "ws_message",
              request_id: _newEventId(),
              method: "WS",
              url: channelUrl,
              started_at: _now(),
              finished_at: _now(),
              status: 0,
              mutation_summary: summary,
              initiator_event_id: (activeInteraction && _isWithinWindow())
                ? activeInteraction.id : null,
            }, attr));
          } catch (e) {
            if (DEBUG) console.warn("[cp] ws message emit failed", e);
          }
        });
        return ws;
      };
      window.WebSocket.prototype = OrigWS.prototype;
    }
    if (typeof window.EventSource === "function" && !window.__cp_es_hooked) {
      window.__cp_es_hooked = true;
      var OrigES = window.EventSource;
      window.EventSource = function (url, init) {
        var es = init !== undefined
          ? new OrigES(url, init) : new OrigES(url);
        var channelUrl = String(url || "");
        try {
          var openAttr = _attribution("sse_open");
          post(_merge({
            kind: "network_request",
            page_url: location.href,
            raw_event_kind: "sse_open",
            request_id: _newEventId(),
            method: "SSE",
            url: channelUrl,
            started_at: _now(),
            initiator_event_id: (activeInteraction && _isWithinWindow())
              ? activeInteraction.id : null,
          }, openAttr));
        } catch (e) {
          if (DEBUG) console.warn("[cp] sse open emit failed", e);
        }
        es.addEventListener("message", function (ev) {
          try {
            var attr = _attribution("sse_message");
            var summary = _summarizeFrame(ev && ev.data);
            post(_merge({
              kind: "network_request",
              page_url: location.href,
              raw_event_kind: "sse_message",
              request_id: _newEventId(),
              method: "SSE",
              url: channelUrl,
              started_at: _now(),
              finished_at: _now(),
              status: 0,
              mutation_summary: summary,
              initiator_event_id: (activeInteraction && _isWithinWindow())
                ? activeInteraction.id : null,
            }, attr));
          } catch (e) {
            if (DEBUG) console.warn("[cp] sse message emit failed", e);
          }
        });
        return es;
      };
      window.EventSource.prototype = OrigES.prototype;
    }
  })();

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
          // WI-44: aria-invalid='true' means the field just got
          // flagged as invalid by the page's validator (either client
          // or server-side, surfaced via the field's attribute).
          // Emit a validation_invalid signal so the annotator can
          // emit a validation_field assertion AND the runner's
          // post-action _check_validation_errors auto-detection has
          // the trace evidence to surface a server_validation
          // failure.
          if (
            name === "aria-invalid"
            && t.getAttribute("aria-invalid") === "true"
          ) {
            _emitReadiness("validation_invalid", t, "true");
          }
        }
      });
      mo.observe(root, {
        attributes: true,
        subtree: true,
        attributeFilter: ["aria-busy", "disabled", "aria-invalid"],
      });
    } catch (e) {
      if (DEBUG) console.warn("[cp] readiness watcher failed", e);
    }
  }

  function _readinessSelector(el) {
    // Build a stable selector for the observed element. Prefer
    // data-testid -> id -> tag+class.
    var tid = el.getAttribute && el.getAttribute("data-testid");
    // WI-24: escape the test_id via CSS.escape so testids containing
    // ``]`` / quotes / spaces / colons / non-ASCII don't produce an
    // unparseable selector. Pre-WI-24 the raw string was concatenated.
    if (tid) return "[data-testid=\"" + tid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
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

  // ---- WI-34: dialog mount/unmount watcher --------------------------------
  //
  // Detects modals appearing or disappearing during an active user
  // interaction. Emits ``kind: "modal"`` TraceEvents with dialog_state
  // ("open" / "closed"), dialog_selector (stable; testid > role+nth),
  // and dialog_aria_modal. The annotator pairs these with the causing
  // user action via caused_by (set by _attribution when an interaction
  // is active) and folds them into the step's effects.modal field.
  //
  // We track CURRENTLY-visible dialog elements in a Set so we can
  // distinguish "newly appeared" from "already-there." The set is
  // refreshed on every mutation tick rather than on full DOM scans;
  // the cost is one querySelectorAll('[role="dialog"]') per mutation
  // burst which is cheap compared to fingerprinting.
  function _installDialogWatcher() {
    if (window.__cp_dialog_installed) return;
    window.__cp_dialog_installed = true;
    // Seed with whatever dialogs are already mounted at install time
    // so "we just installed" doesn't appear as a mount event.
    var trackedDialogs = new WeakSet();
    try {
      var initial = document.querySelectorAll('[role="dialog"], dialog');
      for (var i = 0; i < initial.length; i++) {
        if (_isElementVisible(initial[i])) {
          trackedDialogs.add(initial[i]);
        }
      }
    } catch (e) {}

    function _isElementVisible(el) {
      if (!el || !el.getBoundingClientRect) return false;
      try {
        var rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return false;
        var style = (el.ownerDocument && el.ownerDocument.defaultView)
          ? el.ownerDocument.defaultView.getComputedStyle(el)
          : null;
        if (style && (style.display === "none" || style.visibility === "hidden")) {
          return false;
        }
      } catch (e) {}
      return true;
    }

    function _dialogSelector(el) {
      // Prefer testid; fall back to role + index among visible dialogs
      // so multiple stacked dialogs each get a distinct selector.
      var tid = el.getAttribute && el.getAttribute("data-testid");
      if (tid) {
        return "[data-testid=\"" + tid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
      }
      var id = el.id;
      if (id) return "#" + cssEscape(id);
      // Compute the dialog's index among the currently-visible dialogs
      // (DOM order). Stable enough for replay so long as the dialog
      // count is consistent.
      try {
        var all = document.querySelectorAll('[role="dialog"], dialog');
        var idx = 0;
        for (var i = 0; i < all.length; i++) {
          if (all[i] === el) break;
          if (_isElementVisible(all[i])) idx++;
        }
        return '[role="dialog"]:nth-of-type(' + (idx + 1) + ')';
      } catch (e) {}
      return '[role="dialog"]';
    }

    function _emitDialog(el, state) {
      // Modal events are CONSEQUENCE events -- caused_by is the active
      // interaction (the click that opened/closed the dialog). When no
      // interaction is active (background-mounted dialogs / toasts that
      // claim role=dialog), we still emit but caused_by=null.
      var attr = _attribution("dialog_observer");
      var ariaModal = null;
      try {
        var amv = el.getAttribute && el.getAttribute("aria-modal");
        if (amv !== null && amv !== undefined) {
          ariaModal = amv === "true";
        }
      } catch (e) {}
      try {
        post(_merge({
          kind: "modal",
          page_url: location.href,
          raw_event_kind: "dialog_" + state,
          dialog_state: state,
          dialog_selector: _dialogSelector(el),
          dialog_aria_modal: ariaModal,
          // initiator_event_id mirrors caused_by but is the explicit
          // F-07 field used by the annotator for non-network events.
          initiator_event_id: (activeInteraction && _isWithinWindow())
            ? activeInteraction.id : null,
        }, attr));
      } catch (e) {
        if (DEBUG) console.warn("[cp] modal emit failed", e);
      }
    }

    try {
      var root = document.body || document.documentElement;
      if (!root) {
        return setTimeout(_installDialogWatcher, 100);
      }
      var mo = new MutationObserver(function (records) {
        // Find all currently-visible dialogs. Anything in the tracked
        // set that's no longer visible -> emit "closed." Anything
        // newly visible -> emit "open."
        var currentlyVisible = [];
        try {
          var all = document.querySelectorAll('[role="dialog"], dialog');
          for (var i = 0; i < all.length; i++) {
            if (_isElementVisible(all[i])) {
              currentlyVisible.push(all[i]);
            }
          }
        } catch (e) {}
        // Detect mounts.
        for (var j = 0; j < currentlyVisible.length; j++) {
          var el = currentlyVisible[j];
          if (!trackedDialogs.has(el)) {
            trackedDialogs.add(el);
            _emitDialog(el, "open");
          }
        }
        // Detect unmounts -- we can't iterate a WeakSet, so we walk
        // the mutation records and check the targets. Targets removed
        // from the DOM (or whose subtree had role=dialog removed) get
        // visited here.
        for (var k = 0; k < records.length; k++) {
          var r = records[k];
          if (!r.removedNodes) continue;
          for (var m = 0; m < r.removedNodes.length; m++) {
            var rn = r.removedNodes[m];
            if (rn && rn.nodeType === 1 && trackedDialogs.has(rn)) {
              trackedDialogs.delete(rn);
              _emitDialog(rn, "closed");
            }
            // Also walk into the removed subtree for nested dialogs.
            try {
              if (rn && rn.querySelectorAll) {
                var inner = rn.querySelectorAll('[role="dialog"], dialog');
                for (var n = 0; n < inner.length; n++) {
                  if (trackedDialogs.has(inner[n])) {
                    trackedDialogs.delete(inner[n]);
                    _emitDialog(inner[n], "closed");
                  }
                }
              }
            } catch (e) {}
          }
        }
        // Attribute changes that hide a dialog (display:none,
        // aria-hidden=true) also count as close events.
        for (var p = 0; p < records.length; p++) {
          var rec = records[p];
          if (rec.type !== "attributes") continue;
          var t = rec.target;
          if (!t || t.nodeType !== 1) continue;
          var role = t.getAttribute && t.getAttribute("role");
          var isDialog = role === "dialog" ||
            (t.tagName && t.tagName.toLowerCase() === "dialog");
          if (!isDialog) continue;
          if (trackedDialogs.has(t) && !_isElementVisible(t)) {
            trackedDialogs.delete(t);
            _emitDialog(t, "closed");
          } else if (!trackedDialogs.has(t) && _isElementVisible(t)) {
            trackedDialogs.add(t);
            _emitDialog(t, "open");
          }
        }
      });
      mo.observe(root, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["aria-hidden", "style", "class", "hidden", "open"],
      });
    } catch (e) {
      if (DEBUG) console.warn("[cp] dialog watcher install failed", e);
    }
  }
  _installDialogWatcher();

  // ---- WI-42: toast / snackbar watcher -----------------------------------
  //
  // Toasts appear after save / publish / delete actions to confirm
  // success or signal a conflict. The grabber watches for role=status
  // / role=alert nodes appearing during a user interaction window AND
  // for common toast-container patterns (react-toastify, sonner,
  // antd-message, .toast / .snackbar generic classes).
  //
  // Emits ONE ``kind='toast'`` event per appearance with toast_text,
  // toast_level, toast_selector, and toast_action_buttons. The
  // annotator folds toasts into the causing step's effects.toast
  // (ToastEffect).
  function _installToastWatcher() {
    if (window.__cp_toast_installed) return;
    window.__cp_toast_installed = true;

    var trackedToasts = (typeof WeakSet === "function") ? new WeakSet() : null;
    function _wasTracked(el) {
      if (!trackedToasts) return false;
      try { return trackedToasts.has(el); } catch (e) { return false; }
    }
    function _markTracked(el) {
      if (!trackedToasts) return;
      try { trackedToasts.add(el); } catch (e) {}
    }

    var TOAST_TESTID_RE = /toast|snackbar|notification|alert/i;
    var TOAST_CLASS_RE = /(^|\s)(toast|snackbar|notification|Toastify__toast|sonner|ant-message|ant-notification)(\s|$|-|_)/i;

    function _isToastEl(el) {
      if (!el || el.nodeType !== 1) return false;
      var role = el.getAttribute && el.getAttribute("role");
      if (role === "status" || role === "alert") return true;
      var tid = el.getAttribute && el.getAttribute("data-testid");
      if (tid && TOAST_TESTID_RE.test(tid)) return true;
      var cls = (el.className && typeof el.className === "string")
        ? el.className : "";
      if (cls && TOAST_CLASS_RE.test(cls)) return true;
      return false;
    }

    function _inferLevel(el) {
      var role = el.getAttribute && el.getAttribute("role");
      if (role === "alert") return "error";
      var cls = (el.className && typeof el.className === "string")
        ? el.className.toLowerCase() : "";
      if (/(toast|message|alert|notification)[-_]error|error[-_](toast|message)|--error|\bfailure\b/.test(cls)) {
        return "error";
      }
      if (/--success|success[-_]|\bsuccess\b/.test(cls)) return "success";
      if (/--warning|warning[-_]|\bwarn\b/.test(cls)) return "warning";
      return "info";
    }

    function _toastSelector(el) {
      var tid = el.getAttribute && el.getAttribute("data-testid");
      if (tid) {
        return "[data-testid=\"" + tid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
      }
      if (el.id) return "#" + cssEscape(el.id);
      var role = el.getAttribute && el.getAttribute("role");
      if (role) return "[role=\"" + role + "\"]";
      return buildCssPath(el);
    }

    function _collectActionButtons(el) {
      var out = [];
      try {
        var btns = el.querySelectorAll("button, [role='button'], a[href]");
        for (var i = 0; i < btns.length && i < 5; i++) {
          var b = btns[i];
          var lbl = trim(b.innerText || b.textContent || b.getAttribute("aria-label") || "");
          if (!lbl) continue;
          var bt = b.getAttribute && b.getAttribute("data-testid");
          // Infer action_kind from the label
          var akind = "view";
          var lower = lbl.toLowerCase();
          if (/undo|revert/.test(lower)) akind = "undo";
          else if (/retry|try again/.test(lower)) akind = "retry";
          else if (/dismiss|close|×/.test(lower)) akind = "dismiss";
          out.push({
            label: lbl.slice(0, 60),
            test_id: bt || null,
            action_kind: akind,
          });
        }
      } catch (e) {}
      return out;
    }

    function _emitToast(el) {
      if (_wasTracked(el)) return;
      _markTracked(el);
      var text = "";
      try {
        text = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (text.length > 512) text = text.slice(0, 512) + "...";
      } catch (e) {}
      if (!text) return;
      var level = _inferLevel(el);
      var sel = _toastSelector(el);
      var buttons = _collectActionButtons(el);
      var attr = _attribution("toast_observer");
      try {
        post(_merge({
          kind: "toast",
          page_url: location.href,
          raw_event_kind: "toast_appeared",
          toast_text: text,
          toast_level: level,
          toast_selector: sel,
          toast_action_buttons: buttons.length ? buttons : null,
          initiator_event_id: (activeInteraction && _isWithinWindow())
            ? activeInteraction.id : null,
        }, attr));
      } catch (e) {
        if (DEBUG) console.warn("[cp] toast emit failed", e);
      }
    }

    try {
      var root = document.body || document.documentElement;
      if (!root) {
        return setTimeout(_installToastWatcher, 100);
      }
      var mo = new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i++) {
          var r = records[i];
          if (!r.addedNodes) continue;
          for (var j = 0; j < r.addedNodes.length; j++) {
            var n = r.addedNodes[j];
            if (!n || n.nodeType !== 1) continue;
            if (_isToastEl(n)) {
              _emitToast(n);
              continue;
            }
            // Walk shallow children too (some toast libs mount inside
            // a wrapper).
            try {
              if (n.querySelectorAll) {
                var inner = n.querySelectorAll(
                  "[role='alert'], [role='status'], [data-testid*='toast' i], [data-testid*='snackbar' i], [class*='toast' i], [class*='snackbar' i]"
                );
                for (var k = 0; k < inner.length && k < 5; k++) {
                  if (_isToastEl(inner[k])) _emitToast(inner[k]);
                }
              }
            } catch (e) {}
          }
        }
      });
      mo.observe(root, { childList: true, subtree: true });
    } catch (e) {
      if (DEBUG) console.warn("[cp] toast watcher install failed", e);
    }
  }
  _installToastWatcher();

  // ---- WI-37: scroll-to-find-row capture ---------------------------------
  //
  // Scroll events are normally filtered (the very first line of this
  // file lists them as noise). For WI-37 we capture scrolls ONLY when:
  //   (a) the scroll target carries a scrollable role (table / listbox
  //       / tree / grid / generic scrollable div with overflow:auto), AND
  //   (b) the scroll happens during an active user interaction window.
  //
  // Each captured scroll emits a kind="visibility_change" TraceEvent
  // with scroller_selector + scroll_direction. The annotator detects
  // "operator scrolled to find a row" by pairing the scrolls with a
  // subsequent click inside the same scroller, and emits a
  // scroll_until step before the click.
  function _installScrollObserver() {
    if (window.__cp_scroll_installed) return;
    window.__cp_scroll_installed = true;
    var lastScrollByTarget = (typeof WeakMap === "function")
      ? new WeakMap()
      : new Map();
    document.addEventListener(
      "scroll",
      function (e) {
        var target = e.target;
        if (!target || target.nodeType !== 1) {
          // The document itself scrolls; convert to the documentElement.
          if (target === document) target = document.documentElement;
          else return;
        }
        // We only care about scrollable containers, not the window /
        // documentElement (those are typically incidental nav scrolls).
        if (
          target === document.documentElement ||
          target === document.body
        ) {
          return;
        }
        // Track scroll deltas; only emit when there's an active user
        // interaction (the operator scrolling intentionally to find a
        // row). Background-driven scrolls (auto-scroll from a focus)
        // don't get captured.
        var prev = lastScrollByTarget.get(target);
        var top = target.scrollTop || 0;
        if (prev === undefined) {
          lastScrollByTarget.set(target, top);
          return;
        }
        var delta = top - prev;
        lastScrollByTarget.set(target, top);
        if (Math.abs(delta) < 16) return;  // sub-pixel noise threshold
        if (!activeInteraction || !_isWithinWindow()) return;
        var sel = null;
        try {
          var tid = target.getAttribute && target.getAttribute("data-testid");
          if (tid) {
            sel = "[data-testid=\"" + tid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
          } else if (target.id) {
            sel = "#" + cssEscape(target.id);
          } else {
            sel = buildCssPath(target);
          }
        } catch (er) {}
        if (!sel) return;
        try {
          var attr = _attribution("scroll_observer");
          post(_merge({
            kind: "visibility_change",
            page_url: location.href,
            raw_event_kind: "scroll",
            scroller_selector: sel,
            scroll_direction: delta > 0 ? "down" : "up",
            // visible_row_count_delta is not measured here; the
            // annotator only needs the direction + scroller identity
            // to detect "operator scrolled to find a row" with the
            // subsequent click inside the same scroller.
            visible_row_count_delta: null,
            initiator_event_id: activeInteraction.id,
          }, attr));
        } catch (e2) {
          if (DEBUG) console.warn("[cp] scroll emit failed", e2);
        }
      },
      true,
    );
  }
  _installScrollObserver();

  // Module-level visibility helper -- duplicates the one inside
  // _installDialogWatcher (which is scoped). Cheap to define twice;
  // moving them into one place is out of scope for WI-40 (and would
  // re-touch every observer install site).
  function _isHoverElVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    try {
      var rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return false;
      var style = (el.ownerDocument && el.ownerDocument.defaultView)
        ? el.ownerDocument.defaultView.getComputedStyle(el)
        : null;
      if (style && (style.display === "none" || style.visibility === "hidden")) {
        return false;
      }
    } catch (e) {}
    return true;
  }

  // ---- WI-40: hover -> submenu reveal capture ----------------------------
  //
  // Hover menus / mega menus: the operator moves the cursor over a
  // parent trigger, the page renders a submenu container, and the
  // operator clicks a child item inside. The grabber records the click
  // normally; this watcher pairs it with the originating hover so the
  // annotator can fold them into ONE click step with a HoverEffect.
  //
  // We don't capture every pointerenter (the page is full of incidental
  // hovers). Only hovers that:
  //   (a) targeted a hoverable parent (role=menuitem / menubar / has
  //       aria-haspopup OR a known menu testid pattern), AND
  //   (b) were followed by a NEW visible element appearing inside or
  //       near the hovered parent within HOVER_REVEAL_WINDOW_MS
  // produce a kind="hover" event.
  //
  // The annotator pairs the hover with the next click whose target is
  // a descendant of the hovered parent's revealed submenu.
  function _installHoverWatcher() {
    if (window.__cp_hover_installed) return;
    window.__cp_hover_installed = true;
    var HOVER_REVEAL_WINDOW_MS = 800;  // generous; portals vary
    var pendingHover = null;
    var pendingHoverTimer = null;

    function _isHoverTrigger(el) {
      if (!el || el.nodeType !== 1) return false;
      // Explicit menu-trigger markers
      if (el.getAttribute && el.getAttribute("aria-haspopup")) return true;
      var role = el.getAttribute && el.getAttribute("role");
      if (role === "menuitem" || role === "menubar" || role === "menu") return true;
      // Common testid / class patterns. Conservative: only well-known
      // mega-menu markers, not arbitrary nav links (those produce too
      // many false-positive hovers).
      var tid = el.getAttribute && el.getAttribute("data-testid");
      if (tid && /menu|nav-|mega-|dropdown/i.test(tid)) return true;
      var cls = (el.className && typeof el.className === "string")
        ? el.className.toLowerCase() : "";
      if (/menu-trigger|mega-menu|has-submenu|dropdown-toggle/.test(cls)) {
        return true;
      }
      return false;
    }

    function _snapshotVisibleSet(scope) {
      // Snapshot a Set of visible elements inside the given scope. Used
      // to detect "new element appeared" between pre-hover and post-
      // hover. Bounded by 200 to cap cost for huge subtrees.
      var out = new Set();
      try {
        var nodes = scope.querySelectorAll("*");
        var cap = Math.min(nodes.length, 200);
        for (var i = 0; i < cap; i++) {
          var n = nodes[i];
          if (n.nodeType === 1 && _isHoverElVisible(n)) {
            out.add(n);
          }
        }
      } catch (e) {}
      return out;
    }

    function _findRevealedSubmenu(trigger, preVisibleSet) {
      // Walk up to find the menu's container (typically the trigger's
      // parent or grandparent), then find a child of the container
      // that's now visible but wasn't in the pre-hover snapshot.
      //
      // Followup #3: surface the loop cap through PortalContext.wait_policy
      // .hover_submenu_search_cap. Pushed by the runner onto
      // window.__cp_hover_submenu_search_cap. Literal 200 remains as
      // last-resort fallback (covers typical mega-menu depths).
      var SEARCH_CAP = (typeof window.__cp_hover_submenu_search_cap === "number"
        && window.__cp_hover_submenu_search_cap > 0)
        ? window.__cp_hover_submenu_search_cap : 200;
      try {
        var scope = trigger.parentElement || trigger;
        var nodes = scope.querySelectorAll("*");
        for (var i = 0; i < nodes.length && i < SEARCH_CAP; i++) {
          var n = nodes[i];
          if (n.nodeType !== 1) continue;
          if (n === trigger) continue;
          if (preVisibleSet.has(n)) continue;
          if (!_isHoverElVisible(n)) continue;
          // Prefer a node carrying role=menu / role=listbox / aria-
          // expanded=true; fall back to the first visible non-trigger
          // descendant.
          var role = n.getAttribute && n.getAttribute("role");
          if (role === "menu" || role === "listbox") {
            return n;
          }
        }
        // Fallback: first newly-visible element
        for (var j = 0; j < nodes.length && j < SEARCH_CAP; j++) {
          var m = nodes[j];
          if (m.nodeType !== 1 || m === trigger) continue;
          if (preVisibleSet.has(m)) continue;
          if (_isHoverElVisible(m)) return m;
        }
      } catch (e) {}
      return null;
    }

    document.addEventListener(
      "pointerenter",
      function (e) {
        var target = e.target;
        if (!target || target.nodeType !== 1) return;
        // Walk up to a hover trigger if the pointer entered a descendant
        // of the trigger (icon inside the menu button).
        var trigger = target;
        var depth = 0;
        while (trigger && depth < 5 && !_isHoverTrigger(trigger)) {
          trigger = trigger.parentElement;
          depth++;
        }
        if (!trigger || !_isHoverTrigger(trigger)) return;
        // Snapshot pre-hover visible set inside the trigger's container
        // so we can diff after the window.
        var scope = trigger.parentElement || trigger;
        var preVisible = _snapshotVisibleSet(scope);
        var hoverStart = _now();
        pendingHover = {
          trigger: trigger,
          scope: scope,
          preVisible: preVisible,
          ts: hoverStart,
        };
        if (pendingHoverTimer) clearTimeout(pendingHoverTimer);
        pendingHoverTimer = setTimeout(function () {
          if (!pendingHover || pendingHover.trigger !== trigger) return;
          // Window expired; check for a revealed submenu.
          var revealed = _findRevealedSubmenu(trigger, preVisible);
          if (revealed) {
            var dwell = Math.round(_now() - hoverStart);
            var revealedSel = null;
            try {
              var rtid = revealed.getAttribute && revealed.getAttribute("data-testid");
              if (rtid) {
                revealedSel = "[data-testid=\"" + rtid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
              } else if (revealed.id) {
                revealedSel = "#" + cssEscape(revealed.id);
              } else {
                revealedSel = buildCssPath(revealed);
              }
            } catch (er) {}
            // Emit ONE hover event for the trigger -> reveal pair.
            // We open a fresh interaction so the subsequent child
            // click attributes correctly via causality.
            var attr = _rootAttribution("user_hover");
            _setActiveInteraction("hover", attr.event_id);
            try {
              post(_merge({
                kind: "hover",
                fingerprint: fingerprint(trigger),
                page_url: location.href,
                raw_event_kind: "pointerenter",
                submenu_selector: revealedSel,
                dwell_ms: dwell,
              }, attr));
            } catch (he) {
              if (DEBUG) console.warn("[cp] hover emit failed", he);
            }
          }
          pendingHover = null;
          pendingHoverTimer = null;
        }, HOVER_REVEAL_WINDOW_MS);
      },
      true
    );
    // Cancel pending hover detection if the operator clicks before the
    // window expires -- the click handler will still get the click
    // normally; we just don't want a delayed phantom hover to fire
    // after the operator already committed.
    document.addEventListener(
      "click",
      function () {
        if (pendingHoverTimer) {
          // Force an immediate check so a hover-revealed submenu that
          // the operator IS clicking inside still emits its hover
          // event BEFORE the click. The setTimeout callback runs the
          // diff logic; trigger it now.
          var t = pendingHoverTimer;
          pendingHoverTimer = null;
          clearTimeout(t);
          if (pendingHover) {
            var trig = pendingHover.trigger;
            var preV = pendingHover.preVisible;
            var startedTs = pendingHover.ts;
            pendingHover = null;
            var revealed = _findRevealedSubmenu(trig, preV);
            if (revealed) {
              var dwell = Math.round(_now() - startedTs);
              var revealedSel = null;
              try {
                var rtid = revealed.getAttribute && revealed.getAttribute("data-testid");
                if (rtid) {
                  revealedSel = "[data-testid=\"" + rtid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
                } else if (revealed.id) {
                  revealedSel = "#" + cssEscape(revealed.id);
                } else {
                  revealedSel = buildCssPath(revealed);
                }
              } catch (er) {}
              var attr = _rootAttribution("user_hover");
              _setActiveInteraction("hover", attr.event_id);
              try {
                post(_merge({
                  kind: "hover",
                  fingerprint: fingerprint(trig),
                  page_url: location.href,
                  raw_event_kind: "pointerenter_pre_click",
                  submenu_selector: revealedSel,
                  dwell_ms: dwell,
                }, attr));
              } catch (he) {
                if (DEBUG) console.warn("[cp] hover pre-click emit failed", he);
              }
            }
          }
        }
      },
      true
    );
  }
  _installHoverWatcher();

  // ---- WI-35: window.open / popup hook ------------------------------------
  //
  // Hook window.open synchronously inside a user interaction so popups
  // are attributed to the click that opened them. Each call emits a
  // ``kind: "popup"`` TraceEvent with popup_url, popup_target,
  // popup_features, and popup_binding_key (a stable key the runner can
  // use to register the new page in its page registry).
  //
  // Also detects target=_blank link clicks by inspecting the click
  // target inside the click handler -- those don't fire window.open
  // but the browser still opens a new tab.
  (function _installPopupHook() {
    if (window.__cp_popup_hooked) return;
    window.__cp_popup_hooked = true;
    var _origOpen = window.open;
    if (typeof _origOpen !== "function") return;
    var _popupSeq = 0;
    window.open = function (url, target, features) {
      try {
        var attr = _attribution("window_open");
        var bindingKey = "popup_" + (++_popupSeq);
        post(_merge({
          kind: "popup",
          page_url: location.href,
          raw_event_kind: "window_open",
          popup_url: url ? String(url) : null,
          popup_target: target ? String(target) : null,
          popup_features: features ? String(features) : null,
          popup_binding_key: bindingKey,
          initiator_event_id: (activeInteraction && _isWithinWindow())
            ? activeInteraction.id : null,
        }, attr));
      } catch (e) {
        if (DEBUG) console.warn("[cp] popup emit failed", e);
      }
      return _origOpen.apply(this, arguments);
    };
  })();

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

    // 2026-06-02 diagnosis: Angular Material portals use two extra
    // labeling patterns that the WAI-ARIA cascade above misses.
    // (B2.a) mat-label / .mat-form-field-label INSIDE the same
    //   mat-form-field as the clicked element. Excludes the
    //   mat-select-value-text span (which carries the CURRENT VALUE,
    //   not the label).
    // (B2.b) preceding-sibling <label> outside a "field container"
    //   (mat-form-field, ng-multiselect-dropdown, .form-group,
    //   .form-field, or a generic div that contains both the element
    //   and a sibling <label>). Cap the walk at 4 levels so non-
    //   Material portals don't pay for an unbounded climb.
    try {
      var matFormField = el.closest && el.closest("mat-form-field");
      if (matFormField) {
        var inner = matFormField.querySelector(
          "mat-label, .mat-form-field-label"
        );
        if (inner && !inner.classList.contains("mat-select-value-text")) {
          var inText = trim(inner.textContent || "");
          if (inText) return inText;
        }
      }
      var container = _findFieldContainer(el);
      if (container) {
        var sibLabel = _findPrecedingSiblingLabel(container, el);
        if (sibLabel) {
          var sibText = trim(sibLabel.textContent || "");
          if (sibText) return sibText;
        }
      }
    } catch (eLab) {
      if (DEBUG) console.warn("[cp] extended label discovery failed", eLab);
    }

    if (el.alt) return trim(el.alt);
    if (el.title) return trim(el.title);
    if (el.placeholder) return trim(el.placeholder);
    return trim(el.innerText || el.textContent || "").slice(0, 120);
  }

  // 2026-06-02 B2: walk up to a "field container" for sibling-label
  // discovery. The container is the nearest ancestor that is one of:
  //   - <mat-form-field>
  //   - <ng-multiselect-dropdown>
  //   - .form-group / .form-field
  //   - a <div> that contains BOTH the original element AND a sibling
  //     <label> element (the hand-authored pattern where a row is just
  //     "<div><label>X</label> <some-input/></div>")
  // Capped at 4 levels so non-Material pages don't pay for the walk.
  function _findFieldContainer(el) {
    if (!el) return null;
    var cur = el.parentElement;
    var depth = 0;
    while (cur && cur.nodeType === 1 && depth < 4) {
      var tag = (cur.tagName || "").toLowerCase();
      if (tag === "mat-form-field" || tag === "ng-multiselect-dropdown") {
        return cur;
      }
      var cls = (cur.className && typeof cur.className === "string")
        ? cur.className : "";
      if (/(^|\s)form-group(\s|$)/.test(cls) ||
          /(^|\s)form-field(\s|$)/.test(cls)) {
        return cur;
      }
      // Generic div with sibling <label>: the cur div contains both the
      // original element and a <label> child that is NOT an ancestor of
      // the original element. We prefer the outermost such container.
      if (tag === "div") {
        var kids = cur.children || [];
        for (var i = 0; i < kids.length; i++) {
          if (kids[i].tagName && kids[i].tagName.toLowerCase() === "label"
              && !kids[i].contains(el)) {
            return cur;
          }
        }
      }
      cur = cur.parentElement;
      depth++;
    }
    return null;
  }

  // 2026-06-02 B2: find a preceding-sibling <label> for a field
  // container. Two real-world shapes to handle:
  //   Shape A: the container is the wrapping div that holds BOTH the
  //     label and the field as direct children
  //     (<div><label>X</label><mat-form-field/></div>). We look INSIDE
  //     the container for the closest direct-child <label> that is NOT
  //     an ancestor of ``origEl``.
  //   Shape B: the container IS the field (e.g. <mat-form-field>) and
  //     its previousElementSibling is the <label>:
  //     <label>X</label><mat-form-field>... So we walk
  //     previousElementSibling up to 3 hops.
  // origEl is the element the operator clicked -- needed for Shape A
  // so we don't return an ancestor-of-el as the "sibling" label.
  function _findPrecedingSiblingLabel(container, origEl) {
    if (!container) return null;
    // Shape A: a child <label> NOT containing origEl.
    if (container.children) {
      for (var i = 0; i < container.children.length; i++) {
        var c = container.children[i];
        if (c.tagName && c.tagName.toLowerCase() === "label"
            && (!origEl || !c.contains(origEl))) {
          return c;
        }
      }
    }
    // Shape B: previousElementSibling chain.
    var prev = container.previousElementSibling;
    var hops = 0;
    while (prev && hops < 3) {
      if (prev.tagName && prev.tagName.toLowerCase() === "label") return prev;
      // Hand-authored markup sometimes wraps a label in a span. Peek in.
      if (prev.querySelector) {
        var nested = prev.querySelector("label");
        if (nested && (!origEl || !nested.contains(origEl))) return nested;
      }
      prev = prev.previousElementSibling;
      hops++;
    }
    return null;
  }

  // 2026-06-02 B1: resolve a click target up to the nearest "semantic
  // widget root" for Angular Material / hand-authored custom widgets.
  // The grabber's fingerprint should describe the widget the operator
  // INTERACTED WITH, not the inner <div> that happened to receive the
  // click. We walk up at most 6 levels looking for one of the semantic
  // tags; on a non-Material portal the walk falls off the end and we
  // return ``el`` unchanged, so existing behavior is preserved.
  //
  // Preference order (so a click inside .mat-select-trigger inside a
  // mat-select inside a mat-form-field resolves to the mat-select, not
  // the mat-form-field): mat-select > mat-checkbox / mat-radio-button /
  // mat-slide-toggle > ng-multiselect-dropdown > mat-form-field.
  var _SEMANTIC_TAGS = {
    "mat-select": 1,
    "mat-checkbox": 1,
    "mat-radio-button": 1,
    "mat-slide-toggle": 1,
    "ng-multiselect-dropdown": 1,
    "mat-form-field": 1,
  };
  var _SEMANTIC_CLASSES = [
    "mat-select-trigger",
    "mat-form-field-flex",
    "mat-checkbox-layout",
  ];

  function _resolveSemanticTarget(el) {
    if (!el || el.nodeType !== 1) return el;
    var cur = el;
    var depth = 0;
    var hit = null;
    var preferredOrder = ["mat-select", "mat-checkbox", "mat-radio-button",
      "mat-slide-toggle", "ng-multiselect-dropdown", "mat-form-field"];
    while (cur && cur.nodeType === 1 && depth < 6) {
      var tag = (cur.tagName || "").toLowerCase();
      if (_SEMANTIC_TAGS[tag]) {
        if (!hit) hit = cur;
        else {
          // Replace ONLY when the new candidate ranks higher in
          // preferredOrder than the existing hit -- a mat-select wins
          // over a mat-form-field, but mat-form-field doesn't replace
          // a mat-select we already found.
          var newRank = preferredOrder.indexOf(tag);
          var curHitTag = (hit.tagName || "").toLowerCase();
          var oldRank = preferredOrder.indexOf(curHitTag);
          if (newRank >= 0 && (oldRank < 0 || newRank < oldRank)) {
            hit = cur;
          }
        }
      } else {
        var cls = (cur.className && typeof cur.className === "string")
          ? cur.className : "";
        for (var i = 0; i < _SEMANTIC_CLASSES.length; i++) {
          var pat = _SEMANTIC_CLASSES[i];
          if (cls.split(/\s+/).indexOf(pat) >= 0) {
            // semantic class hit -- mark cur (the div carrying the
            // class) but keep walking to find the outer semantic root.
            if (!hit) hit = cur;
            break;
          }
        }
      }
      cur = cur.parentElement;
      depth++;
    }
    return hit || el;
  }

  // 2026-06-02 B3: capture a human-readable display value for the
  // common Material widgets. Returns null for widgets that don't expose
  // a steady-state value (buttons, links, etc.). Capped at 120 chars to
  // bound payload size for accidental multi-line captures.
  function _currentDisplayValue(el) {
    if (!el || el.nodeType !== 1) return null;
    var tag = (el.tagName || "").toLowerCase();
    try {
      if (tag === "mat-select") {
        var vt = el.querySelector(".mat-select-value-text");
        if (vt) {
          var t = trim(vt.textContent || "");
          if (t) return t.slice(0, 120);
        }
        // Fallback to the mat-select's own textContent if no value-text
        // span exists (the picker may be empty -- return null then).
        return null;
      }
      if (tag === "mat-checkbox") {
        // aria-checked on the inner input is the source of truth; the
        // outer mat-checkbox carries the mat-checkbox-checked class
        // when the box is ticked.
        var input = el.querySelector("input.mat-checkbox-input, input[type='checkbox']");
        if (input) {
          var ac = input.getAttribute("aria-checked");
          if (ac === "true") return "true";
          if (ac === "false") return "false";
          if (input.checked !== undefined) return input.checked ? "true" : "false";
        }
        var cls = (el.className && typeof el.className === "string")
          ? el.className : "";
        if (/(^|\s)mat-checkbox-checked(\s|$)/.test(cls)) return "true";
        return "false";
      }
      if (tag === "mat-radio-button" || tag === "mat-slide-toggle") {
        var rinput = el.querySelector("input");
        if (rinput) {
          var ra = rinput.getAttribute("aria-checked");
          if (ra === "true" || ra === "false") return ra;
          if (rinput.checked !== undefined) return rinput.checked ? "true" : "false";
        }
        return null;
      }
      if (tag === "ng-multiselect-dropdown") {
        // Selected chips first; fall back to the visible placeholder.
        var chips = el.querySelectorAll(".selected-item");
        if (chips && chips.length) {
          var parts = [];
          for (var i = 0; i < chips.length; i++) {
            // The chip's textContent includes a trailing "x" close
            // button glyph in the real portal; strip a trailing single
            // "x" so we don't record "Albania x".
            var ct = trim(chips[i].textContent || "");
            if (ct.endsWith(" x")) ct = ct.slice(0, -2).trim();
            else if (ct.endsWith("x") && ct.length > 1) ct = ct.slice(0, -1).trim();
            if (ct) parts.push(ct);
          }
          if (parts.length) return parts.join(", ").slice(0, 120);
        }
        var ph = el.querySelector(".dropdown-btn");
        if (ph) {
          var pt = trim(ph.textContent || "");
          if (pt) return pt.slice(0, 120);
        }
        return null;
      }
      if (tag === "mat-form-field") {
        // A mat-form-field with no inner mat-select still has a value
        // (an <input matinput> inside .mat-form-field-infix). Read it.
        var inp = el.querySelector(".mat-form-field-infix input, .mat-form-field-infix textarea");
        if (inp && inp.value != null) {
          var iv = String(inp.value);
          if (iv) return iv.slice(0, 120);
        }
        var inner2 = el.querySelector(".mat-select-value-text");
        if (inner2) {
          var i2 = trim(inner2.textContent || "");
          if (i2) return i2.slice(0, 120);
        }
        return null;
      }
    } catch (eVal) {
      if (DEBUG) console.warn("[cp] _currentDisplayValue failed", eVal);
    }
    return null;
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
        // WI-24: escape the testid into a double-quoted form. Pre-WI-24
        // any testid containing a single quote / ``]`` / spaces / colons
        // produced an unparseable selector.
        seg += "[data-testid=\"" + tid.replace(/\\/g, "\\\\").replace(/"/g, "\\\"") + "\"]";
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

  // WI-32: walk up from an element through shadow roots and frames
  // building the outer->inner traversal chain. Returns
  // {frame_chain, shadow_path, in_shadow_root, frame_path_legacy}.
  function _buildFramePath(el) {
    var chain = [];
    var shadowSelectors = [];
    var inShadow = false;
    var node = el;
    // Walk inner -> outer; we'll reverse at the end so the chain is
    // ordered from the top document down to the target.
    while (node) {
      var root = (node.getRootNode && node.getRootNode()) || null;
      if (root && root.host) {
        // We're inside a shadow root. Step out to the host element.
        inShadow = true;
        var host = root.host;
        var hostSel = null;
        try { hostSel = buildCssPath(host); } catch (e) {}
        if (hostSel) {
          chain.push({ kind: "shadow", host_selector: hostSel });
          shadowSelectors.push(hostSel);
        }
        node = host;
        continue;
      }
      // Same document tree. Are we inside an iframe?
      var doc = node.ownerDocument;
      var win = doc && doc.defaultView;
      if (win && win.frameElement) {
        // Step out to the iframe element in the parent document.
        var frameSel = null;
        try { frameSel = buildCssPath(win.frameElement); } catch (e) {}
        if (frameSel) {
          chain.push({ kind: "iframe", selector: frameSel });
        }
        node = win.frameElement;
        continue;
      }
      break;
    }
    chain.reverse();  // outer-most first
    return {
      frame_chain: chain,
      shadow_path: shadowSelectors.reverse(),
      in_shadow_root: inShadow,
      // Legacy frame_path: iframe-only selectors so pre-WI-32 runner
      // paths still see something useful when they look at
      // ``frame_path`` instead of ``frame_chain``.
      frame_path_legacy: chain
        .filter(function (s) { return s.kind === "iframe"; })
        .map(function (s) { return s.selector; }),
    };
  }

  function fingerprint(el) {
    if (!el || el.nodeType !== 1) return null;
    var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    var meta = _controlMetadata(el);
    var framePath;
    try {
      framePath = _buildFramePath(el);
    } catch (fpe) {
      framePath = {
        frame_chain: [],
        shadow_path: [],
        in_shadow_root: false,
        frame_path_legacy: [],
      };
    }
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
      frame_path: framePath.frame_path_legacy,
      in_shadow_root: framePath.in_shadow_root,
      frame_chain: framePath.frame_chain,
      shadow_path: framePath.shadow_path,
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
      // 2026-06-02 B3: human-readable current value of the widget. For
      // mat-select this is the .mat-select-value-text (e.g.
      // "18_KANTM2_8K"); for mat-checkbox the "true"/"false" boolean;
      // for ng-multiselect-dropdown the joined chip text. null when
      // the widget doesn't expose a steady-state value.
      current_value: _currentDisplayValue(el),
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

  // ---- multi-select option universe capture --------------------------
  //
  // Locked design decision (2026-05-28 id+label sprint): capture id+label
  // for EVERY option row that renders during a custom multi-select
  // interaction (the whole option universe seen, including server-search
  // results as they come back), not just the clicked one. Emitted on the
  // option/checkbox click AND the search input_change payloads as
  // ``options_seen`` so the annotator can union them into the spec's
  // known_options (feeds label->id resolution at replay, the LLM
  // annotation, and planner clarifying questions).
  //
  // This does NOT touch the native <select> ``options_snapshot`` path --
  // it only fires for the MultiSelect.jsx testid family
  // ({prefix}-toggle / -search / -checkbox-X / -item-X / -chip-X /
  // -popover) so the existing select option-snapshot code is untouched.
  var _MS_PREFIX_SUFFIXES = [
    /-toggle$/,
    /-search$/,
    /-popover$/,
    /-chips$/,
    /-checkbox-[^-].*$/,
    /-item-[^-].*$/,
    /-chip-[^-]+$/,
  ];

  // Given any element, walk up to find the multiselect container's testid
  // prefix (the part before -toggle / -search / -popover / -checkbox-X /
  // ...). Returns null when the element isn't part of a recognized
  // MultiSelect widget.
  function _multiselectPrefixFor(el) {
    var cur = el;
    var depth = 0;
    while (cur && cur.nodeType === 1 && depth < 12) {
      var tid = cur.getAttribute && cur.getAttribute("data-testid");
      if (tid) {
        for (var i = 0; i < _MS_PREFIX_SUFFIXES.length; i++) {
          var m = tid.match(_MS_PREFIX_SUFFIXES[i]);
          if (m) {
            return tid.slice(0, tid.length - m[0].length);
          }
        }
        // A bare ``{prefix}`` container (no suffix) -- MultiSelect.jsx
        // renders the root div with data-testid={prefix}. If it has a
        // descendant popover/toggle with the same prefix, treat it as the
        // prefix directly.
        if (
          cur.querySelector &&
          (cur.querySelector('[data-testid="' + cssEscape(tid) + '-popover"]') ||
            cur.querySelector('[data-testid="' + cssEscape(tid) + '-toggle"]'))
        ) {
          return tid;
        }
      }
      cur = cur.parentElement;
      depth++;
    }
    return null;
  }

  // 2026-06-02 B4: collect mat-option rows from an open overlay panel
  // associated with the given mat-select. id comes from the mat-option's
  // own id attribute (e.g. mat-option-85); label is the option's text.
  // Preference order for finding the panel:
  //   1. aria-owns on the mat-select (panel id explicitly listed),
  //   2. any cdk-overlay-pane currently in the DOM that contains
  //      mat-option rows (the Material runtime renders panels into
  //      .cdk-overlay-container at click time).
  // Returns [] when no panel is open / no options exist.
  function _collectMatSelectOptions(matSelectEl) {
    var out = [];
    var seen = {};
    try {
      var pane = null;
      var owns = matSelectEl.getAttribute && matSelectEl.getAttribute("aria-owns");
      if (owns) {
        var ids = owns.split(/\s+/);
        for (var i = 0; i < ids.length; i++) {
          var byId = document.getElementById(ids[i]);
          if (byId) {
            // Either the id IS the pane, or the pane is its closest
            // .cdk-overlay-pane ancestor / descendant.
            if (byId.classList && byId.classList.contains("cdk-overlay-pane")) {
              pane = byId; break;
            }
            var ancestorPane = byId.closest && byId.closest(".cdk-overlay-pane");
            if (ancestorPane) { pane = ancestorPane; break; }
            // Treat the id as the pane wrapper itself (some portals
            // assign the id to the inner panel, not the .cdk-overlay-pane).
            if (byId.querySelector && byId.querySelector(".mat-option")) {
              pane = byId; break;
            }
          }
        }
      }
      var panes = pane ? [pane]
        : Array.prototype.slice.call(
          document.querySelectorAll(".cdk-overlay-pane"));
      for (var p = 0; p < panes.length; p++) {
        var matOpts = panes[p].querySelectorAll(".mat-option");
        for (var k = 0; k < matOpts.length; k++) {
          var opt = matOpts[k];
          var id = opt.id || ("mat-option-anon-" + k);
          if (seen[id]) continue;
          var labelText = trim(opt.textContent || "");
          seen[id] = 1;
          out.push({ value: id, label: labelText });
        }
        if (out.length) break; // first pane with options wins
      }
    } catch (e) {
      if (DEBUG) console.warn("[cp] _collectMatSelectOptions failed", e);
    }
    return out;
  }

  // Scan a multiselect's currently-rendered option rows and return
  // [{value, label}] for each. id comes from the -checkbox-X / -item-X
  // testid suffix; label is the option's accessible name / row text. The
  // popover may not be in the DOM (closed picker) -- returns [] then.
  function _collectMultiselectOptions(prefix) {
    if (!prefix) return [];
    var out = [];
    var seen = {};
    try {
      var rowSel =
        '[data-testid^="' + cssEscape(prefix) + '-checkbox-"],' +
        '[data-testid^="' + cssEscape(prefix) + '-item-"]';
      var rows = document.querySelectorAll(rowSel);
      for (var i = 0; i < rows.length; i++) {
        var row = rows[i];
        var tid = row.getAttribute("data-testid") || "";
        var id = null;
        var cm = tid.match(/-checkbox-(.+)$/);
        var im = tid.match(/-item-(.+)$/);
        if (cm) id = cm[1];
        else if (im) id = im[1];
        if (id == null || seen[id]) continue;
        // Label: prefer the row's accessible name, then the nearest
        // label/li text, then trimmed text content. The checkbox
        // <input> next to the <span>{name}</span> exposes the name as
        // its accessible name; the row <li> carries the visible text.
        var label = trim(getAccessibleName(row));
        if (!label) {
          // Climb to the row container (li / label) for the visible text.
          var container = row.closest
            ? row.closest('[data-testid^="' + cssEscape(prefix) + '-item-"]')
            : null;
          if (container) label = trim(container.textContent || "");
          if (!label) label = trim(row.textContent || "");
        }
        if (!label) label = id;
        seen[id] = 1;
        out.push({ value: id, label: label });
      }
    } catch (e) {
      if (DEBUG) console.warn("[cp] _collectMultiselectOptions failed", e);
    }
    return out;
  }

  document.addEventListener(
    "click",
    function (e) {
      var target = closestInteractable(e.target);
      if (!target) return;
      // 2026-06-02 B1: lift the click target to the nearest semantic
      // widget root for Material / hand-authored custom widgets. The
      // resolver is a no-op on plain HTML; on a mat-select inner-div
      // click it returns the <mat-select role=listbox> so the
      // fingerprint captures the WIDGET, not the inner trigger div.
      target = _resolveSemanticTarget(target);
      // Flush any pending text input debounce BEFORE the click is
      // recorded, so order is fill→click, not click→fill.
      if (pendingInputEl && pendingInputEl !== target) flushPendingInput();
      // WI-39: same for rich-text burst -- a click on Save while the
      // operator's typing burst is still debouncing must commit the
      // burst first, so the captured order is rich_text_set -> click.
      if (pendingRichTextEl && pendingRichTextEl !== target
          && !pendingRichTextEl.contains(target)) {
        _flushPendingRichText();
      }
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

      // WI-35: detect target=_blank link clicks. These open a new tab
      // without firing window.open (the browser does it natively). We
      // emit a popup event inline so the annotator can fold it into
      // the click's PopupEffect.
      try {
        var anchorTarget = (function _findAnchor(el) {
          var cur = el;
          while (cur && cur !== document) {
            if (cur.tagName && cur.tagName.toLowerCase() === "a") return cur;
            cur = cur.parentElement;
          }
          return null;
        })(target);
        if (
          anchorTarget &&
          anchorTarget.getAttribute &&
          anchorTarget.getAttribute("target") === "_blank" &&
          anchorTarget.getAttribute("href")
        ) {
          var blankAttr = _attribution("anchor_blank");
          post(_merge({
            kind: "popup",
            page_url: location.href,
            raw_event_kind: "anchor_blank",
            popup_url: anchorTarget.getAttribute("href"),
            popup_target: "_blank",
            popup_features: null,
            popup_binding_key: "popup_anchor_" + attr.event_id.slice(0, 8),
            initiator_event_id: attr.event_id,
          }, blankAttr));
        }
      } catch (popErr) {
        if (DEBUG) console.warn("[cp] anchor _blank emit failed", popErr);
      }

      // WI-45: detect download intent on the click target. Two
      // patterns are emitted as a kind=``download`` TraceEvent that
      // the annotator folds onto the click step (becomes a
      // ``download`` action with DownloadSpec):
      //   1. <a download> / <a download="filename.csv"> -- HTML5
      //      download attribute. The browser will start a download
      //      when the link is clicked. ``href`` carries the source.
      //   2. <button data-download-filename="..."> -- portal-specific
      //      convention for buttons that trigger a download via JS.
      // The Content-Disposition path (network_response-driven) is
      // detected by the annotator from the kind=network_response
      // header summary; the grabber doesn't need a separate hook for
      // it because the response event already carries headers in its
      // upstream pipeline.
      try {
        var downloadEl = (function _findDownloadIntent(el) {
          var cur = el;
          while (cur && cur !== document) {
            if (
              cur.hasAttribute &&
              (cur.hasAttribute("download") ||
                cur.hasAttribute("data-download-filename"))
            ) {
              return cur;
            }
            cur = cur.parentElement;
          }
          return null;
        })(target);
        if (downloadEl) {
          var dlAttr = _attribution("download_intent");
          var declaredName = (
            downloadEl.getAttribute("download") ||
            downloadEl.getAttribute("data-download-filename") ||
            ""
          );
          post(_merge({
            kind: "download",
            page_url: location.href,
            raw_event_kind: "download_intent",
            initiator_event_id: attr.event_id,
            download_filename: declaredName || null,
            download_href: (downloadEl.getAttribute &&
              downloadEl.getAttribute("href")) || null,
          }, dlAttr));
        }
      } catch (dlErr) {
        if (DEBUG) console.warn("[cp] download intent emit failed", dlErr);
      }

      // id+label sprint: when the click target is inside a custom
      // multi-select, snapshot the whole rendered option universe so the
      // annotator can collect known_options (id+label). Captures the
      // currently-surfaced rows -- on a toggle-open click this is the
      // initial list; on an option click it's whatever the last search
      // surfaced. Combined with the search input_change capture below,
      // this covers the universe the operator saw.
      var clickMsPrefix = _multiselectPrefixFor(target);
      var clickOptionsSeen = clickMsPrefix
        ? _collectMultiselectOptions(clickMsPrefix)
        : null;
      // 2026-06-02 B4: when the click resolves to a mat-select widget
      // root, ALSO probe for an associated overlay panel's mat-options
      // and emit them as options_seen so the annotator can build
      // known_options for the planner. The panel may be the just-opened
      // one (aria-owns set by the Material runtime), or -- on portals
      // where aria-owns isn't wired -- ANY currently-open
      // .cdk-overlay-pane carrying mat-option rows.
      if ((!clickOptionsSeen || !clickOptionsSeen.length) &&
          (target.tagName || "").toLowerCase() === "mat-select") {
        var matOpts = _collectMatSelectOptions(target);
        if (matOpts && matOpts.length) clickOptionsSeen = matOpts;
      }
      var payload = _merge({
        kind: "click",
        fingerprint: fingerprint(target),
        page_url: location.href,
        raw_event_kind: "click",
        click_detail: clickDetail,
        pointer_type: pointerType,
        target_state_before: targetStateBefore,
        options_seen: (clickOptionsSeen && clickOptionsSeen.length)
          ? clickOptionsSeen : null,
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
    // id+label sprint: if this input is a multi-select search box, the
    // debounce timer has elapsed AFTER the server-side search settled, so
    // the filtered option rows are rendered now. Snapshot them as
    // options_seen so the annotator captures the universe the operator
    // surfaced via search (a row that only appears for q="Argentina"
    // wouldn't be in the initial list). Fires only for the MultiSelect
    // testid family; non-multiselect inputs get null.
    var inputMsPrefix = _multiselectPrefixFor(el);
    var inputOptionsSeen = inputMsPrefix
      ? _collectMultiselectOptions(inputMsPrefix)
      : null;
    var payload = _merge({
      kind: "input_change",
      fingerprint: fingerprint(el),
      value: el.value != null ? String(el.value) : "",
      value_before: beforeValue,
      page_url: location.href,
      raw_event_kind: "input",
      options_seen: (inputOptionsSeen && inputOptionsSeen.length)
        ? inputOptionsSeen : null,
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

  // 2026-06-02 B5: a "search-like" input is one whose downstream effect
  // is a server-side fetch keyed by ?q=. Two shapes catch the universe:
  //   (a) <input placeholder="Search"> -- the canonical Material
  //       autocomplete / filter input;
  //   (b) any input inside an <ng-multiselect-dropdown> (the search
  //       row's input has placeholder="Search" too, but the cheap
  //       ancestor test handles non-canonical variants).
  function _isSearchLikeInput(el) {
    if (!el || el.nodeType !== 1) return false;
    try {
      var ph = el.getAttribute && el.getAttribute("placeholder");
      if (ph && /search/i.test(ph)) return true;
      var al = el.getAttribute && el.getAttribute("aria-label");
      if (al && /search/i.test(al)) return true;
      if (el.closest && el.closest("ng-multiselect-dropdown")) return true;
    } catch (e) {}
    return false;
  }

  // 2026-06-02 B5: trailing-edge coalescing. Track the most recent
  // value typed into a given element so a burst (e.g. "cana"->"canada")
  // emits ONLY the last value. fireInput already reads el.value at
  // emission time -- the WeakMap is here to give the operator a way to
  // assert "the recorded value was the final one I saw" in tests + to
  // serve as a quick "skip emit if value didn't actually change" guard
  // on the trailing edge (covers programmatic input dispatches that
  // re-fire 'input' with the same value).
  var _lastInputValueFor = (typeof WeakMap === "function")
    ? new WeakMap() : new Map();

  function schedulePending(el) {
    if (pendingInputEl && pendingInputEl !== el) {
      // different element — flush the old one before tracking the new
      if (pendingInputTimer) clearTimeout(pendingInputTimer);
      fireInput(pendingInputEl);
    }
    pendingInputEl = el;
    if (pendingInputTimer) clearTimeout(pendingInputTimer);
    // 2026-06-02 B5: record the CURRENT value so when the trailing-edge
    // timer fires we can dedupe (cheap guard against double-fires).
    try { _lastInputValueFor.set(el, el.value == null ? "" : String(el.value)); } catch (e) {}
    // 2026-06-02 B5: pick the right debounce window. Search-like inputs
    // need the longer window because the server-search round-trip is
    // measured in hundreds of ms, and an early-fired input_change
    // captures a mid-typing value the operator never committed.
    var debounceMs = _isSearchLikeInput(el)
      ? SEARCH_INPUT_DEBOUNCE_MS : INPUT_DEBOUNCE_MS;
    pendingInputTimer = setTimeout(function () {
      if (pendingInputEl) {
        fireInput(pendingInputEl);
        pendingInputEl = null;
        pendingInputTimer = null;
      }
    }, debounceMs);
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
      // WI-39: contenteditable / rich-text editor. The input target
      // may be a child node inside a contenteditable root (e.g. a
      // <p> inside <div contenteditable>); walk up to the editor root
      // before debouncing so the burst attributes to ONE root, not to
      // whatever element happened to receive the input event.
      if (tag !== "input") {
        var editorRoot = _closestContenteditableRoot(t);
        if (editorRoot) {
          _captureRichTextBeforeValue(editorRoot);
          _scheduleRichTextBurst(editorRoot);
        }
        return;
      }
      var itype = (t.getAttribute && t.getAttribute("type") || "text").toLowerCase();
      if (!TEXTISH_TYPES[itype]) return;
      _captureBeforeValue(t);
      schedulePending(t);
    },
    true
  );

  // ---- WI-39: contenteditable / rich-text editor burst capture --------
  //
  // Plain inputs go through fireInput / flushPendingInput above. Editors
  // that mount on a contenteditable root (raw, TinyMCE, Quill, Lexical,
  // ProseMirror) need a parallel debounce path because:
  //   (a) the input target inside the editor is typically a descendant
  //       <p> / <span>, not the root -- the fingerprint must point at
  //       the root so the annotator can collapse the burst
  //   (b) the value to record is the editor's innerHTML / textContent,
  //       not el.value (contenteditable elements have no value property)
  //   (c) framework detection (window.tinymce / window.Quill / Lexical
  //       editor classes) happens once per burst, stamped on the emit
  //
  // The annotator detects this raw_event_kind="rich_text_input" pattern
  // and collapses the burst into one rich_text_set step (WI-39).
  var pendingRichTextEl = null;
  var pendingRichTextTimer = null;
  var richTextBeforeFor = (typeof WeakMap === "function") ? new WeakMap() : new Map();
  var RICH_TEXT_DEBOUNCE_MS = 400;

  function _closestContenteditableRoot(el) {
    // Walks up from the input target to the nearest ancestor whose own
    // contenteditable attribute is "true" or "" (presence form). Returns
    // null when the element isn't inside an editable region (the page's
    // input target was some non-contenteditable element that happened
    // to fire 'input' -- e.g. a number-spinner widget, or a designMode
    // document we don't model).
    var node = el;
    while (node && node.nodeType === 1) {
      var ce = node.getAttribute && node.getAttribute("contenteditable");
      if (ce === "true" || ce === "") return node;
      if (ce === "false") return null;  // explicit non-editable scope
      node = node.parentElement;
    }
    return null;
  }

  function _captureRichTextBeforeValue(root) {
    if (!root) return;
    if (richTextBeforeFor.has(root)) return;
    try {
      // Snapshot innerHTML; textContent is derivable but we keep both
      // on the emit so the annotator can pick the right format. Cap at
      // 64KB to bound payload size for runaway editors.
      var html = root.innerHTML || "";
      if (html.length > 65536) html = html.slice(0, 65536) + "...";
      richTextBeforeFor.set(root, html);
    } catch (e) {}
  }

  function _consumeRichTextBeforeValue(root) {
    if (!root || !richTextBeforeFor.has(root)) return null;
    var v = richTextBeforeFor.get(root);
    try { richTextBeforeFor.delete(root); } catch (e) {}
    return v;
  }

  function _detectEditorFramework(root) {
    // Cheap framework probes -- read window-scoped instances + class
    // names. We don't try every editor under the sun; the runner falls
    // back to ``input_event`` when framework_hint is null/unknown.
    if (!root) return "unknown";
    try {
      var cls = (root.className && typeof root.className === "string")
        ? root.className.toLowerCase() : "";
      if (cls.indexOf("ql-editor") !== -1) return "quill";
      if (cls.indexOf("tox-edit-area") !== -1) return "tinymce";
      if (cls.indexOf("public-DraftEditor") !== -1) return "draft";
      if (cls.indexOf("ProseMirror") !== -1) return "prosemirror";
      // Lexical editors carry a data-lexical-editor attribute.
      if (root.hasAttribute && root.hasAttribute("data-lexical-editor")) {
        return "lexical";
      }
      // Slate editors carry data-slate-editor.
      if (root.hasAttribute && root.hasAttribute("data-slate-editor")) {
        return "slate";
      }
    } catch (e) {}
    try {
      if (window.tinymce && typeof window.tinymce === "object") {
        // Confirm THIS root is a tinymce instance, not just that the
        // global exists.
        try {
          if (window.tinymce.activeEditor &&
              window.tinymce.activeEditor.getBody &&
              window.tinymce.activeEditor.getBody() === root) {
            return "tinymce";
          }
        } catch (te) {}
      }
      if (typeof window.Quill === "function") {
        // Quill exposes Quill.find(domNode) -> instance | null. Use it
        // when available so the hint is precise.
        try {
          if (window.Quill.find && window.Quill.find(root)) return "quill";
        } catch (qe) {}
      }
    } catch (e) {}
    return "unknown";
  }

  function _fireRichTextBurst(root) {
    if (!root) return;
    var attr = _rootAttribution("user_input");
    // Opening a fresh interaction so consequence events (network calls
    // from a remote-save autosave) attribute back to this burst.
    _setActiveInteraction("change", attr.event_id);
    var html = "";
    var text = "";
    try {
      html = root.innerHTML || "";
      if (html.length > 65536) html = html.slice(0, 65536) + "...";
      text = (root.innerText || root.textContent || "");
      if (text.length > 16384) text = text.slice(0, 16384) + "...";
    } catch (e) {}
    var beforeValue = _consumeRichTextBeforeValue(root);
    var framework = _detectEditorFramework(root);
    var fp = fingerprint(root);
    // Force the fingerprint's control_kind to ``contenteditable`` so
    // the annotator routes this through the rich_text_set detector
    // regardless of what control_kind the static fingerprint inferred.
    if (fp) fp.control_kind = "contenteditable";
    post(_merge({
      kind: "input_change",
      fingerprint: fp,
      // ``value`` carries the textContent (plain string) -- this is what
      // the annotator binds to a SkillParam by default. innerHTML lives
      // on rich_text_html for the html-format path.
      value: text,
      value_before: beforeValue,
      // WI-39: signal to the annotator that this is a rich-text burst,
      // not a plain input. The annotator uses raw_event_kind to pick
      // the rich_text_set cluster detector before the generic
      // fill_submit / change detector.
      rich_text_html: html,
      rich_text_framework: framework,
      page_url: location.href,
      raw_event_kind: "rich_text_input",
    }, attr));
  }

  function _flushPendingRichText() {
    if (pendingRichTextEl) {
      if (pendingRichTextTimer) clearTimeout(pendingRichTextTimer);
      _fireRichTextBurst(pendingRichTextEl);
      pendingRichTextEl = null;
      pendingRichTextTimer = null;
    }
  }

  function _scheduleRichTextBurst(root) {
    if (pendingRichTextEl && pendingRichTextEl !== root) {
      if (pendingRichTextTimer) clearTimeout(pendingRichTextTimer);
      _fireRichTextBurst(pendingRichTextEl);
    }
    pendingRichTextEl = root;
    if (pendingRichTextTimer) clearTimeout(pendingRichTextTimer);
    pendingRichTextTimer = setTimeout(function () {
      if (pendingRichTextEl) {
        _fireRichTextBurst(pendingRichTextEl);
        pendingRichTextEl = null;
        pendingRichTextTimer = null;
      }
    }, RICH_TEXT_DEBOUNCE_MS);
  }

  // WI-39: capture before-value on focusin for contenteditable
  // ancestors so the first input doesn't overwrite the starting state.
  document.addEventListener(
    "focusin",
    function (e) {
      var root = _closestContenteditableRoot(e.target);
      if (root) _captureRichTextBeforeValue(root);
    },
    true
  );

  // Flush pending rich-text burst on blur (operator clicked away) and
  // also flush on click outside the editor (covers operator clicking a
  // Save button while the burst is still debouncing).
  document.addEventListener(
    "blur",
    function (e) {
      var root = _closestContenteditableRoot(e.target);
      if (root && root === pendingRichTextEl) _flushPendingRichText();
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
        // WI-29: capture file metadata, NOT path bytes. The browser
        // never reveals the absolute path; the original_name + size +
        // type + extension are enough for replay to validate that the
        // operator-supplied REPLACEMENT path matches the recorded
        // constraints (accept attr, multiple flag, MIME family).
        var files = t.files || [];
        var fname = files[0] ? files[0].name : "";
        var fmeta = [];
        for (var fi = 0; fi < files.length; fi++) {
          var fl = files[fi];
          var ext = "";
          var dotIdx = fl.name ? fl.name.lastIndexOf(".") : -1;
          if (dotIdx >= 0) ext = fl.name.substring(dotIdx).toLowerCase();
          fmeta.push({
            name: fl.name || "",
            size: typeof fl.size === "number" ? fl.size : null,
            mime: fl.type || null,
            ext: ext,
          });
        }
        var attr2 = _rootAttribution("user_file_selected");
        _setActiveInteraction("file_selected", attr2.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "file_selected",
          fingerprint: fingerprint(t),
          file_name: fname,
          file_metadata: fmeta,
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
      } else if (tag === "input" && t.type === "range") {
        // WI-28: range slider COMMIT. The drag burst's input events
        // (emitted by the input handler below) already attributed
        // back to the same interaction; this final ``change`` carries
        // the committed value and the annotator collapses the whole
        // burst into one slider_set step.
        var attrR = _rootAttribution("user_change");
        _setActiveInteraction("change", attrR.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "input_change",
          fingerprint: fingerprint(t),
          value: t.value != null ? String(t.value) : "",
          page_url: location.href,
          raw_event_kind: "change",
        }, attrR), changeStateBefore);
      }
    },
    true
  );

  // WI-28: range slider drag emits a burst of ``input`` events as the
  // thumb moves. The text-input listener above skips range (it's not
  // in TEXTISH_TYPES), so we hook a separate ``input`` listener
  // dedicated to range so the burst is captured. The annotator
  // collapses the burst (same fingerprint + adjacent same-interaction
  // input events) into one slider_set step using the LAST value.
  document.addEventListener(
    "input",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      if (t.tagName.toLowerCase() !== "input") return;
      if (t.type !== "range") return;
      // Attribute every range input event to the same interaction so
      // the annotator can fold the burst on the causality graph rather
      // than guessing by adjacency. ``_setActiveInteraction`` is
      // idempotent inside a tick; the FIRST input opens the
      // interaction, subsequent inputs share its id.
      var attr = _rootAttribution("user_change");
      _setActiveInteraction("change", attr.event_id);
      post(_merge({
        kind: "input_change",
        fingerprint: fingerprint(t),
        value: t.value != null ? String(t.value) : "",
        page_url: location.href,
        raw_event_kind: "input",
      }, attr));
    },
    true
  );

  // WI-30: drag-and-drop capture. dragstart anchors the sequence;
  // dragover is sampled (we don't need every move -- one per 100ms
  // per target is enough for the annotator); drop carries the final
  // landing + DataTransfer summary. All three share an interaction
  // via _setActiveInteraction so the annotator can causally cluster
  // them into one drag_drop step.
  var _lastDragoverTs = 0;
  document.addEventListener(
    "dragstart",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var attr = _rootAttribution("user_dragstart");
      _setActiveInteraction("dragstart", attr.event_id);
      post(_merge({
        kind: "dragstart",
        fingerprint: fingerprint(t),
        page_url: location.href,
        raw_event_kind: "dragstart",
      }, attr));
    },
    true
  );
  document.addEventListener(
    "dragover",
    function (e) {
      // Sample at most once per 100ms to keep the trace bounded; a
      // drag of 5 seconds otherwise produces 50+ dragover events.
      var now = (typeof performance !== "undefined" && performance.now)
        ? performance.now() : Date.now();
      if (now - _lastDragoverTs < 100) return;
      _lastDragoverTs = now;
      var t = e.target;
      if (!t || !t.tagName) return;
      var attr = _rootAttribution("user_dragover");
      // dragover doesn't OPEN a new interaction -- the dragstart did.
      // _rootAttribution preserves the existing activeInteraction id
      // when present, which is the behavior we want here.
      post(_merge({
        kind: "dragover",
        fingerprint: fingerprint(t),
        page_url: location.href,
        raw_event_kind: "dragover",
      }, attr));
    },
    true
  );
  document.addEventListener(
    "drop",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      // Capture the DataTransfer summary -- types + first 200 chars
      // of text/plain if present. dropEffect read from the event's
      // dataTransfer.
      var summary = null;
      try {
        var dt = e.dataTransfer;
        if (dt) {
          var types = [];
          if (dt.types && dt.types.length != null) {
            for (var ti = 0; ti < dt.types.length; ti++) {
              types.push(dt.types[ti]);
            }
          }
          var text_plain = null;
          try {
            var p = dt.getData ? dt.getData("text/plain") : null;
            if (p) text_plain = String(p).slice(0, 200);
          } catch (gd) {}
          summary = {
            types: types,
            text_plain: text_plain,
            drop_effect: dt.dropEffect || "move",
          };
        }
      } catch (de) {}
      var attr = _rootAttribution("user_drop");
      // The drop is the COMMIT of the drag interaction; mark it as
      // such so the annotator can fold dragstart -> drop into one
      // cluster.
      _setActiveInteraction("drop", attr.event_id);
      post(_merge({
        kind: "drop",
        fingerprint: fingerprint(t),
        drop_target_fp: fingerprint(t),
        data_transfer_summary: summary,
        page_url: location.href,
        raw_event_kind: "drop",
      }, attr));
    },
    true
  );

  document.addEventListener(
    "submit",
    function (e) {
      // Flush any pending text input first (e.g. the last field of a form)
      flushPendingInput();
      // WI-39: flush any pending rich-text burst before the submit so
      // the captured order matches operator intent (typed -> submit).
      _flushPendingRichText();
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

    // Followup #3: surface the per-<select> option cap through
    // PortalContext.wait_policy.page_snapshot_option_cap. NOT the same
    // as options_snapshot_max (which bounds the fingerprint snapshot for
    // ONE interacted-with select); this bounds the catalog snapshot of
    // every select on the page. Pushed by the runner onto
    // window.__cp_page_snapshot_option_cap. Literal 40 remains as last-
    // resort fallback (keeps catalog payloads small).
    var SNAPSHOT_OPT_CAP = (typeof window.__cp_page_snapshot_option_cap === "number"
      && window.__cp_page_snapshot_option_cap > 0)
      ? window.__cp_page_snapshot_option_cap : 40;
    var sels = document.querySelectorAll("select");
    for (var k = 0; k < sels.length && selects.length < 40; k++) {
      var se = sels[k];
      var opts = [];
      for (var m = 0; m < se.options.length && opts.length < SNAPSHOT_OPT_CAP; m++) {
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

  // Enter / Escape on focused input + WI-41 global shortcuts. WI-02:
  // a key is a USER ACTION, opens a fresh interaction window so
  // consequences (form submit, navigation, fetch) attribute back.
  document.addEventListener(
    "keydown",
    function (e) {
      var t = e.target;
      if (!t || !t.tagName) return;
      var tag = t.tagName.toLowerCase();

      // WI-41: detect a global keyboard shortcut. A shortcut is a
      // modifier+key chord (Ctrl+S, Cmd+K, Ctrl+Enter etc.) OR a
      // bare special key that's meaningful outside text fields
      // (Escape, Tab when used to advance focus is captured by the
      // page itself; we only emit Escape here when no dialog is open
      // -- the WI-34 path below covers dialog-close Escape).
      //
      // Important: we still record Enter / Escape inside text inputs
      // as ``key`` events (legacy path -- needed for fill_submit's
      // Enter trigger detection in WI-15). The shortcut path is for
      // CHORDS or bare keys OUTSIDE text-input focus.
      var hasModifier = !!(e.ctrlKey || e.metaKey || e.altKey);
      var isModifierKey = (
        e.key === "Control" || e.key === "Meta"
        || e.key === "Shift" || e.key === "Alt"
      );
      var insideTextField = (tag === "input" || tag === "textarea")
        && !e.ctrlKey && !e.metaKey;
      var inEditor = !!_closestContenteditableRoot(t)
        && !e.ctrlKey && !e.metaKey;
      // Treat as shortcut when:
      //   (a) modifier(s) + non-modifier key, OR
      //   (b) the key is in a small allow-list of standalone
      //       shortcuts: F-keys (F1..F12), '/' outside text, '?'
      //       outside text. Escape is handled below by the
      //       legacy Enter/Escape path so dialogs still close.
      var isShortcutCandidate = (
        (hasModifier && !isModifierKey)
        || (/^F([1-9]|1[0-2])$/.test(e.key) && !insideTextField && !inEditor)
        || (e.key === "/" && !insideTextField && !inEditor)
        || (e.key === "?" && !insideTextField && !inEditor)
      );
      if (isShortcutCandidate) {
        var mods = [];
        if (e.ctrlKey) mods.push("Control");
        if (e.metaKey) mods.push("Meta");
        if (e.altKey) mods.push("Alt");
        if (e.shiftKey) mods.push("Shift");
        var keyTarget = (document.activeElement && document.activeElement !== document.body)
          ? document.activeElement : t;
        var stateBefore3 = _pageState();
        var attr3 = _rootAttribution("user_shortcut");
        _setActiveInteraction("key", attr3.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "key",
          fingerprint: fingerprint(keyTarget),
          value: e.key,
          page_url: location.href,
          raw_event_kind: "shortcut",
          shortcut_modifiers: mods,
        }, attr3), stateBefore3);
        // Do not return here for Escape/Enter -- they may also be
        // meaningful as legacy key events. But Ctrl+S etc. ARE the
        // shortcut; return to avoid a duplicate key event.
        if (hasModifier) return;
      }

      if (e.key !== "Enter" && e.key !== "Escape") return;
      // WI-34: global Escape -- when Escape is pressed and ANY dialog
      // is currently open (visible), we still emit a key event even
      // if focus isn't on a textbox. The annotator pairs the global
      // Escape with a subsequent dialog-closed modal event to
      // construct a ModalCloseAction(kind="escape").
      var dialogOpen = false;
      try {
        var all = document.querySelectorAll('[role="dialog"], dialog');
        for (var i = 0; i < all.length; i++) {
          var el = all[i];
          var rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
          if (rect && rect.width > 0 && rect.height > 0) {
            dialogOpen = true;
            break;
          }
        }
      } catch (er) {}
      if (
        e.key === "Escape" &&
        dialogOpen &&
        tag !== "input" &&
        tag !== "textarea"
      ) {
        // Global Escape with a dialog open. The fingerprint here is
        // the focused element (or document.body if none) so the
        // annotator has SOMETHING to bind to; the key value carries
        // the semantic intent.
        var keyTarget2 = (document.activeElement && document.activeElement !== document.body)
          ? document.activeElement : t;
        var stateBefore2 = _pageState();
        var attr2 = _rootAttribution("user_keydown");
        _setActiveInteraction("key", attr2.event_id);
        _emitWithStateSnapshot(_merge({
          kind: "key",
          fingerprint: fingerprint(keyTarget2),
          value: e.key,
          page_url: location.href,
          raw_event_kind: "keydown",
        }, attr2), stateBefore2);
        return;
      }
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

  // 2026-06-02 test bridge. Unit tests under tests/agent/
  // test_angular_label_capture.py load grabber.js into a jsdom Window
  // and need access to the IIFE-private helpers (getAccessibleName,
  // _resolveSemanticTarget, _currentDisplayValue, _collectMatSelectOptions,
  // _isSearchLikeInput, fingerprint). Gate behind a flag so a real
  // browser running the grabber doesn't leak these into the page global.
  if (window.__cp_test_bridge) {
    window.__cp_helpers = {
      getAccessibleName: getAccessibleName,
      resolveSemanticTarget: _resolveSemanticTarget,
      currentDisplayValue: _currentDisplayValue,
      collectMatSelectOptions: _collectMatSelectOptions,
      isSearchLikeInput: _isSearchLikeInput,
      fingerprint: fingerprint,
      findFieldContainer: _findFieldContainer,
      findPrecedingSiblingLabel: _findPrecedingSiblingLabel,
      SEARCH_INPUT_DEBOUNCE_MS: SEARCH_INPUT_DEBOUNCE_MS,
      INPUT_DEBOUNCE_MS: INPUT_DEBOUNCE_MS,
    };
  }
})();
