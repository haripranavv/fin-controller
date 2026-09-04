import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { BatchProvider, useBatch } from "./context/BatchContext";
import { formatDuration, formatNumber } from "./format";
import Login from "./screens/Login";
import Overview from "./screens/Overview";
import Batches from "./screens/Batches";
import Reconciliation from "./screens/Reconciliation";
import AgentActivity from "./screens/AgentActivity";
import Investigation from "./screens/Investigation";
import Exceptions from "./screens/Exceptions";
import RecordDetail from "./screens/RecordDetail";
import Import from "./screens/Import";

const NAV_COLLAPSED_KEY = "fc_nav_collapsed";

function TopBar({ onMenuClick }: { onMenuClick: () => void }) {
  const { batches, selectedBatchId, setSelectedBatchId, runStatus, triggerRun, runError } = useBatch();
  const [datasetInput, setDatasetInput] = useState("");
  const running = runStatus?.running ?? false;
  const stage = runStatus?.stage ?? (running ? "RUNNING" : "QUEUED");

  return (
    <div className="topbar">
      <button className="menu-btn" onClick={onMenuClick} aria-label="Open menu" title="Menu">☰</button>
      <select
        className="batch-select"
        value={selectedBatchId ?? ""}
        onChange={(e) => setSelectedBatchId(e.target.value)}
        title="Select a persisted batch"
      >
        {batches.length === 0 && <option value="">no batches yet</option>}
        {batches.map((b) => (
          <option key={b.batch_id} value={b.batch_id}>
            {b.dataset_version} — {b.resolved}/{b.total_cases} resolved
          </option>
        ))}
      </select>

      <div className="topbar-controls">
        {runError && <span className="error-state topbar-error" title={runError}>{runError}</span>}
        {stage === "FAILED" && runStatus?.error_message && (
          <span className="error-state topbar-error" title={runStatus.error_message}>
            run failed: {runStatus.error_message}
          </span>
        )}
        <div className="live-indicator" title={running ? "processed/total is a real, live count of cases actually written so far — never a simulated percentage" : undefined}>
          <span className={`live-dot ${running ? "on" : ""}`} />
          <span className="live-indicator-text">
            {running ? (
              <>
                running {runStatus?.dataset_version} —{" "}
                {runStatus?.total
                  ? <>{formatNumber(runStatus.processed)} / {formatNumber(runStatus.total)} cases</>
                  : "starting…"}
                {" "}· {formatDuration(runStatus?.elapsed_seconds)}
              </>
            ) : stage === "FAILED" ? (
              "failed"
            ) : stage === "COMPLETED" ? (
              `completed — ${runStatus?.resolved ?? 0} resolved / ${runStatus?.escalated ?? 0} escalated`
            ) : (
              "idle"
            )}
          </span>
        </div>
        <input
          type="text"
          className="run-input"
          placeholder="dataset_version to run…"
          value={datasetInput}
          onChange={(e) => setDatasetInput(e.target.value)}
        />
        <button
          className="primary"
          disabled={!datasetInput || running}
          onClick={() => { triggerRun(datasetInput); }}
        >
          Run controller
        </button>
      </div>
    </div>
  );
}

const WORKSPACE_ITEMS = [
  { to: "/", label: "Control Center", short: "CR", end: true },
  { to: "/batches", label: "Batches", short: "BT" },
  { to: "/reconciliation", label: "Cases", short: "CS" },
  { to: "/investigation", label: "Investigations", short: "IG" },
  { to: "/exceptions", label: "Exceptions", short: "EX" },
];
const OPERATIONS_ITEMS = [
  { to: "/import", label: "Import Data", short: "ID" },
  { to: "/activity", label: "Agent Activity", short: "AA" },
];

