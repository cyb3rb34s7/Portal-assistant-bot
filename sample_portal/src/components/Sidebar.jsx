import { NavLink } from "react-router-dom";
import { useAuth } from "../store/AuthStore.jsx";

const items = [
  { to: "/catalog", label: "Catalog", testId: "nav-catalog" },
  { to: "/upload", label: "Upload", testId: "nav-upload" },
  { to: "/curation", label: "Curation", testId: "nav-curation" },
];

export default function Sidebar() {
  const { user, logout } = useAuth();
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
