"use client";

import { useAdminOrg } from "@/components/Admin";
import { OrgData } from "@/components/DataControls";

export default function DataPage() {
  const org = useAdminOrg();
  return <OrgData org={org} />;
}
