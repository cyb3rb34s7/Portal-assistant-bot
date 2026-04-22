import { useCallback, useEffect, useMemo, useState } from "react";
import LayoutTab from "./LayoutTab.jsx";
import ScheduleTab from "./ScheduleTab.jsx";
import ThumbnailsTab from "./ThumbnailsTab.jsx";
import PreviewFrame from "./PreviewFrame.jsx";
import UnlabeledToolbar from "./UnlabeledToolbar.jsx";

const TABS = [
  { id: "layout", label: "Layout", testId: "curation-tab-layout" },
  { id: "schedule", label: "Schedule", testId: "curation-tab-schedule" },
  { id: "thumbnails", label: "Thumbnails", testId: "curation-tab-thumbnails" },
  { id: "preview", label: "Preview", testId: "curation-tab-preview" },
];

export default function Curation() {
  const [activeTab, setActiveTab] = useState("layout");
  const [banner, setBanner] = useState(null);
  const [dirtyMap, setDirtyMap] = useState({
    layout: false,
    schedule: false,
    thumbnails: false,
  });
  const [pendingTab, setPendingTab] = useState(null);

  const [layoutRows, setLayoutRows] = useState([
    { contentId: "A-1001", row: 1, position: 1 },
  ]);
  const [schedules, setSchedules] = useState([
    { contentId: "A-1001", start: "2026-04-01", end: "2026-04-30" },
  ]);
  const [thumbnails, setThumbnails] = useState([
    { contentId: "A-1001", fileName: "a1001_hero.jpg" },
  ]);

  const showBanner = useCallback((message, kind = "success") => {
    setBanner({ message, kind });
    setTimeout(() => setBanner(null), 3000);
  }, []);

  const anyDirty = useMemo(
    () => Object.values(dirtyMap).some(Boolean),
    [dirtyMap]
  );

  const markDirty = useCallback((tabId, dirty) => {
    setDirtyMap((prev) => ({ ...prev, [tabId]: dirty }));
  }, []);

  function requestTab(tabId) {
    if (tabId === activeTab) return;
    if (dirtyMap[activeTab]) {
      setPendingTab(tabId);
      return;
    }
    setActiveTab(tabId);
  }

  function confirmDiscard() {
    if (!pendingTab) return;
    markDirty(activeTab, false);
    setActiveTab(pendingTab);
    setPendingTab(null);
  }

  function cancelDiscard() {
    setPendingTab(null);
  }

  useEffect(() => {
    function onBeforeUnload(e) {
      if (anyDirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [anyDirty]);

  return (
    <section className="page" data-testid="page-curation">
      <header className="page-header">
        <h1>Curation</h1>
        <UnlabeledToolbar
          onExport={() => showBanner("Exported draft")}
          onReset={() => showBanner("Workspace reset")}
          onPublishPreview={() => showBanner("Preview staged")}
        />
      </header>

      {banner && (
        <div
          className={`banner ${banner.kind}`}
          role="status"
          data-testid="curation-banner"
        >
          {banner.message}
        </div>
      )}

      <div
        className="curation-tabs"
        role="tablist"
        data-testid="curation-tabs"
      >
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={activeTab === t.id}
            className={`curation-tab ${activeTab === t.id ? "active" : ""} ${
              dirtyMap[t.id] ? "dirty" : ""
            }`}
            onClick={() => requestTab(t.id)}
            data-testid={t.testId}
          >
            {t.label}
            {dirtyMap[t.id] && (
              <span className="dirty-dot" aria-label="unsaved changes">
                ●
              </span>
            )}
          </button>
        ))}
      </div>

      <div className="curation-body" data-testid="curation-body">
        {activeTab === "layout" && (
          <LayoutTab
            rows={layoutRows}
            setRows={setLayoutRows}
            markDirty={(d) => markDirty("layout", d)}
            showBanner={showBanner}
          />
        )}
        {activeTab === "schedule" && (
          <ScheduleTab
            schedules={schedules}
            setSchedules={setSchedules}
            markDirty={(d) => markDirty("schedule", d)}
            showBanner={showBanner}
          />
        )}
        {activeTab === "thumbnails" && (
          <ThumbnailsTab
            thumbnails={thumbnails}
            setThumbnails={setThumbnails}
            markDirty={(d) => markDirty("thumbnails", d)}
            showBanner={showBanner}
          />
        )}
        {activeTab === "preview" && <PreviewFrame />}
      </div>

      {pendingTab && (
        <div
          className="modal-overlay"
          role="dialog"
          aria-modal="true"
          data-testid="dirty-guard-modal"
        >
          <div className="modal">
            <h2>Unsaved changes</h2>
            <p>
              You have unsaved changes on the{" "}
              <strong>{activeTab}</strong> tab. Switching tabs will discard
              them.
            </p>
            <div className="modal-actions">
              <button
                className="btn"
                onClick={cancelDiscard}
                data-testid="btn-dirty-cancel"
              >
                Keep editing
              </button>
              <button
                className="btn danger"
                onClick={confirmDiscard}
                data-testid="btn-dirty-discard"
              >
                Discard and switch
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
