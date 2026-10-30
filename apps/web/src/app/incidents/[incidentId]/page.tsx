import { IncidentDetail } from "@/components/incident-detail";
export default async function IncidentPage({params}: {params: Promise<{incidentId: string}>}) { const {incidentId} = await params; return <IncidentDetail incidentId={incidentId} />; }
