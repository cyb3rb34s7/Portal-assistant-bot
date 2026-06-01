/* Static Angular-Material-style replica of the Frame TV
 * "Country Content Mapping" page.
 *
 * The grabber only sees the DOM, so all that matters is:
 *  - exact class names / tag names / nesting from page-filled.html
 *  - mat-select panel renders into .cdk-overlay-container at click time
 *  - ng-multiselect-dropdown fires server-side search on input
 *  - cdk-virtual-scroll-viewport virtualizes left/right lists
 *
 * No Angular runtime. No framework. Vanilla JS.
 */
(function () {
  "use strict";

  // ---------- Backend base ----------
  const API = window.__API_BASE__ || "";
  let TOKEN = null;

  // ---------- App state ----------
  const state = {
    year: 2024,
    make: "Samsung",
    model: "24_BOMRB_8K",
    country: null,
    showed: false,
    left: [],          // rows from /api/show-data
    leftChecked: {},   // id -> bool
    right: [],         // accumulated
    leftScroll: 0,
    rightScroll: 0,
  };

  // ---------- Login ----------
  document.getElementById("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = document.getElementById("loginUser").value;
    const password = document.getElementById("loginPass").value;
    const res = await fetch(API + "/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const j = await res.json();
    TOKEN = j.token;
    document.getElementById("loginOverlay").style.display = "none";
    await refreshMakes();
  });

  // ---------- Mat-select panel rendering ----------
  function closeAllPanels() {
    document.querySelectorAll(".cdk-overlay-pane").forEach((n) => n.remove());
  }

  function openMatSelectPanel(matSelect, options, onPick) {
    closeAllPanels();
    const pane = document.createElement("div");
    pane.className = "cdk-overlay-pane";
    pane.id = "cdk-overlay-" + Math.random().toString(36).slice(2, 7);
    const panel = document.createElement("div");
    panel.className = "mat-select-panel mat-primary";
    pane.appendChild(panel);
    let optionIdSeq = (window.__optionIdSeq = (window.__optionIdSeq || 0));
    options.forEach((opt) => {
      const node = document.createElement("mat-option");
      node.className = "mat-option";
      node.setAttribute("role", "option");
      const optId = "mat-option-" + optionIdSeq++;
      window.__optionIdSeq = optionIdSeq;
      node.id = optId;
      const span = document.createElement("span");
      span.className = "mat-option-text";
      span.textContent = opt;
      node.appendChild(span);
      node.addEventListener("click", () => {
        onPick(opt);
        closeAllPanels();
      });
      panel.appendChild(node);
    });
    // Position relative to the mat-select
    const rect = matSelect.getBoundingClientRect();
    pane.style.left = rect.left + "px";
    pane.style.top = rect.bottom + window.scrollY + "px";
    pane.style.minWidth = rect.width + "px";
    // Mark mat-select as aria-owns the panel
    matSelect.setAttribute("aria-owns", pane.id);
    matSelect.setAttribute("aria-expanded", "true");
    document.getElementById("cdk-overlay-container").appendChild(pane);
  }

  // Close mat-select panels on outside click
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!t) return;
    if (t.closest && t.closest(".cdk-overlay-pane")) return;
    if (t.closest && t.closest("mat-select")) return;
    closeAllPanels();
    document.querySelectorAll("mat-select[aria-expanded='true']").forEach((s) => {
      s.removeAttribute("aria-expanded");
      s.removeAttribute("aria-owns");
    });
  }, true);

  // ---------- Year picker ----------
  const yearSelect = document.getElementById("mat-select-year");
  const yearDisplay = document.querySelector("[data-display='year']");
  yearSelect.addEventListener("click", () => {
    const opts = [];
    for (let y = 2026; y >= 2018; y--) opts.push(String(y));
    openMatSelectPanel(yearSelect, opts, (v) => {
      state.year = parseInt(v, 10);
      yearDisplay.textContent = v;
      refreshModels();
      updateShowDataEnabled();
    });
  });

  // ---------- Make picker ----------
  const makeSelect = document.getElementById("mat-select-make");
  const makeDisplay = document.querySelector("[data-display='make']");
  async function refreshMakes() {
    const res = await fetch(API + "/api/makes", {
      headers: TOKEN ? { authorization: "Bearer " + TOKEN } : {},
    });
    const makes = await res.json();
    window.__makes = makes;
    if (!makes.includes(state.make)) {
      state.make = makes[0];
      makeDisplay.textContent = state.make;
    }
    await refreshModels();
  }
  makeSelect.addEventListener("click", async () => {
    const makes = window.__makes || [];
    openMatSelectPanel(makeSelect, makes, async (v) => {
      state.make = v;
      makeDisplay.textContent = v;
      await refreshModels();
      updateShowDataEnabled();
    });
  });

  // ---------- Model picker (cascading on make+year) ----------
  const modelSelect = document.getElementById("mat-select-model");
  const modelDisplay = document.querySelector("[data-display='model']");
  async function refreshModels() {
    const res = await fetch(
      API + "/api/models?make=" + encodeURIComponent(state.make)
        + "&year=" + encodeURIComponent(state.year),
      { headers: TOKEN ? { authorization: "Bearer " + TOKEN } : {} }
    );
    const models = await res.json();
    window.__models = models;
    if (!models.includes(state.model)) {
      state.model = models[0] || "";
      modelDisplay.textContent = state.model;
    }
    updateShowDataEnabled();
  }
  modelSelect.addEventListener("click", () => {
    const models = window.__models || [];
    openMatSelectPanel(modelSelect, models, (v) => {
      state.model = v;
      modelDisplay.textContent = v;
      updateShowDataEnabled();
    });
  });

  // ---------- Country/Region multi-select ----------
  const countryTrigger = document.querySelector("[data-role='country-trigger']");
  const countrySearchInput = document.querySelector("[data-role='country-search']");
  const countryOptionsUl = document.querySelector("[data-role='country-options']");
  const countryDropdownList = document.querySelector(
    "ng-multiselect-dropdown .dropdown-list");
  const countryPlaceholderSpan = document.querySelector(
    "[data-role='country-placeholder']");
  const countryDropdownBtn = countryTrigger;

  function renderCountryChip() {
    // remove existing chips
    countryTrigger.querySelectorAll(".selected-item").forEach((n) => n.remove());
    if (state.country) {
      countryPlaceholderSpan.style.display = "none";
      const chip = document.createElement("span");
      chip.className = "selected-item ng-star-inserted";
      chip.textContent = state.country + " ";
      const x = document.createElement("a");
      x.textContent = "x";
      x.style.color = "white";
      x.style.paddingLeft = "4px";
      x.addEventListener("click", (ev) => {
        ev.stopPropagation();
        state.country = null;
        renderCountryChip();
        updateShowDataEnabled();
      });
      chip.appendChild(x);
      countryTrigger.insertBefore(chip, countryTrigger.firstChild);
    } else {
      countryPlaceholderSpan.style.display = "";
    }
  }
  renderCountryChip();

  function openCountryList() {
    countryDropdownList.removeAttribute("hidden");
    countrySearchInput.focus();
    refreshCountries("");
  }
  function closeCountryList() {
    countryDropdownList.setAttribute("hidden", "");
  }
  countryTrigger.addEventListener("click", (e) => {
    if (e.target.tagName === "A") return; // chip x
    if (countryDropdownList.hasAttribute("hidden")) openCountryList();
    else closeCountryList();
  });

  // Outside click closes the dropdown
  document.addEventListener("click", (e) => {
    if (!e.target.closest("ng-multiselect-dropdown")) closeCountryList();
  });

  async function refreshCountries(q) {
    const res = await fetch(
      API + "/api/countries?q=" + encodeURIComponent(q || "")
        + "&make=" + encodeURIComponent(state.make)
        + "&model=" + encodeURIComponent(state.model),
      { headers: TOKEN ? { authorization: "Bearer " + TOKEN } : {} }
    );
    const rows = await res.json();
    countryOptionsUl.innerHTML = "";
    rows.forEach((c) => {
      const li = document.createElement("li");
      li.className = "multiselect-item-checkbox ng-star-inserted";
      li.innerHTML =
        '<input aria-label="multiselect-item" type="checkbox" />'
        + '<div>' + c.name + '</div>';
      li.addEventListener("click", () => {
        state.country = c.name;
        renderCountryChip();
        closeCountryList();
        updateShowDataEnabled();
      });
      countryOptionsUl.appendChild(li);
    });
  }

  // Server-side search on the search input -- debounced (250 ms client-side,
  // backend adds ~300 ms latency on top).
  let countrySearchTimer = null;
  countrySearchInput.addEventListener("input", (e) => {
    if (countrySearchTimer) clearTimeout(countrySearchTimer);
    const q = e.target.value;
    countrySearchTimer = setTimeout(() => refreshCountries(q), 250);
  });

  // ---------- Show Data button ----------
  const showDataBtn = document.querySelector("[data-action='show-data']");
  function updateShowDataEnabled() {
    const ready = state.year && state.make && state.model && state.country;
    if (ready) showDataBtn.removeAttribute("disabled");
    else showDataBtn.setAttribute("disabled", "true");
  }
  showDataBtn.addEventListener("click", async () => {
    showDataBtn.setAttribute("disabled", "true");
    const url = API + "/api/show-data?country="
      + encodeURIComponent(state.country)
      + "&year=" + state.year
      + "&make=" + encodeURIComponent(state.make)
      + "&model=" + encodeURIComponent(state.model);
    const res = await fetch(url, {
      headers: TOKEN ? { authorization: "Bearer " + TOKEN } : {},
    });
    const j = await res.json();
    state.left = j.rows || [];
    state.leftChecked = {};
    state.showed = true;
    renderLeftList();
    showDataBtn.removeAttribute("disabled");
  });

  // ---------- Virtual scroll: left & right lists ----------
  const ROW_HEIGHT = 40;
  const VISIBLE_ROWS = 15;

  function makeMatCheckboxRow(rowIdSeq, item, checked, side) {
    // Mirrors page-filled.html 992-1080 EXACTLY:
    // <div class="ng-star-inserted">
    //   <div class="cdkRow">
    //     <div style="float:left">
    //       <mat-checkbox class="firstCol mat-checkbox mat-primary [mat-checkbox-checked]"
    //         id="mat-checkbox-2">
    //         <label class="mat-checkbox-layout" for="mat-checkbox-2-input">...
    //     <div style="display:inline-block">
    //       <p class="elps m0">{title}</p>
    //       <p class="greyText m0 smallFont">{id}</p>
    var wrapper = document.createElement("div");
    wrapper.className = "ng-star-inserted";
    var row = document.createElement("div");
    row.className = "cdkRow";
    var cbCell = document.createElement("div");
    cbCell.style.cssText = "float:left";
    var mcId = "mat-checkbox-" + side + "-" + rowIdSeq;
    var checkedClass = checked ? " mat-checkbox-checked" : "";
    cbCell.innerHTML =
      '<mat-checkbox class="firstCol mat-checkbox mat-primary' + checkedClass + '"'
      + ' color="primary" id="' + mcId + '">'
        + '<label class="mat-checkbox-layout" for="' + mcId + '-input">'
          + '<div class="mat-checkbox-inner-container">'
            + '<input class="mat-checkbox-input cdk-visually-hidden"'
              + ' type="checkbox"'
              + ' id="' + mcId + '-input"'
              + ' tabindex="0"'
              + ' aria-checked="' + (checked ? "true" : "false") + '"'
              + (checked ? " checked" : "") + ' />'
            + '<div class="mat-checkbox-frame"></div>'
            + '<div class="mat-checkbox-background">'
              + '<svg class="mat-checkbox-checkmark" viewBox="0 0 24 24">'
                + '<path d="M4.1,12.7 9,17.6 20.3,6.3" fill="none" stroke="white"></path>'
              + '</svg>'
            + '</div>'
          + '</div>'
          + '<span class="mat-checkbox-label"><label>' + item.id + '</label></span>'
        + '</label>'
      + '</mat-checkbox>';
    var labelCell = document.createElement("div");
    labelCell.style.cssText = "display:inline-block";
    labelCell.innerHTML =
      '<p class="elps m0">' + item.title + '</p>'
      + '<p class="greyText m0 smallFont">' + item.id + '</p>';
    row.appendChild(cbCell);
    row.appendChild(labelCell);
    wrapper.appendChild(row);
    return { wrapper, matCheckbox: cbCell.querySelector("mat-checkbox") };
  }

  function renderList(side, list, checkedMap) {
    const viewport = document.querySelector(
      "[data-role='" + side + "-viewport']");
    const rowsContainer = document.querySelector(
      "[data-role='" + side + "-rows']");
    const spacer = document.querySelector(
      "[data-role='" + side + "-spacer']");
    const footer = document.querySelector(
      "[data-role='" + side + "-footer']");
    const total = list.length;
    spacer.style.height = (total * ROW_HEIGHT) + "px";

    function paint() {
      const scrollTop = viewport.scrollTop;
      const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT));
      const endIdx = Math.min(total, startIdx + VISIBLE_ROWS);
      rowsContainer.style.transform = "translateY("
        + (startIdx * ROW_HEIGHT) + "px)";
      rowsContainer.innerHTML = "";
      for (let i = startIdx; i < endIdx; i++) {
        const item = list[i];
        const checked = !!checkedMap[item.id];
        const { wrapper, matCheckbox } = makeMatCheckboxRow(
          i, item, checked, side);
        // wire checkbox toggle (only left side participates in checkedMap)
        if (side === "left") {
          matCheckbox.addEventListener("click", (e) => {
            e.preventDefault();
            checkedMap[item.id] = !checkedMap[item.id];
            paint();
            const n = Object.values(checkedMap).filter(Boolean).length;
            footer.textContent = n + " of " + total + " artworks selected";
          });
        }
        rowsContainer.appendChild(wrapper);
      }
    }
    viewport.onscroll = paint;
    paint();
    const n = Object.values(checkedMap).filter(Boolean).length;
    if (side === "left") {
      footer.textContent = n + " of " + total + " artworks selected";
    } else {
      footer.textContent = total + " transferred";
    }
  }
  function renderLeftList() { renderList("left", state.left, state.leftChecked); }
  function renderRightList() { renderList("right", state.right, {}); }

  // ---------- Transfer Right ----------
  const transferBtn = document.querySelector(
    "[data-action='transfer-right']");
  transferBtn.addEventListener("click", async () => {
    const ids = Object.keys(state.leftChecked).filter(
      (k) => state.leftChecked[k]);
    if (!ids.length) return;
    const res = await fetch(API + "/api/transfer", {
      method: "POST",
      headers: Object.assign(
        { "content-type": "application/json" },
        TOKEN ? { authorization: "Bearer " + TOKEN } : {}
      ),
      body: JSON.stringify({ ids }),
    });
    await res.json();
    const moving = state.left.filter((r) => ids.includes(r.id));
    state.right = state.right.concat(moving);
    state.left = state.left.filter((r) => !ids.includes(r.id));
    state.leftChecked = {};
    renderLeftList();
    renderRightList();
  });

  // ---------- Optional left/right text-filter (visual only) ----------
  const leftSearchEl = document.querySelector(
    "[data-role='left-search']");
  leftSearchEl.addEventListener("input", () => {
    // Visual filter only; mirrors the real portal where the search bar
    // is OUTSIDE the cdk-virtual-scroll-viewport (line 874 vs 984).
    const q = leftSearchEl.value.trim().toUpperCase();
    if (!q) { renderList("left", state.left, state.leftChecked); return; }
    const filtered = state.left.filter(
      (r) => r.id.toUpperCase().includes(q)
        || r.title.toUpperCase().includes(q));
    renderList("left", filtered, state.leftChecked);
  });
})();
