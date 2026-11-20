"use client";

// The skill editor (docs/architecture.md, Section 9.6). The user reads the skills of the
// workspace, writes the skills of the scopes that they can change, and reads old versions.
// A built-in skill is read-only. A save is a new version with the author "user".

import { useCallback, useState } from "react";
import {
  errorText,
  type SkillDetail,
  type SkillScope,
  type SkillSummary,
  type SkillVersion,
  type SkillVersionDetail,
  type SkillWriteBody,
  type WritableSkillScope,
} from "@/lib/api";
import { filterSkills, nameProblem, pathProblem, SCOPE_LABELS, skillTemplate } from "@/lib/skills";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

type Open = { kind: "skill"; name: string; scope: SkillScope } | { kind: "new" } | null;

export function SkillEditor() {
  const api = useApi();
  const { me, meError, reloadMe, workspaces, workspaceId } = useData();
  // The open skill keeps its workspace: a new workspace shows the list again.
  const [opened, setOpened] = useState<{ workspaceId: string | null; open: Open }>({
    workspaceId: null,
    open: null,
  });
  const [filter, setFilter] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(
    () => (workspaceId === null ? Promise.resolve(null) : api.listSkills(workspaceId)),
    [api, workspaceId],
  );
  const catalog = useLoad(load, "Could not load the skills.");
  const open = opened.workspaceId === workspaceId ? opened.open : null;
  const setOpen = (next: Open) => setOpened({ workspaceId, open: next });

  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the skills" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;

  const back = (text: string | null) => {
    setOpen(null);
    setNotice(text);
    catalog.reload();
  };

  return (
    <div className={styles.page}>
      <PageHeader
        title="Skills"
        meta={
          <>
            {workspace ? workspace.label : "No workspace"} · The step-by-step guides that the
            agents load when a task needs them.
          </>
        }
      />
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {workspace === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : catalog.error ? (
        <ErrorState title="The skills did not load" message={catalog.error} onRetry={catalog.reload} />
      ) : catalog.data === null ? (
        <LoadingState label="Loading the skills" />
      ) : open?.kind === "new" ? (
        <NewSkill
          workspaceId={workspace.id}
          scopes={catalog.data.writable_scopes}
          onCancel={() => setOpen(null)}
          onCreated={(name, scope) => {
            setNotice(`The skill ${name} is created.`);
            catalog.reload();
            setOpen({ kind: "skill", name, scope });
          }}
        />
      ) : open?.kind === "skill" ? (
        <SkillView
          key={`${open.scope}:${open.name}`}
          workspaceId={workspace.id}
          name={open.name}
          scope={open.scope}
          onBack={() => back(null)}
          onOpen={(scope) => setOpen({ kind: "skill", name: open.name, scope })}
          onArchived={() => back(`The skill ${open.name} is archived.`)}
          onSaved={(text) => {
            setNotice(text);
            catalog.reload();
          }}
        />
      ) : (
        <>
          <div className={styles.toolbar}>
            <label className={`field ${styles.grow}`}>
              <span className="label">Filter</span>
              <input
                className="input"
                type="search"
                value={filter}
                placeholder="Name, description, or tag"
                onChange={(e) => setFilter(e.target.value)}
              />
            </label>
            {catalog.data.writable_scopes.length > 0 ? (
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => {
                  setNotice(null);
                  setOpen({ kind: "new" });
                }}
              >
                New skill
              </button>
            ) : null}
          </div>
          <SkillList
            skills={filterSkills(catalog.data.skills, filter)}
            total={catalog.data.skills.length}
            onOpen={(s) => {
              setNotice(null);
              setOpen({ kind: "skill", name: s.name, scope: s.scope });
            }}
          />
        </>
      )}
    </div>
  );
}

function SkillList({
  skills,
  total,
  onOpen,
}: {
  skills: SkillSummary[];
  total: number;
  onOpen: (skill: SkillSummary) => void;
}) {
  if (total === 0) return <p className="alert">This workspace has no skills yet. Make one with New skill.</p>;
  if (skills.length === 0) return <p className={styles.meta}>No skill matches the filter.</p>;
  return (
    <ul className={styles.list} aria-label="Skills">
      {skills.map((s) => (
        <li key={`${s.scope}:${s.name}`}>
          <section className={`card ${styles.card}`} aria-label={s.name}>
            <div className={styles.cardHead}>
              <h2 className={styles.heading}>{s.name}</h2>
              <div className={styles.name}>
                <span className={s.scope === "builtin" ? "badge" : "badge badge-accent"}>
                  {SCOPE_LABELS[s.scope]}
                </span>
                <span className={styles.meta}>v{s.version}</span>
              </div>
            </div>
            <p className={styles.text}>{s.description}</p>
            {s.tags.length > 0 ? <p className={styles.meta}>Tags: {s.tags.join(", ")}</p> : null}
            <div className={styles.actions}>
              <button type="button" className="btn btn-small" onClick={() => onOpen(s)}>
                {s.can_edit ? "Open and edit" : "Open"}
                <span className="sr-only"> {s.name}</span>
              </button>
            </div>
          </section>
        </li>
      ))}
    </ul>
  );
}

