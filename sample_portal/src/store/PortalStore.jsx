import { createContext, useContext, useEffect, useMemo, useReducer } from "react";

const STORAGE_KEY = "sample_portal_state_v2";

const empty = {
  // Contents uploaded from the CSV.
  // Each: { content_id, title, image_path, category, release_date }
  contents: [],

  // Layout currently being curated, or null.
  // {
  //   layout_id: "grid-2x2" | "featured-row" | "carousel",
  //   slots: [{ idx, content_id?, image_uploaded? }, ...]   // length = slot count
  //   comment: string,
  //   saved: bool,
  //   applied: bool,
  // }
  draftLayout: null,

  // History of applied layouts (snapshot copies).
  appliedLayouts: [],
};

function loadInitial() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return empty;
    const parsed = JSON.parse(raw);
    return { ...empty, ...parsed };
  } catch {
    return empty;
  }
}

function reducer(state, action) {
  switch (action.type) {
    case "RESET":
      return empty;

    case "UPLOAD_CONTENTS": {
      // Replace the whole list (matches "operator uploads a CSV").
      return { ...state, contents: action.contents };
    }

    case "SELECT_LAYOUT": {
      const slotCount = action.slotCount;
      return {
        ...state,
        draftLayout: {
          layout_id: action.layout_id,
          slots: Array.from({ length: slotCount }, (_, i) => ({
            idx: i + 1,
            content_id: null,
            image_uploaded: false,
          })),
          comment: "",
          saved: false,
          applied: false,
        },
      };
    }

    case "ASSIGN_SLOT_CONTENT": {
      if (!state.draftLayout) return state;
      const slots = state.draftLayout.slots.map((s) =>
        s.idx === action.idx ? { ...s, content_id: action.content_id } : s
      );
      return { ...state, draftLayout: { ...state.draftLayout, slots } };
    }

    case "UPLOAD_SLOT_IMAGE": {
      if (!state.draftLayout) return state;
      const slots = state.draftLayout.slots.map((s) =>
        s.idx === action.idx ? { ...s, image_uploaded: true } : s
      );
      return { ...state, draftLayout: { ...state.draftLayout, slots } };
    }

    case "SET_COMMENT": {
      if (!state.draftLayout) return state;
      return {
        ...state,
        draftLayout: { ...state.draftLayout, comment: action.comment },
      };
    }

    case "SAVE_LAYOUT": {
      if (!state.draftLayout) return state;
      return {
        ...state,
        draftLayout: { ...state.draftLayout, saved: true },
      };
    }

    case "APPLY_LAYOUT": {
      if (!state.draftLayout || !state.draftLayout.saved) return state;
      const snapshot = { ...state.draftLayout, applied: true };
      return {
        ...state,
        draftLayout: snapshot,
        appliedLayouts: [...state.appliedLayouts, snapshot],
      };
    }

    default:
      return state;
  }
}

const Ctx = createContext(null);

export function PortalProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, undefined, loadInitial);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // ignore
    }
  }, [state]);

  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePortal() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("usePortal must be used inside <PortalProvider>");
  return ctx;
}

export const LAYOUTS = {
  "grid-2x2": { label: "Grid 2x2", slotCount: 4 },
  "featured-row": { label: "Featured Row", slotCount: 4 },
  carousel: { label: "Carousel", slotCount: 5 },
};
