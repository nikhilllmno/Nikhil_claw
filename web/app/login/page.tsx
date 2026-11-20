"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/AuthProvider";
import { ErrorState, LoadingState } from "@/components/ui";
import styles from "./login.module.css";

/** The codes of sign-in links that this page already used (a code works once). */
const usedCodes = new Set<string>();

export default function LoginPage() {
  const { status, auth, retry } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState<"password" | "link" | "code" | null>(null);
  const [notice, setNotice] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const checkedLink = useRef(false);

  useEffect(() => {
    if (status.kind === "signed-in") router.replace("/");
  }, [status.kind, router]);

  // A sign-in link comes back here with `?code=`. Never use tokens from the URL fragment.
  useEffect(() => {
    if (auth === null || checkedLink.current) return;
    checkedLink.current = true;
    if (window.location.hash) {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
    const code = new URLSearchParams(window.location.search).get("code");
    if (!code) return;
    history.replaceState(null, "", window.location.pathname);
    if (usedCodes.has(code)) return;
    usedCodes.add(code);
    // One-time exchange when the page opens from the emailed link; there is no event to hang it on.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBusy("code");
    auth
      .exchangeCode(code)
      .catch((error: unknown) =>
        setNotice({ kind: "error", text: error instanceof Error ? error.message : "Sign-in failed." }),
      )
      .finally(() => setBusy(null));
  }, [auth]);

  if (status.kind === "loading") return <LoadingState label="Connecting" />;
  if (status.kind === "unconfigured") {
    return (
      <ErrorState
        title="The app is not set up"
        message="NEXT_PUBLIC_SAMA_API_URL is not set. Set it to the address of the sama API and build the app again."
      />
    );
  }
  if (status.kind === "unreachable") {
    return <ErrorState title="Cannot reach the sama API" message={status.message} onRetry={retry} />;
  }

  const fail = (error: unknown) =>
    setNotice({ kind: "error", text: error instanceof Error ? error.message : "Sign-in failed." });

  const signIn = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!auth) return;
    setBusy("password");
    setNotice(null);
    try {
      await auth.signInWithPassword(email, password);
      setPassword("");
    } catch (error) {
      fail(error);
    } finally {
      setBusy(null);
    }
  };

  const sendLink = async () => {
    if (!auth) return;
    if (!email.trim()) {
      setNotice({ kind: "error", text: "Enter your email first." });
      return;
    }
    setBusy("link");
    setNotice(null);
    try {
      await auth.sendSignInLink(email.trim(), `${window.location.origin}/login`);
      setNotice({ kind: "success", text: "Look in your email for the sign-in link." });
    } catch (error) {
      fail(error);
    } finally {
      setBusy(null);
    }
  };

  const useToken = (event: React.FormEvent) => {
    event.preventDefault();
    if (!auth || !token.trim()) return;
    auth.useAccessToken(token);
    setToken("");
  };

  const working = busy !== null;
  return (
    <main className={styles.page}>
      <div className={styles.panel}>
        <h1 className={styles.brand}>sama</h1>
        <p className={styles.tagline}>Sign in to your agents.</p>
        <form className={styles.form} onSubmit={signIn}>
          <label className="field">
            <span className="label">Email</span>
            <input
              className="input"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={working}
            />
          </label>
          <label className="field">
            <span className="label">Password</span>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={working}
            />
          </label>
          <button type="submit" className="btn btn-primary" disabled={working}>
            {busy === "password" || busy === "code" ? "Signing in…" : "Sign in"}
          </button>
          <button type="button" className="btn" onClick={sendLink} disabled={working}>
            {busy === "link" ? "Sending…" : "Send a sign-in link"}
          </button>
        </form>
        <div aria-live="polite">
          {notice ? (
            <p className={`alert ${notice.kind === "error" ? "alert-error" : "alert-success"}`}>
              {notice.text}
            </p>
          ) : null}
        </div>
        <details className={styles.dev}>
          <summary>Use an access token (development)</summary>
          <form className={styles.form} onSubmit={useToken}>
            <label className="field">
              <span className="label">Access token</span>
              <input
                className="input"
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
            <button type="submit" className="btn" disabled={!token.trim()}>
              Use token
            </button>
          </form>
        </details>
      </div>
    </main>
  );
}
