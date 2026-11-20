import Link from "next/link";
import styles from "./Page.module.css";

/** The head of a settings-like page: a back link, the title, and one line of help. */
export function PageHeader({ title, meta }: { title: string; meta: React.ReactNode }) {
  return (
    <header className={styles.header}>
      <Link href="/" className={styles.back}>
        Back to sessions
      </Link>
      <h1 className={styles.title}>{title}</h1>
      <p className={styles.meta}>{meta}</p>
    </header>
  );
}
