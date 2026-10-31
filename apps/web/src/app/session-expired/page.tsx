import { SessionExpiredView } from "@/components/auth-pages";

export default async function SessionExpiredPage({searchParams}: {searchParams: Promise<{returnTo?: string}>}) {
  const {returnTo} = await searchParams;
  return <SessionExpiredView returnTo={returnTo}/>;
}
