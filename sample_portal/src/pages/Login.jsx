import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../store/AuthStore.jsx";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  async function onSubmit(e) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(username, password);
      navigate("/catalog");
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="page page-login" data-testid="login-page">
      <header className="page-header">
        <h1>Sign in</h1>
        <p className="muted">
          Any non-empty username + password works (sample portal).
        </p>
      </header>

      <form
        className="card login-form"
        data-testid="login-form"
        onSubmit={onSubmit}
      >
        <label htmlFor="login-username">Username</label>
        <input
          id="login-username"
          type="text"
          data-testid="input-username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
        />
        <label htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          data-testid="input-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
        />
        <button
          className="btn-primary"
          type="submit"
          data-testid="btn-login"
          disabled={!username || !password || submitting}
        >
          {submitting ? "Signing in..." : "Sign in"}
        </button>
        {submitting && (
          <p className="muted" data-testid="status-signing-in">
            Authenticating...
          </p>
        )}
        {error && (
          <p className="error" data-testid="status-login-error">
            {error}
          </p>
        )}
      </form>
    </section>
  );
}
