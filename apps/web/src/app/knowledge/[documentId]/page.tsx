import { KnowledgeDetailView } from "@/components/knowledge-detail";

export default async function KnowledgeDocumentPage({params}: {params: Promise<{documentId: string}>}) { const {documentId} = await params; return <KnowledgeDetailView documentId={documentId}/>; }
