import { LlmopsEvalDetail } from "@/components/llmops-eval-detail";

export default async function LlmopsEvalRunPage({params}: {params: Promise<{evalRunId: string}>}) { const {evalRunId} = await params; return <LlmopsEvalDetail evalRunId={evalRunId}/>; }
