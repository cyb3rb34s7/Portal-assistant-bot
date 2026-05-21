import { useState } from "react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../store/AuthStore.jsx";

const items = [
  { to: "/catalog", label: "Catalog", testId: "nav-catalog" },
  { to: "/upload", label: "Upload", testId: "nav-upload" },
  { to: "/curation", label: "Curation", testId: "nav-curation" },
];

export default function Sidebar() {
  const { user, logout } = useAuth();
  // WI-40: File mega-menu. Submenu doesn't render until the trigger
  // is hovered (or focused, for keyboard a11y). The grabber's
  // pointerenter watcher captures the hover-then-reveal and pairs it
  // with the next click for an effects.hover annotation.
  const [fileMenuOpen, setFileMenuOpen] = useState(false);
  return (
    <aside className="sidebar" data-testid="sidebar" aria-label="Primary">
      <div className="sidebar-brand">Sample Portal</div>
      <nav className="sidebar-nav" aria-label="Main menu">
        <ul>
          {items.map((item) => (
            <li key={item.to}>
              <NavLink
                to={item.to}
                data-testid={item.testId}
                className={({ isActive }) =>
                  isActive ? "sidebar-link active" : "sidebar-link"
                }
              >
                {item.label}
              </NavLink>
            </li>
          ))}
          {/* WI-40: hover-to-reveal File menu. */}
          <li
            className="menu-trigger"
            data-testid="nav-file-menu"
            aria-haspopup="menu"
            onPointerEnter={() => setFileMenuOpen(true)}
            onPointerLeave={() => setFileMenuOpen(false)}
            style={{ position: "relative" }}
          >
            <span className="sidebar-link" style={{ cursor: "pointer" }}>
              File ▾
            </span>
            {fileMenuOpen && (
              <ul
                role="menu"
                data-testid="nav-file-submenu"
                style={{
                  position: "absolute",
                  left: "100%",
                  top: 0,
                  background: "white",
                  border: "1px solid var(--border, #ccc)",
                  padding: 6,
                  margin: 0,
                  listStyle: "none",
                  minWidth: 140,
                  zIndex: 10,
                }}
              >
                <li role="none">
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="menu-file-export"
                    className="btn-link"
                    onClick={() => {
                      // Stub: in production this would call
                      // /api/export and download a file.
                      setFileMenuOpen(false);
                    }}
                  >
                    Export...
                  </button>
                </li>
                <li role="none">
                  <button
                    type="button"
                    role="menuitem"
                    data-testid="menu-file-save-as"
                    className="btn-link"
                    onClick={() => setFileMenuOpen(false)}
                  >
                    Save As...
                  </button>
                </li>
              </ul>
            )}
          </li>
        </ul>
      </nav>
      {user && (
        <div
          className="sidebar-user"
          data-testid="nav-user-menu"
          aria-label="Account"
        >
          <div className="user-name">{user.username}</div>
          <button
            type="button"
            className="btn-link"
            data-testid="btn-logout"
            onClick={logout}
          >
            Sign out
          </button>
        </div>
      )}
    </aside>
  );
}
