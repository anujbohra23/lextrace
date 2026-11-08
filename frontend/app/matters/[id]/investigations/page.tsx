import { InvestigationWorkspace } from "@/components/InvestigationWorkspace";

export default async function InvestigationsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <InvestigationWorkspace key={id} matterId={id} />;
}