function useWrite(workspaceId: string) {
  const api = useApi();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const write = async (label: string, body: SkillWriteBody) => {
    setBusy(label);
    setError(null);
    try {
      return await api.writeSkill(workspaceId, body);
    } catch (e) {
      setError(errorText(e, "The skill was not saved."));
      return null;
    } finally {
      setBusy(null);
    }
  };
  return { busy, error, setError, write };
}

function NewSkill({
  workspaceId,
  scopes,
  onCancel,
  onCreated,
}: {
  workspaceId: string;
  scopes: WritableSkillScope[];
  onCancel: () => void;
  onCreated: (name: string, scope: WritableSkillScope) => void;
}) {
  const [name, setName] = useState("");
  const [scope, setScope] = useState<WritableSkillScope>(scopes.includes("user") ? "user" : (scopes[0] ?? "user"));
  const [content, setContent] = useState(() => skillTemplate(""));
  const [touched, setTouched] = useState(false);
  const { busy, error, setError, write } = useWrite(workspaceId);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = nameProblem(name);
    if (problem !== null) {
      setError(problem);
      return;
    }
    const done = await write("create", { action: "create", name, scope, content });
    if (done) onCreated(done.name, done.scope);
  };

  return (
    <section className={`card ${styles.card}`} aria-label="New skill">
      <h2 className={styles.heading}>New skill</h2>
      <form className={styles.form} onSubmit={submit}>
        <label className="field">
          <span className="label">Name</span>
          <input
            className="input"
            value={name}
            maxLength={64}
            autoComplete="off"
            onChange={(e) => {
              const next = e.target.value;
              setName(next);
              // The template follows the name until the user changes the text.
              if (!touched) setContent(skillTemplate(next));
            }}
          />
          <span className="hint">Lowercase letters, digits, and hyphens. The name in SKILL.md must be the same.</span>
        </label>
        <label className="field">
          <span className="label">Who can use it</span>
          <select className="input" value={scope} onChange={(e) => setScope(e.target.value as WritableSkillScope)}>
            {scopes.map((s) => (
              <option key={s} value={s}>
                {SCOPE_LABELS[s]}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="label">SKILL.md</span>
          <textarea
            className={`input ${styles.editor}`}
            value={content}
            spellCheck={false}
            onChange={(e) => {
              setTouched(true);
              setContent(e.target.value);
            }}
          />
        </label>
        {error ? (
          <p className="alert alert-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
            {busy ? "Creating" : "Create"}
          </button>
          <button type="button" className="btn btn-quiet btn-small" onClick={onCancel} disabled={busy !== null}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}

function SkillView({
  workspaceId,
  name,
  scope,
  onBack,
  onOpen,
  onArchived,
  onSaved,
}: {
  workspaceId: string;
  name: string;
  scope: SkillScope;
  onBack: () => void;
  onOpen: (scope: SkillScope) => void;
  onArchived: () => void;
  onSaved: (text: string) => void;
}) {
  const api = useApi();
  const load = useCallback(() => api.getSkill(workspaceId, name, scope), [api, workspaceId, name, scope]);
  const detail = useLoad(load, "Could not load the skill.");
  const [content, setContent] = useState<string | null>(null);
  const [summary, setSummary] = useState("");
  const [versionsTick, setVersionsTick] = useState(0);
  const { busy, error, write } = useWrite(workspaceId);

  if (detail.error) {
    return (
      <>
        <BackButton onBack={onBack} />
        <ErrorState title="The skill did not load" message={detail.error} onRetry={detail.reload} />
      </>
    );
  }
  if (detail.data === null) return <LoadingState label="Loading the skill" />;
  const skill = detail.data;
  const writable = skill.can_edit && skill.scope !== "builtin";
  const text = content ?? skill.skill_md;

  const saved = (version: number, what: string) => {
    onSaved(`${what} Version ${version}.`);
    setContent(null);
    setSummary("");
    setVersionsTick((n) => n + 1);
    detail.reload();
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    const done = await write("edit", {
      action: "edit",
      name: skill.name,
      scope: skill.scope as WritableSkillScope,
      content: text,
      summary: summary.trim() || undefined,
    });
    if (done) saved(done.version, "The skill is saved.");
  };

  const archive = async () => {
    if (!window.confirm(`Archive the skill ${skill.name}? The agents can no longer load it.`)) return;
    const done = await write("archive", {
      action: "archive",
      name: skill.name,
      scope: skill.scope as WritableSkillScope,
    });
    if (done) onArchived();
  };

  return (
    <>
      <BackButton onBack={onBack} />
      <section className={`card ${styles.card}`} aria-label={skill.name}>
        <div className={styles.cardHead}>
          <h2 className={styles.heading}>{skill.name}</h2>
          <div className={styles.name}>
            <span className={skill.scope === "builtin" ? "badge" : "badge badge-accent"}>
              {SCOPE_LABELS[skill.scope]}
            </span>
            <span className={styles.meta}>v{skill.version}</span>
          </div>
        </div>
        <p className={styles.text}>{skill.description}</p>
        {skill.requires_toolsets.length > 0 ? (
          <p className={styles.meta}>Needs the toolsets: {skill.requires_toolsets.join(", ")}</p>
        ) : null}
        {skill.other_scopes.length > 0 ? (
          <div className={styles.row}>
            <p className={styles.meta}>
              A skill with this name is also in another scope. The narrowest scope wins: only you,
              then workspace, then org, then built-in.
            </p>
            <div className={styles.actions}>
              {skill.other_scopes.map((s) => (
                <button key={s} type="button" className="btn btn-quiet btn-small" onClick={() => onOpen(s)}>
                  Open {SCOPE_LABELS[s].toLowerCase()} version
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {writable ? (
          <form className={styles.form} onSubmit={save} aria-label={`Edit ${skill.name}`}>
            <label className="field">
              <span className="label">SKILL.md</span>
              <textarea
                className={`input ${styles.editor}`}
                value={text}
                spellCheck={false}
                onChange={(e) => setContent(e.target.value)}
              />
            </label>
            <label className="field">
              <span className="label">What changed (optional)</span>
              <input className="input" value={summary} maxLength={500} onChange={(e) => setSummary(e.target.value)} />
            </label>
            {error ? (
              <p className="alert alert-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className={styles.actions}>
              <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null || content === null}>
                {busy === "edit" ? "Saving" : "Save"}
              </button>
              {content !== null ? (
                <button type="button" className="btn btn-quiet btn-small" onClick={() => setContent(null)}>
                  Discard changes
                </button>
              ) : null}
              <button
                type="button"
                className="btn btn-danger btn-small"
                disabled={busy !== null}
                onClick={() => void archive()}
              >
                {busy === "archive" ? "Archiving" : "Archive"}
              </button>
            </div>
          </form>
        ) : (
          <>
            <pre className={styles.pre}>{skill.skill_md}</pre>
            <p className={styles.meta}>
              {skill.scope === "builtin"
                ? "A built-in skill is read-only. To change it, make a skill with the same name."
                : "You cannot change this skill. An admin of its scope can."}
            </p>
          </>
        )}
        <Files skill={skill} writable={writable} workspaceId={workspaceId} onSaved={saved} />
        {skill.id !== null ? (
          <Versions
            key={versionsTick}
            skillId={skill.id}
            writable={writable}
            onLoad={(md) => setContent(md)}
          />
        ) : null}
      </section>
    </>
  );
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <div className={styles.actions}>
      <button type="button" className="btn btn-quiet btn-small" onClick={onBack}>
        All skills
      </button>
    </div>
  );
}

function Files({
  skill,
  writable,
  workspaceId,
  onSaved,
}: {
  skill: SkillDetail;
  writable: boolean;
  workspaceId: string;
  onSaved: (version: number, what: string) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ path: string; content: string; isNew: boolean } | null>(null);
  const { busy, error, setError, write } = useWrite(workspaceId);
  const paths = Object.keys(skill.files).sort();
  const scope = skill.scope as WritableSkillScope;

  const saveFile = async (event: React.FormEvent) => {
    event.preventDefault();
    if (editing === null) return;
    const problem = pathProblem(editing.path);
    if (problem !== null) {
      setError(problem);
      return;
    }
    const done = await write("file", {
      action: "write_file",
      name: skill.name,
      scope,
      file_path: editing.path,
      file_content: editing.content,
    });
    if (done) {
      setEditing(null);
      onSaved(done.version, `The file ${editing.path} is saved.`);
    }
  };

  const remove = async (path: string) => {
    if (!window.confirm(`Remove the file ${path}?`)) return;
    const done = await write(`remove:${path}`, { action: "remove_file", name: skill.name, scope, file_path: path });
    if (done) onSaved(done.version, `The file ${path} is removed.`);
  };

  return (
    <div className={styles.section}>
      <div className={styles.row}>
        <h3 className={styles.subheading}>Files</h3>
        {writable && editing === null ? (
          <button
            type="button"
            className="btn btn-small"
            onClick={() => setEditing({ path: "references/", content: "", isNew: true })}
          >
            Add file
          </button>
        ) : null}
      </div>
      {paths.length === 0 && editing === null ? <p className={styles.meta}>No files. SKILL.md is the whole skill.</p> : null}
      {paths.length > 0 ? (
        <ul className={styles.list}>
          {paths.map((path) => (
            <li key={path} className={styles.form}>
              <div className={styles.row}>
                <span className={`mono ${styles.name}`}>{path}</span>
                <div className={styles.actions}>
                  <button
                    type="button"
                    className="btn btn-quiet btn-small"
                    aria-expanded={open === path}
                    onClick={() => setOpen((o) => (o === path ? null : path))}
                  >
                    {open === path ? "Hide" : "Show"}
                  </button>
                  {writable ? (
                    <>
                      <button
                        type="button"
                        className="btn btn-small"
                        disabled={busy !== null}
                        onClick={() => setEditing({ path, content: skill.files[path] ?? "", isNew: false })}
                      >
                        Edit
                      </button>
                      <button
                        type="button"
                        className="btn btn-danger btn-small"
                        disabled={busy !== null}
                        onClick={() => void remove(path)}
                      >
                        {busy === `remove:${path}` ? "Removing" : "Remove"}
                      </button>
                    </>
                  ) : null}
                </div>
              </div>
              {open === path ? <pre className={styles.pre}>{skill.files[path]}</pre> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {editing !== null ? (
        <form className={styles.form} onSubmit={saveFile} aria-label="Edit a file">
          <label className="field">
            <span className="label">Path</span>
            <input
              className="input mono"
              value={editing.path}
              readOnly={!editing.isNew}
              maxLength={200}
              onChange={(e) => setEditing((f) => f && { ...f, path: e.target.value })}
            />
            <span className="hint">For example references/notes.md. Folders: references, templates, scripts, assets.</span>
          </label>
          <label className="field">
            <span className="label">Content</span>
            <textarea
              className={`input ${styles.editor}`}
              value={editing.content}
              spellCheck={false}
              onChange={(e) => setEditing((f) => f && { ...f, content: e.target.value })}
            />
          </label>
          {error ? (
            <p className="alert alert-error" role="alert">
              {error}
            </p>
          ) : null}
          <div className={styles.actions}>
            <button type="submit" className="btn btn-primary btn-small" disabled={busy !== null}>
              {busy === "file" ? "Saving" : "Save file"}
            </button>
            <button type="button" className="btn btn-quiet btn-small" onClick={() => setEditing(null)}>
              Cancel
            </button>
          </div>
        </form>
      ) : error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function Versions({
  skillId,
  writable,
  onLoad,
}: {
  skillId: string;
  writable: boolean;
  onLoad: (skillMd: string) => void;
}) {
  const api = useApi();
  const load = useCallback(() => api.listSkillVersions(skillId), [api, skillId]);
  const versions = useLoad<SkillVersion[]>(load, "Could not load the versions.");
  const [shown, setShown] = useState<SkillVersionDetail | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const show = async (version: number) => {
    if (shown?.version === version) {
      setShown(null);
      return;
    }
    setBusy(version);
    setError(null);
    try {
      setShown(await api.getSkillVersion(skillId, version));
    } catch (e) {
      setError(errorText(e, "Could not load the version."));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={styles.section}>
      <h3 className={styles.subheading}>Versions</h3>
      {versions.error ? (
        <ErrorState title="The versions did not load" message={versions.error} onRetry={versions.reload} />
      ) : versions.data === null ? (
        <LoadingState label="Loading the versions" />
      ) : versions.data.length === 0 ? (
        <p className={styles.meta}>No versions yet.</p>
      ) : (
        <ul className={styles.list}>
          {versions.data.map((v, i) => (
            <li key={v.version} className={styles.form}>
              <div className={styles.row}>
                <div className={styles.name}>
                  <span>Version {v.version}</span>
                  <span className="badge">{v.author_kind === "user" ? "Person" : "Agent"}</span>
                  <span className={styles.meta}>
                    {v.change} · {relativeTime(v.created_at, now)}
                  </span>
                </div>
                <button
                  type="button"
                  className="btn btn-quiet btn-small"
                  aria-expanded={shown?.version === v.version}
                  disabled={busy !== null}
                  onClick={() => void show(v.version)}
                >
                  {busy === v.version ? "Loading" : shown?.version === v.version ? "Hide" : "Show"}
                </button>
              </div>
              {v.summary ? <p className={styles.text}>{v.summary}</p> : null}
              {shown?.version === v.version ? (
                <>
                  <pre className={styles.pre}>{shown.skill_md}</pre>
                  {writable && i > 0 ? (
                    <div className={styles.actions}>
                      <button type="button" className="btn btn-small" onClick={() => onLoad(shown.skill_md)}>
                        Load into the editor
                      </button>
                      <span className={styles.meta}>Then save to make it the newest version.</span>
                    </div>
                  ) : null}
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
