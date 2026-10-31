import { AuthErrorView } from "@/components/auth-pages";

export default async function AuthErrorPage({searchParams}: {searchParams: Promise<{reason?: string}>}) {
  const {reason} = await searchParams;
  return <AuthErrorView reason={reason}/>;
}
