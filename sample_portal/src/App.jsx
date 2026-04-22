import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import MediaAssets from "./pages/MediaAssets.jsx";
import Curation from "./pages/Curation/index.jsx";
import Schedule from "./pages/Schedule.jsx";
import Settings from "./pages/Settings.jsx";

export default function App() {
  return (
    <div className="app-shell" data-testid="app-shell">
      <Sidebar />
      <main className="app-main" data-testid="app-main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/media-assets" element={<MediaAssets />} />
          <Route path="/curation" element={<Curation />} />
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
