import { createContext, useContext, useEffect, useState } from "react";
import { api, getToken, setToken } from "../lib/api.js";

const AuthCtx = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [bootstrapping, setBootstrapping] = useState(true);

  // On mount, if a token exists try to fetch /auth/me. Lets a refresh
  // restore the session without forcing a re-login.
  useEffect(() => {
    if (!getToken()) {
      setBootstrapping(false);
      return;
    }
    api
      .get("/api/auth/me")
      .then((r) => setUser(r.user || null))
      .catch(() => {
        setToken(null);
        setUser(null);
      })
      .finally(() => setBootstrapping(false));
  }, []);

  async function login(username, password) {
    const r = await api.post("/api/auth/login", { username, password });
    setToken(r.token);
    setUser(r.user);
    return r.user;
  }

  async function logout() {
    try {
      await api.post("/api/auth/logout", {});
    } catch {
      // ignore -- we're clearing local state anyway
    }
    setToken(null);
    setUser(null);
  }

  return (
    <AuthCtx.Provider value={{ user, login, logout, bootstrapping }}>
      {children}
    </AuthCtx.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error("useAuth must be inside <AuthProvider>");
  return ctx;
}
