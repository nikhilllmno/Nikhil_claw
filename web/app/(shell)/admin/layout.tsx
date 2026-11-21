"use client";

import { AdminFrame } from "@/components/Admin";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  return <AdminFrame>{children}</AdminFrame>;
}
