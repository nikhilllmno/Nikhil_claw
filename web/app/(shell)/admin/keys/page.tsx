"use client";

import { useAdminOrg } from "@/components/Admin";
import { OrgKeys } from "@/components/OrgKeys";

export default function KeysPage() {
  const org = useAdminOrg();
  return <OrgKeys org={org} />;
}
