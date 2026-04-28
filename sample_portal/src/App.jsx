import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import Upload from "./pages/Upload.jsx";
import Curation from "./pages/Curation/index.jsx";
import { PortalProvider } from "./store/PortalStore.jsx";

export default function App() {
  return (
    <PortalProvider>
      <div className="app-shell" data-testid="app-shell">
        <Sidebar />
        <main className="app-main" data-testid="app-main">
          <Routes>
            <Route path="/" element={<Navigate to="/upload" replace />} />
            <Route path="/upload" element={<Upload />} />
            <Route path="/curation" element={<Curation />} />
          </Routes>
        </main>
      </div>
    </PortalProvider>
  );
}
