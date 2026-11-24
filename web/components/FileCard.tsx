"use client";

// The files of a run, under its answer in the chat. One card per file: the name, the type and
// the size, and a Download button. An image also shows a small preview. The bytes need the
// Bearer token, so the page loads them as a blob (the same way as `SentImage` in the chat).

import { useEffect, useState } from "react";
import { errorText, type MediaItem } from "@/lib/api";
import { saveFile } from "@/lib/history";
import { mediaLabel, mediaName } from "@/lib/media";
import { useApi } from "./AuthProvider";
import styles from "./FileCard.module.css";

export function FileCards({ sessionId, items }: { sessionId: string; items: readonly MediaItem[] }) {
  if (items.length === 0) return null;
  return (
    <ul className={styles.list} aria-label="Files">
      {items.map((item) => (
        <li key={item.id}>
          <FileCard sessionId={sessionId} item={item} />
        </li>
      ))}
    </ul>
  );
}

function FileCard({ sessionId, item }: { sessionId: string; item: MediaItem }) {
  const api = useApi();
  const name = mediaName(item);
  const isImage = item.kind === "image";
  const [blob, setBlob] = useState<Blob | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // An image loads once for the preview. The download then uses the same bytes.
  useEffect(() => {
    if (!isImage) return;
    let live = true;
    let made: string | null = null;
    api
      .getMedia(sessionId, item.id, name)
      .then((file) => {
        if (!live) return;
        made = URL.createObjectURL(file.blob);
        setBlob(file.blob);
        setUrl(made);
      })
      .catch(() => {
        // No preview. The Download button still tries again.
      });
    return () => {
      live = false;
      if (made !== null) URL.revokeObjectURL(made);
    };
  }, [api, sessionId, item.id, isImage, name]);

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      const file = blob ?? (await api.getMedia(sessionId, item.id, name)).blob;
      saveFile(name, file);
    } catch (e) {
      setError(errorText(e, "The file did not download."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-label={name}>
      {url !== null ? (
        // A blob URL of a private image: next/image cannot load it.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt={item.caption || name} className={styles.preview} />
      ) : null}
      <div className={styles.text}>
        <span className={styles.name}>{name}</span>
        <span className={styles.meta}>{mediaLabel(item)}</span>
        {error !== null ? (
          <span className={styles.error} role="alert">
            {error}
          </span>
        ) : null}
      </div>
      <button
        type="button"
        className={`btn btn-small ${styles.action}`}
        onClick={() => void download()}
        disabled={busy}
        aria-label={`Download ${name}`}
      >
        {busy ? "Downloading…" : "Download"}
      </button>
    </section>
  );
}
