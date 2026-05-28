import { useEffect, useRef, useState } from "react";

/* Custom multi-select combobox.
 *
 * Looks like the typical enterprise "click to open, search, check items,
 * see chips" widget. Has every quirk that breaks naive recorders:
 *   - the open/close click is on a button, not the input
 *   - each item is a checkbox + label inside a scrollable popover
 *   - the chip "x" button removes from current selection
 *   - the search input filters the list without closing the popover
 *
 * Renders all options always (the mock backend returns small lists),
 * so we don't virtualize. Virtualization on top of this is a separate
 * problem we're not solving in this commit. */
export default function MultiSelect({
  label,
  testId,
  options,
  value,
  onChange,
  placeholder,
  disabled,
  // Optional server-backed search. When provided, the popover list is
  // populated by calling fetchOptions(query) (returns a Promise of
  // [{id, name}]) instead of filtering `options` client-side. A brief
  // spinner shows while the request is in flight -- this faithfully
  // reproduces a real portal's SERVER-SIDE search. `options` is still
  // used as the canonical label source for rendering selected chips,
  // so callers pass the full list there. When fetchOptions is omitted,
  // the component behaves exactly as before (client-side filter).
  fetchOptions,
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [serverResults, setServerResults] = useState([]);
  const [searching, setSearching] = useState(false);
  const wrapRef = useRef(null);
  const reqSeq = useRef(0);

  // Close on outside click.
  useEffect(() => {
    if (!open) return undefined;
    function handler(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Server-backed search: refetch the option list whenever the filter
  // (or open-state) changes. A monotonically increasing request seq
  // drops stale responses so a fast "z" -> "zi" -> "zim" sequence
  // never renders an out-of-order list. Only active when fetchOptions
  // is provided.
  useEffect(() => {
    if (!fetchOptions || !open) return undefined;
    const seq = ++reqSeq.current;
    setSearching(true);
    let cancelled = false;
    Promise.resolve(fetchOptions(filter))
      .then((rows) => {
        if (cancelled || seq !== reqSeq.current) return;
        setServerResults(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (cancelled || seq !== reqSeq.current) return;
        setServerResults([]);
      })
      .finally(() => {
        if (cancelled || seq !== reqSeq.current) return;
        setSearching(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchOptions, filter, open]);

  const selected = new Set(value || []);
  const filtered = fetchOptions
    ? serverResults
    : (options || []).filter((o) =>
        o.name.toLowerCase().includes(filter.toLowerCase()),
      );

  function toggle(id) {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    onChange(Array.from(selected));
  }

  function removeChip(id) {
    selected.delete(id);
    onChange(Array.from(selected));
  }

  return (
    <div className="multiselect" ref={wrapRef} data-testid={testId}>
      {label && (
        <label
          className="multiselect-label"
          data-testid={`${testId}-label`}
        >
          {label}
        </label>
      )}
      <div className="multiselect-chips" data-testid={`${testId}-chips`}>
        {[...selected].map((id) => {
          const opt = (options || []).find((o) => o.id === id);
          if (!opt) return null;
          return (
            <span
              key={id}
              className="chip"
              data-testid={`${testId}-chip-${id}`}
            >
              {opt.name}
              <button
                type="button"
                className="chip-x"
                data-testid={`${testId}-chip-${id}-remove`}
                onClick={() => removeChip(id)}
                aria-label={`Remove ${opt.name}`}
              >
                ×
              </button>
            </span>
          );
        })}
        {selected.size === 0 && (
          <span className="multiselect-placeholder">{placeholder}</span>
        )}
        <button
          type="button"
          className="multiselect-toggle"
          data-testid={`${testId}-toggle`}
          onClick={() => setOpen((o) => !o)}
          disabled={disabled}
          aria-expanded={open}
        >
          {open ? "Close" : "Choose..."}
        </button>
      </div>
      {open && (
        <div
          className="multiselect-popover"
          data-testid={`${testId}-popover`}
          role="listbox"
        >
          <input
            type="text"
            placeholder="Search..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            data-testid={`${testId}-search`}
            className="multiselect-search"
          />
          {searching && (
            <div
              className="multiselect-searching"
              data-testid={`${testId}-searching`}
              role="status"
            >
              Searching...
            </div>
          )}
          <ul className="multiselect-list">
            {filtered.map((o) => (
              <li
                key={o.id}
                className="multiselect-item"
                data-testid={`${testId}-item-${o.id}`}
              >
                <label>
                  <input
                    type="checkbox"
                    data-testid={`${testId}-checkbox-${o.id}`}
                    checked={selected.has(o.id)}
                    onChange={() => toggle(o.id)}
                  />
                  <span>{o.name}</span>
                </label>
              </li>
            ))}
            {filtered.length === 0 && (
              <li
                className="multiselect-empty"
                data-testid={`${testId}-empty`}
              >
                No matches
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
