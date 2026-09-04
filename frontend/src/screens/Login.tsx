import { useState } from "react";
import { useAuth } from "../context/AuthContext";

const DEMO_EMAIL = "operator@financecontroller.demo";
const DEMO_PASSWORD = "finance-demo-2026";

type Mode = "login" | "register";

export default function Login() {
  const { login, register, loginAsDemo } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [demoSubmitting, setDemoSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDemo() {
    setDemoSubmitting(true);
    setError(null);
    try {
      await loginAsDemo();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDemoSubmitting(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "login") await login(email, password);
      else await register(email, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="login-shell">
      <div className="login-panel">
        <div className="login-brand">FINANCE CONTROLLER</div>
        <div className="login-sub">Financial case-closure console</div>

        <button type="button" className="primary login-submit" disabled={demoSubmitting} onClick={handleDemo}>
          {demoSubmitting ? "Signing in…" : "Use demo account →"}
        </button>
        <div className="login-demo-callout">
          <div className="login-demo-callout-title">Demo account</div>
          <div className="login-demo-callout-row"><span>Email</span><span className="mono">{DEMO_EMAIL}</span></div>
          <div className="login-demo-callout-row"><span>Access</span><span className="mono">one-click sign-in above, no password needed</span></div>
          <div className="login-demo-callout-row"><span>Password</span><span className="mono">{DEMO_PASSWORD} <span style={{ color: "var(--text-faint)" }}>(if signing in manually below)</span></span></div>
          <div className="login-demo-callout-sub">Synthetic data only — no real financial records.</div>
        </div>

        <div className="login-divider"><span>or {mode === "login" ? "sign in" : "create an account"} manually</span></div>

        <form onSubmit={handleSubmit} className="login-form">
          <label className="login-label" htmlFor="login-email">Email</label>
          <input
            id="login-email" type="email" required autoFocus={false}
            value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder={DEMO_EMAIL}
          />

          <label className="login-label" htmlFor="login-password">Password</label>
          <input
            id="login-password" type="password" required minLength={mode === "register" ? 8 : undefined}
            value={password} onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
          />

          {error && <div className="error-state" style={{ marginTop: 4 }}>{error}</div>}

          <button type="submit" disabled={submitting} style={{ marginTop: 12 }}>
            {submitting ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        <button
          type="button" className="link-btn" style={{ marginTop: 12, fontSize: 11.5 }}
          onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(null); }}
        >
          {mode === "login" ? "New here? Create an account instead" : "Have an account? Sign in instead"}
        </button>

        <div className="login-footnote">
          Real authentication — passwords are hashed, sessions are server-side tokens. No OAuth,
          MFA, or password recovery in this build. Judges never need to register; use the demo account above.
        </div>
      </div>
    </div>
  );
}
