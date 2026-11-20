import Link from "next/link";
import styles from "./Page.module.css";

/** The head of a settings-like page: a back link, the title, and one line of help. */
export function PageHeader({
  title,
  meta,
  back = { href: "/", label: "Back to sessions" },
}: {
  title: string;
  meta: React.ReactNode;
  back?: { href: string; label: string };
}) {
  return (
    <header className={styles.header}>
      <Link href={back.href} className={styles.back}>
        {back.label}
      </Link>
      <h1 className={styles.title}>{title}</h1>
      <p className={styles.meta}>{meta}</p>
    </header>
  );
}
