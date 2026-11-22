"use client";

import Link from "next/link";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/components/AuthProvider";
import { AuthStatusScreen } from "@/components/ui";
import { errorText } from "@/lib/api";
import { pendingInvite } from "@/lib/orgs";
import { takeReturnTo } from "@/lib/returnTo";
import styles from "./login.module.css";

/** The codes of sign-in links that this page already used (a code works once). */
const usedCodes = new Set<string>();

const noSubscribe = () => () => {};

/** True when the browser keeps the token of an invite link (see `/invite`). */
function useInvited(): boolean {
  return useSyncExternalStore(
    noSubscribe,
    () => pendingInvite(window.localStorage, Date.now()) !== null,
    () => false,
  );
}

export default function LoginPage() {
  const { status, auth, retry } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [mode, setMode] = useState<"sign-in" | "sign-up">("sign-in");
  const [busy, setBusy] = useState<"password" | "link" | "code" | "sign-up" | null>(null);
  const [notice, setNotice] = useState<{ kind: "error" | "success"; text: string } | null>(null);
  const checkedLink = useRef(false);
  const invited = useInvited();

  // After sign-in, an invite link that the user opened before goes first, then the page that the
  // user opened before the sign-in (for example a link from the chat, M8.1).
  useEffect(() => {
    if (status.kind !== "signed-in") return;
    if (pendingInvite(window.localStorage, Date.now())) router.replace("/invite");
    else router.replace(takeReturnTo(window.localStorage) ?? "/");
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
      .catch((error: unknown) => setNotice({ kind: "error", text: errorText(error, "Sign-in failed.") }))
      .finally(() => setBusy(null));
  }, [auth]);

  const fail = (error: unknown) => setNotice({ kind: "error", text: errorText(error, "Sign-in failed.") });

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

  const signUp = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!auth) return;
    setBusy("sign-up");
    setNotice(null);
    try {
      const result = await auth.signUp(email.trim(), password, `${window.location.origin}/login`);
      setPassword("");
      if (result === "confirm") {
        setNotice({
          kind: "success",
          text: "Look in your email for the link that confirms your address. Open it in this browser.",
        });
      }
    } catch (error) {
      setNotice({ kind: "error", text: errorText(error, "Could not create the account.") });
    } finally {
      setBusy(null);
    }
  };

  const switchMode = () => {
    setMode((m) => (m === "sign-in" ? "sign-up" : "sign-in"));
    setNotice(null);
    setPassword("");
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
    <AuthStatusScreen status={status} onRetry={retry} loadingLabel="Connecting">
      <main className={styles.page}>
        <div className={styles.panel}>
          <h1 className={styles.brand}>sama</h1>
          <p className={styles.tagline}>
            {invited
              ? "Sign in or create an account to join the org that invited you."
              : mode === "sign-up"
                ? "Create an account."
                : "Sign in to your agents."}
          </p>
          <form className={styles.form} onSubmit={mode === "sign-up" ? signUp : signIn}>
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
                autoComplete={mode === "sign-up" ? "new-password" : "current-password"}
                minLength={mode === "sign-up" ? 8 : undefined}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={working}
              />
            </label>
            {mode === "sign-up" ? (
              <>
                <span className="hint">Use 8 characters or more.</span>
                <button type="submit" className="btn btn-primary" disabled={working}>
                  {busy === "sign-up" ? "Creating the account…" : "Create account"}
                </button>
              </>
            ) : (
              <>
                <button type="submit" className="btn btn-primary" disabled={working}>
                  {busy === "password" || busy === "code" ? "Signing in…" : "Sign in"}
                </button>
                <button type="button" className="btn" onClick={sendLink} disabled={working}>
                  {busy === "link" ? "Sending…" : "Send a sign-in link"}
                </button>
              </>
            )}
          </form>
          <p className={styles.switch}>
            {mode === "sign-up" ? "You have an account?" : "New to sama?"}{" "}
            <button type="button" className="btn btn-quiet btn-small" onClick={switchMode} disabled={working}>
              {mode === "sign-up" ? "Sign in" : "Create an account"}
            </button>
          </p>
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
          <p className={styles.help}>
            New to sama? <Link href="/help/getting-started">Read how to get started</Link>.
          </p>
        </div>
      </main>
    </AuthStatusScreen>
  );
}
