import { LoginView } from "@/components/auth-pages";
import { getAuthConfig } from "@/lib/auth/config";

const LOCAL_USERS = [["owner", "Owner"], ["admin", "Admin"], ["responder", "Responder"], ["analyst", "Analyst"], ["viewer", "Viewer"]] as const;

export default async function LoginPage({searchParams}: {searchParams: Promise<{returnTo?: string}>}) {
  const {returnTo} = await searchParams;
  const local = getAuthConfig().mode === "local";
  return <LoginView returnTo={returnTo} localUsers={local ? LOCAL_USERS.map(([hint, label]) => ({hint, label})) : []}/>;
}
