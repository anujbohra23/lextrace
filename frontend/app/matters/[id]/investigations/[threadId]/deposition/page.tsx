import { DepositionWorkspace } from "@/components/DepositionWorkspace";

export default async function DepositionPage({ params }: { params: Promise<{ id: string; threadId: string }> }) {
  const { id, threadId } = await params;
  return <DepositionWorkspace key={`${id}:${threadId}`} matterId={id} threadId={threadId} />;
}
