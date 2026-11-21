"use client";

import { useAdminOrg } from "@/components/Admin";
import { ApiKeys } from "@/components/ApiKeys";

export default function ApiKeysPage() {
  const org = useAdminOrg();
  return <ApiKeys org={org} />;
}
