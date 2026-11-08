import { DepositionWorkspace } from "@/components/DepositionWorkspace";

export default async function PlanPage({ params }: { params: Promise<{ id: string; threadId: string; planId: string }> }) {
  const { id, threadId, planId } = await params;
  return <DepositionWorkspace key={`${id}:${threadId}:${planId}`} matterId={id} threadId={threadId} planId={planId} />;
}
