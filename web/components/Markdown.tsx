import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./Markdown.module.css";

// The text of the assistant is Markdown. Raw HTML in the text is not rendered (react-markdown
// shows it as text, because no HTML plugin is on). Links open in a new tab without a referrer.
// Images are not loaded: a remote image would tell its host that the page is open.
export function Markdown({ text }: { text: string }) {
  return (
    <div className={styles.markdown}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer nofollow">
              {children}
            </a>
          ),
          img: ({ alt }) => <span className={styles.image}>[image{alt ? `: ${alt}` : ""}]</span>,
          table: ({ children }) => (
            <div className={styles.tableWrap}>
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
