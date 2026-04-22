export default function UnlabeledToolbar({
  onExport,
  onReset,
  onPublishPreview,
}) {
  return (
    <div className="unlabeled-toolbar">
      <div className="u-btn" onClick={onExport}>
        <span className="u-icon">↗</span>
        <span>Export</span>
      </div>
      <div className="u-btn" onClick={onReset}>
        <span className="u-icon">↺</span>
        <span>Reset</span>
      </div>
      <div className="u-btn" onClick={onPublishPreview}>
        <span className="u-icon">◉</span>
        <span>Publish Preview</span>
      </div>
    </div>
  );
}
