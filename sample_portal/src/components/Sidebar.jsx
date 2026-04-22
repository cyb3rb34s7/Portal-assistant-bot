import { NavLink } from "react-router-dom";

const items = [
  { to: "/dashboard", label: "Dashboard", testId: "nav-dashboard" },
  { to: "/media-assets", label: "Media Assets", testId: "nav-media-assets" },
  { to: "/curation", label: "Curation", testId: "nav-curation" },
  { to: "/schedule", label: "Schedule", testId: "nav-schedule" },
  { to: "/settings", label: "Settings", testId: "nav-settings" },
];

export default function Sidebar() {
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
    </aside>
  );
}
