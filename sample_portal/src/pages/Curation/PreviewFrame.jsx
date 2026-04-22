const PREVIEW_HTML = `
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Homepage preview</title>
  <style>
    body { margin: 0; font-family: -apple-system, Segoe UI, sans-serif; color: #1f2937; background: #fff; }
    .frame { padding: 16px; }
    h2 { margin: 0 0 12px; font-size: 15px; }
    .strip { display: flex; gap: 8px; margin-bottom: 14px; }
    .card {
      width: 120px; height: 70px;
      background: linear-gradient(135deg, #e0e7ff, #c7d2fe);
      border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-size: 13px; color: #3730a3;
    }
    .refresh { padding: 4px 10px; border: 1px solid #cbd5e1; background: #fff; border-radius: 4px; cursor: pointer; font-size: 12px; }
    .status { font-size: 12px; color: #6b7280; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="frame">
    <h2>Homepage preview</h2>
    <div class="strip">
      <div class="card" data-frame-testid="preview-card-1">Hero</div>
      <div class="card" data-frame-testid="preview-card-2">Trending</div>
      <div class="card" data-frame-testid="preview-card-3">For You</div>
    </div>
    <button class="refresh" id="refresh-preview" data-frame-testid="preview-refresh">Refresh preview</button>
    <div class="status" id="status" data-frame-testid="preview-status">Preview loaded</div>
    <script>
      document.getElementById('refresh-preview').addEventListener('click', function () {
        var el = document.getElementById('status');
        el.textContent = 'Refreshed at ' + new Date().toLocaleTimeString();
      });
    </script>
  </div>
</body>
</html>
`;

export default function PreviewFrame() {
  return (
    <div className="preview-frame-wrap" data-testid="preview-wrap">
      <iframe
        title="Homepage preview"
        srcDoc={PREVIEW_HTML}
        className="preview-frame"
        data-testid="preview-iframe"
      />
    </div>
  );
}
