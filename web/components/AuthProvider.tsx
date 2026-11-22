"use client";

// Holds the sign-in state and the API client for the whole app.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { ApiClient, errorText, fetchConfig, type SignupMode } from "@/lib/api";
import { AuthSession, TAB_CHANNEL, type TabChannel } from "@/lib/auth";
import { saveReturnTo } from "@/lib/returnTo";
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
  /** The sign-up mode of the API (`GET /config`). */
  signup: SignupMode;
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
  const [signup, setSignup] = useState<SignupMode>("invite_only");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const base = apiUrl();
    if (base === "") return;
    let cancelled = false;
    let ready = false;
    // The other tabs of this browser give a new tab their sign-in (M8.1), for example a tab that
    // a link of the chat opened.
    const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(TAB_CHANNEL);
    const tabs: TabChannel | null =
      channel === null ? null : { postMessage: (message) => channel.postMessage(message), onmessage: null };
    if (channel !== null && tabs !== null) channel.onmessage = (event) => tabs.onmessage?.({ data: event.data });
    const signedOut = () => {
      // Keep the page that the user wanted, so the login page opens it after the sign-in.
      saveReturnTo(window.localStorage, window.location.pathname + window.location.search);
      setStatus({ kind: "signed-out" });
    };
    fetchConfig(base)
      .then(async (loaded) => {
        if (cancelled) return;
        const session = new AuthSession({
          config:
            loaded.supabase_url && loaded.supabase_anon_key
              ? { url: loaded.supabase_url.replace(/\/+$/, ""), anonKey: loaded.supabase_anon_key }
              : null,
          session: window.sessionStorage,
          local: window.localStorage,
          // No status before the API client exists: the first status comes below.
          onChange: (tokens) => {
            if (!ready) return;
            if (tokens) setStatus({ kind: "signed-in" });
            else signedOut();
          },
          tabs,
        });
        setSignup(loaded.signup === "open" ? "open" : "invite_only");
        if (session.load() === null) await session.adoptFromTabs();
        if (cancelled) return;
        ready = true;
        setAuth(session);
        // The state of the session now, not a value from before the wait.
        if (session.current !== null) setStatus({ kind: "signed-in" });
        else signedOut();
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setStatus({ kind: "unreachable", message: errorText(error, "The API did not answer.") });
      });
    return () => {
      cancelled = true;
      if (channel !== null) {
        channel.onmessage = null;
        channel.close();
      }
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
    () => ({ status, auth, api, signup, signOut, retry }),
    [status, auth, api, signup, signOut, retry],
  );
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
