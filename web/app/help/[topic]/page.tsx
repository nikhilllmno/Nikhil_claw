import { notFound } from "next/navigation";
import { HelpArticle } from "@/components/Help";
import { HELP_TOPICS, helpTopic } from "@/lib/help";

export function generateStaticParams() {
  return HELP_TOPICS.map((topic) => ({ topic: topic.id }));
}

export default async function HelpTopicPage({ params }: { params: Promise<{ topic: string }> }) {
  const { topic } = await params;
  const found = helpTopic(topic);
  if (found === null) notFound();
  return <HelpArticle topic={found} />;
}
