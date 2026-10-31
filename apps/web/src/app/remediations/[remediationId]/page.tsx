import { RemediationDetailView } from "@/components/remediation-detail";

export default async function RemediationPage({params}: {params: Promise<{remediationId: string}>}) { const {remediationId} = await params; return <RemediationDetailView remediationId={remediationId}/>; }
