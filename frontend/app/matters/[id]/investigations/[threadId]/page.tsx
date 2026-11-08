import { InvestigationWorkspace } from "@/components/InvestigationWorkspace";

export default async function InvestigationPage({ params }: { params: Promise<{ id: string; threadId: string }> }) {
  const { id, threadId } = await params;
  return <InvestigationWorkspace key={`${id}:${threadId}`} matterId={id} threadId={threadId} />;
}