function Sidebar({
  collapsed, onToggleCollapsed, mobileOpen, onCloseMobile,
}: { collapsed: boolean; onToggleCollapsed: () => void; mobileOpen: boolean; onCloseMobile: () => void }) {
  const { email, isDemo, logout } = useAuth();
  // The mobile drawer is always rendered full-width/full-label, regardless
  // of whatever the desktop collapse toggle was last set to (persisted in
  // localStorage independent of viewport) - only the CSS visual state
  // (nav-collapsed class) is desktop-only; the actual text content React
  // renders has to know this too, since CSS can restyle but not retype.
  const showCollapsed = collapsed && !mobileOpen;

  return (
    <>
      <nav className={`nav ${showCollapsed ? "nav-collapsed" : ""} ${mobileOpen ? "nav-mobile-open" : ""}`}>
        <div className="nav-scroll">
          <div className="nav-brand">
            <div className="nav-brand-row">
              {/* Brand slot - swap this span for an <img>/logo later without
                  touching layout. Exact required text: "nirnaya." expanded,
                  "n." collapsed - never "NIRNAYA"/"Nirnaya"/"N". */}
              <span className="nav-brand-mark">{showCollapsed ? "n." : "nirnaya."}</span>
              <button
                className="nav-collapse-toggle" onClick={onToggleCollapsed}
                title={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              >
                {collapsed ? "›" : "‹"}
              </button>
            </div>
            {!showCollapsed && <span className="nav-brand-sub">Finance ops console</span>}
          </div>

          <div className="nav-section">{!showCollapsed && "Workspace"}</div>
          {WORKSPACE_ITEMS.map((item) => (
            <NavLink
              key={item.to} to={item.to} end={item.end} onClick={onCloseMobile}
              className={({ isActive }) => (isActive ? "active" : "")}
              title={showCollapsed ? item.label : undefined}
            >
              {showCollapsed ? <span className="nav-short">{item.short}</span> : item.label}
            </NavLink>
          ))}
          <div className="nav-section">{!showCollapsed && "Operations"}</div>
          {OPERATIONS_ITEMS.map((item) => (
            <NavLink
              key={item.to} to={item.to} onClick={onCloseMobile}
              className={({ isActive }) => (isActive ? "active" : "")}
              title={showCollapsed ? item.label : undefined}
            >
              {showCollapsed ? <span className="nav-short">{item.short}</span> : item.label}
            </NavLink>
          ))}
        </div>

        <div className="nav-user">
          {!showCollapsed && (
            <div className="nav-user-row">
              <span className="nav-user-email mono" title={email ?? ""}>{email}</span>
              {isDemo && <span className="pill" style={{ marginTop: 3 }}>demo</span>}
            </div>
          )}
          <button onClick={logout} className="nav-signout" title="Sign out">
            {showCollapsed ? "⏻" : "Sign out"}
          </button>
        </div>
      </nav>
      <div className={`nav-backdrop ${mobileOpen ? "visible" : ""}`} onClick={onCloseMobile} aria-hidden="true" />
    </>
  );
}

function Console() {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return localStorage.getItem(NAV_COLLAPSED_KEY) === "1"; } catch { return false; }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    try { localStorage.setItem(NAV_COLLAPSED_KEY, collapsed ? "1" : "0"); } catch { /* ignore */ }
  }, [collapsed]);

  // Belt-and-suspenders: a route change always closes the mobile drawer,
  // even if a click somehow bypassed the NavLink onClick below.
  useEffect(() => { setMobileOpen(false); }, [location.pathname]);

  return (
    <BatchProvider>
      <div className={`app-shell ${collapsed ? "nav-collapsed" : ""}`}>
        <TopBar onMenuClick={() => setMobileOpen(true)} />
        <Sidebar
          collapsed={collapsed} onToggleCollapsed={() => setCollapsed((c) => !c)}
          mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)}
        />
        <main className="main">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/batches" element={<Batches />} />
            <Route path="/import" element={<Import />} />
            <Route path="/import/:jobId" element={<Import />} />
            <Route path="/reconciliation" element={<Reconciliation />} />
            <Route path="/activity" element={<AgentActivity />} />
            <Route path="/investigation" element={<Investigation />} />
            <Route path="/investigation/:caseId" element={<Investigation />} />
            <Route path="/exceptions" element={<Exceptions />} />
            <Route path="/cases/:caseId" element={<RecordDetail />} />
          </Routes>
        </main>
      </div>
    </BatchProvider>
  );
}

function Gate() {
  const { status } = useAuth();
  if (status === "checking") return <div className="loading-state" style={{ paddingTop: 60 }}>Loading…</div>;
  if (status === "unauthenticated") return <Login />;
  return <Console />;
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  );
}
