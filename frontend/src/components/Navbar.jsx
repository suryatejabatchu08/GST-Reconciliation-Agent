// frontend/src/components/Navbar.jsx
import { NavLink } from "react-router-dom";
import {
  LayoutDashboard, Upload, FileText, Bell, Settings, LogOut,
} from "lucide-react";
import { useAuth } from "../contexts/AuthContext";
import "./Navbar.css";

const NAV_ITEMS = [
  { to: "/dashboard", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/upload",    icon: Upload,          label: "New Reconciliation" },
  { to: "/jobs",      icon: FileText,        label: "Job History" },
];

export default function Navbar() {
  const { user, signOut } = useAuth();
  const initials = user?.email?.slice(0, 2).toUpperCase() ?? "CA";
  const email = user?.email ?? "";

  return (
    <nav className="navbar">
      <NavLink to="/dashboard" className="navbar-logo">
        <div className="logo-icon">G</div>
        <div className="logo-text">
          <strong>GST Recon</strong>
          <span>Agent v2.0</span>
        </div>
      </NavLink>

      <div className="nav-section">
        <div className="nav-label">Main</div>
        {NAV_ITEMS.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
          >
            <Icon size={16} />
            {label}
          </NavLink>
        ))}
      </div>

      <div className="navbar-footer">
        <div className="user-info">
          <div className="user-avatar">{initials}</div>
          <div className="user-details">
            <div className="user-name" title={email}>{email}</div>
            <div className="user-role">Chartered Accountant</div>
          </div>
        </div>
        <button className="btn btn-ghost btn-sm" style={{ width: "100%" }} onClick={signOut}>
          <LogOut size={14} />
          Sign out
        </button>
      </div>
    </nav>
  );
}
