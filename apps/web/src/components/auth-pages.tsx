import Link from "next/link";
import { safeReturnTo } from "@/lib/auth/security";

const REASONS: Record<string, string> = {
  state: "The sign-in attempt could not be verified (state mismatch). Please start again.",
  nonce: "The identity provider response could not be verified. Please start again.",
  token_exchange: "The identity provider rejected the sign-in. Please try again.",
  validation: "The sign-in response failed validation. Please try again.",
  provider: "The identity provider reported an error. Please try again.",
  denied: "Access was denied by the identity provider.",
};

export function LoginView({returnTo, localUsers}: {returnTo?: string | null; localUsers: Array<{hint: string; label: string}>}) {
  const target = safeReturnTo(returnTo);
  const action = `/api/auth/login?returnTo=${encodeURIComponent(target)}`;
  return <section className="panel auth-card" aria-labelledby="login-title">
    <p className="eyebrow">OpsPilot AI</p>
    <h1 id="login-title">Sign in</h1>
    <p className="muted">Use your organization’s identity provider. OpsPilot never sees or stores your password.</p>
    {localUsers.length > 0 ? <>
      <p className="notice" role="note">Local development identity provider (not available in production).</p>
      <ul className="plain-list">{localUsers.map((user) => <li key={user.hint}><a className="button" href={`${action}&login_hint=${user.hint}`}>Continue as {user.label}</a></li>)}</ul>
    </> : <a className="button primary" href={action}>Sign in with SSO</a>}
  </section>;
}

export function SessionExpiredView({returnTo}: {returnTo?: string | null}) {
  return <section className="panel auth-card" role="alert" aria-labelledby="expired-title">
    <h1 id="expired-title">Session expired</h1>
    <p>Your session ended, so nothing was changed. Sign in again to continue.</p>
    <a className="button primary" href={`/login?returnTo=${encodeURIComponent(safeReturnTo(returnTo))}`}>Sign in again</a>
  </section>;
}

export function AccessDeniedView() {
  return <section className="panel auth-card" role="alert" aria-labelledby="denied-title">
    <h1 id="denied-title">Access denied</h1>
    <p>Your account does not have access to this resource. If you think this is a mistake, ask a workspace owner to review your role.</p>
    <Link className="button" href="/">Back to the application</Link>
  </section>;
}

export function AuthErrorView({reason}: {reason?: string | null}) {
  return <section className="panel auth-card" role="alert" aria-labelledby="error-title">
    <h1 id="error-title">Sign-in problem</h1>
    <p>{(reason && REASONS[reason]) || "Sign-in could not be completed."}</p>
    <a className="button primary" href="/login">Try again</a>
  </section>;
}
