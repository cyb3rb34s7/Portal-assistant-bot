import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import Upload from "./pages/Upload.jsx";
import Curation from "./pages/Curation/index.jsx";
import Login from "./pages/Login.jsx";
import Catalog from "./pages/Catalog.jsx";
import AssetDetail from "./pages/AssetDetail.jsx";
import { PortalProvider } from "./store/PortalStore.jsx";
import { AuthProvider, useAuth } from "./store/AuthStore.jsx";

export default function App() {
  return (
    <AuthProvider>
      <PortalProvider>
        <AppShell />
      </PortalProvider>
    </AuthProvider>
  );
}

// AppShell decides whether to show the login screen or the full
// portal. Bootstrapping = "are we restoring a session from the token?"
// shows a skeleton so we don't briefly flash the login screen on
// page-load with a valid token.
function AppShell() {
  const { user, bootstrapping } = useAuth();

  if (bootstrapping) {
    return (
      <div className="app-shell" data-testid="app-bootstrapping">
        <p className="muted" style={{ padding: 32 }}>
          Loading session...
        </p>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="app-shell unauthenticated" data-testid="app-shell">
        <main className="app-main" data-testid="app-main">
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="*" element={<Navigate to="/login" replace />} />
          </Routes>
        </main>
      </div>
    );
  }

  return (
    <div className="app-shell" data-testid="app-shell">
      <Sidebar />
      <main className="app-main" data-testid="app-main">
        <Routes>
          <Route path="/" element={<Navigate to="/catalog" replace />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/asset/:id" element={<AssetDetail />} />
          <Route path="/upload" element={<Upload />} />
          <Route path="/curation" element={<Curation />} />
          <Route path="/login" element={<Navigate to="/catalog" replace />} />
        </Routes>
      </main>
    </div>
  );
}
