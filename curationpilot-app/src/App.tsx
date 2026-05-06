import { BrowserRouter, NavLink, Route, Routes, Navigate } from "react-router-dom";
import HomePage from "./routes/HomePage";
import TeachPage from "./routes/TeachPage";
import ReplayPage from "./routes/ReplayPage";
import SkillsPage from "./routes/SkillsPage";
import SessionsPage from "./routes/SessionsPage";

export default function App() {
  return (
    <BrowserRouter>
      <div className="shell-grid">
        <aside className="sidebar">
          <div
            style={{
              fontWeight: 600,
              color: "var(--text-bright)",
              padding: "4px 10px 12px",
              borderBottom: "1px solid var(--border)",
              marginBottom: 8,
            }}
          >
            CurationPilot
          </div>
          <NavLink to="/" end className={navClass}>
            Home
          </NavLink>
          <NavLink to="/teach" className={navClass}>
            Teach
          </NavLink>
          <NavLink to="/replay" className={navClass}>
            Replay
          </NavLink>
          <NavLink to="/skills" className={navClass}>
            Skills
          </NavLink>
          <NavLink to="/sessions" className={navClass}>
            Sessions
          </NavLink>
        </aside>
        <main className="main">
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/teach" element={<TeachPage />} />
            <Route path="/replay" element={<ReplayPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}

function navClass({ isActive }: { isActive: boolean }) {
  return isActive ? "sidebar-link active" : "sidebar-link";
}
