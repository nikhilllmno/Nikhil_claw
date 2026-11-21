"use client";

import { PageHeader } from "@/components/PageHeader";
import { SubagentLibrary } from "@/components/SubagentLibrary";
import { useData } from "@/components/DataProvider";
import { ErrorState, LoadingState } from "@/components/ui";
import styles from "@/components/Page.module.css";

export default function SubagentsPage() {
  const { me, meError, reloadMe, workspaces, workspaceId, orgOfWorkspace } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the sub-agents" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;
  const org = orgOfWorkspace(workspaceId);

  return (
    <div className={styles.page}>
      <PageHeader
        title="Sub-agents"
        meta={
          <>
            {workspace ? workspace.label : "No workspace"} · The helpers that your agents can hand
            work to. Make a helper once and use it in many agents.
          </>
        }
      />
      {workspace === null || org === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : (
        // The key gives each workspace its own list, so one workspace never shows the agents
        // of another one while the new list loads.
        <SubagentLibrary key={workspace.id} orgId={org.id} workspaceId={workspace.id} />
      )}
    </div>
  );
}
