import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { BatchProvider, useBatch } from "./context/BatchContext";
import Login from "./screens/Login";
import Overview from "./screens/Overview";
import Batches from "./screens/Batches";
import Reconciliation from "./screens/Reconciliation";
import AgentActivity from "./screens/AgentActivity";
import Investigation from "./screens/Investigation";
import Exceptions from "./screens/Exceptions";
import RecordDetail from "./screens/RecordDetail";
import Import from "./screens/Import";

function TopBar() {
  const { batches, selectedBatchId, setSelectedBatchId, runStatus, triggerRun, runError } = useBatch();
  const [datasetInput, setDatasetInput] = useState("");
  const running = runStatus?.running ?? false;

  return (
    <div className="topbar">
      <div className="brand">FINANCE CONTROLLER <small>case-closure console</small></div>
      <select
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
        {runError && <span className="error-state" style={{ padding: "3px 8px" }}>{runError}</span>}
        <div className="live-indicator">
          <span className={`live-dot ${running ? "on" : ""}`} />
          {running
            ? `running ${runStatus?.dataset_version} — ${runStatus?.resolved ?? 0} resolved / ${runStatus?.escalated ?? 0} escalated`
            : "idle"}
        </div>
        <input
          type="text"
          placeholder="dataset_version to run (e.g. heldout-v1)"
          value={datasetInput}
          onChange={(e) => setDatasetInput(e.target.value)}
          style={{ minWidth: 190 }}
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

function Sidebar() {
  const { email, isDemo, logout } = useAuth();
  const workspace = [
    { to: "/", label: "Control Center", end: true },
    { to: "/batches", label: "Batches" },
    { to: "/reconciliation", label: "Cases" },
    { to: "/investigation", label: "Investigations" },
    { to: "/exceptions", label: "Exceptions" },
  ];
  const operations = [
    { to: "/import", label: "Import Data" },
    { to: "/activity", label: "Agent Activity" },
  ];
  return (
    <nav className="nav">
      <div className="nav-scroll">
        <div className="nav-section">Workspace</div>
        {workspace.map((item) => (
          <NavLink key={item.to} to={item.to} end={item.end} className={({ isActive }) => (isActive ? "active" : "")}>
            {item.label}
          </NavLink>
        ))}
        <div className="nav-section">Operations</div>
        {operations.map((item) => (
          <NavLink key={item.to} to={item.to} className={({ isActive }) => (isActive ? "active" : "")}>
            {item.label}
          </NavLink>
        ))}
      </div>
      <div className="nav-user">
        <div className="nav-user-row">
          <span className="nav-user-email mono" title={email ?? ""}>{email}</span>
          {isDemo && <span className="pill" style={{ marginTop: 3 }}>demo</span>}
        </div>
        <button onClick={logout} style={{ width: "100%", marginTop: 6 }}>Sign out</button>
      </div>
    </nav>
  );
}

function Console() {
  return (
    <BatchProvider>
      <div className="app-shell">
        <TopBar />
        <Sidebar />
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
