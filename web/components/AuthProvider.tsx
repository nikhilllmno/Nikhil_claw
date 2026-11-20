"use client";

// Holds the sign-in state and the API client for the whole app.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiClient, errorText, fetchConfig } from "@/lib/api";
import { AuthSession } from "@/lib/auth";
import { apiUrl } from "@/lib/config";

export type Status =
  | { kind: "loading" }
  /** The API address is not set. */
  | { kind: "unconfigured" }
  /** The API did not answer `GET /config`. */
  | { kind: "unreachable"; message: string }
  | { kind: "signed-out" }
  | { kind: "signed-in" };

interface AuthContext {
  status: Status;
  /** The sign-in object. `null` until the config is loaded. */
  auth: AuthSession | null;
  api: ApiClient | null;
  signOut: () => void;
  retry: () => void;
}

const Context = createContext<AuthContext | null>(null);

export function useAuth(): AuthContext {
  const value = useContext(Context);
  if (value === null) throw new Error("useAuth needs an AuthProvider.");
  return value;
}

/** The API client of a signed-in user. Use it only below a signed-in gate. */
export function useApi(): ApiClient {
  const { api } = useAuth();
  if (api === null) throw new Error("The API client is not ready.");
  return api;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>(() =>
    apiUrl() === "" ? { kind: "unconfigured" } : { kind: "loading" },
  );
  const [auth, setAuth] = useState<AuthSession | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const base = apiUrl();
    if (base === "") return;
    let cancelled = false;
    fetchConfig(base)
      .then((loaded) => {
        if (cancelled) return;
        const session = new AuthSession({
          config:
            loaded.supabase_url && loaded.supabase_anon_key
              ? { url: loaded.supabase_url.replace(/\/+$/, ""), anonKey: loaded.supabase_anon_key }
              : null,
          session: window.sessionStorage,
          local: window.localStorage,
          onChange: (tokens) => setStatus({ kind: tokens ? "signed-in" : "signed-out" }),
        });
        setAuth(session);
        setStatus({ kind: session.load() ? "signed-in" : "signed-out" });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setStatus({ kind: "unreachable", message: errorText(error, "The API did not answer.") });
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const api = useMemo(
    () => (auth === null ? null : new ApiClient(apiUrl(), auth)),
    [auth],
  );
  const signOut = useCallback(() => auth?.signOut(), [auth]);
  const retry = useCallback(() => {
    setStatus({ kind: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  const value = useMemo(
    () => ({ status, auth, api, signOut, retry }),
    [status, auth, api, signOut, retry],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
