import { MatterWorkspace } from "@/components/MatterWorkspace";

export default async function MatterPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <MatterWorkspace matterId={id} />;
}
