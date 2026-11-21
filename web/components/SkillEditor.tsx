"use client";

// The skill editor (docs/architecture.md, Section 9.6). The user reads the skills of the
// workspace, writes the skills of the scopes that they can change, and reads old versions.
// A built-in skill is read-only. A save is a new version with the author "user". The curator
// (Section 9.7) marks unused skills as stale and archives them. The user pins a skill to keep it,
// and restores an archived skill.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  errorText,
  type ArchivedSkill,
  type SkillCatalog,
  type SkillDetail,
  type SkillScope,
  type SkillSummary,
  type SkillVersionDetail,
  type SkillWriteBody,
  type WritableSkillScope,
} from "@/lib/api";
import {
  curatorBadge,
  filterSkills,
  nameProblem,
  pathProblem,
  SCOPE_LABELS,
  skillTemplate,
  STALE_TEXT,
} from "@/lib/skills";
import { relativeTime } from "@/lib/time";
import { useApi } from "./AuthProvider";
import { useData } from "./DataProvider";
import { PageHeader } from "./PageHeader";
import { ErrorState, LoadingState } from "./ui";
import { useFocusWhen } from "./useFocusWhen";
import { useLoad } from "./useLoad";
import styles from "./Page.module.css";

type Open = { kind: "skill"; name: string; scope: SkillScope } | { kind: "new" } | null;

/**
 * The skill editor. `embedded`: the settings console (M7.10) shows it inside a section, so the
 * page frame and the header stay out. `canCreate` is false when the role cannot write skills.
 */
export function SkillEditor({ embedded = false, canCreate = true }: { embedded?: boolean; canCreate?: boolean } = {}) {
  const { me, meError, reloadMe, workspaces, workspaceId } = useData();
  if (meError) return <ErrorState message={meError} onRetry={reloadMe} />;
  if (me === null) return <LoadingState label="Loading the skills" />;
  const workspace = workspaces.find((w) => w.id === workspaceId) ?? null;

  if (embedded) {
    return workspace === null ? (
      <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
    ) : (
      <WorkspaceSkills key={workspace.id} workspaceId={workspace.id} canCreate={canCreate} />
    );
  }

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
      {workspace === null ? (
        <p className="alert">You are not in a workspace yet. Ask an admin of your org to add you.</p>
      ) : (
        // The key gives each workspace its own catalog, so one workspace never shows the skills
        // of another one while the new list loads.
        <WorkspaceSkills key={workspace.id} workspaceId={workspace.id} canCreate />
      )}
    </div>
  );
}

function WorkspaceSkills({ workspaceId, canCreate }: { workspaceId: string; canCreate: boolean }) {
  const api = useApi();
  const [open, setOpen] = useState<Open>(null);
  // The row that the list gives the focus to when it shows again: "new" or `scope:name`.
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => api.listSkills(workspaceId), [api, workspaceId]);
  const catalog = useLoad(load, "Could not load the skills.");

  const back = (text: string | null) => {
    setOpen(null);
    setNotice(text);
    catalog.reload();
  };

  return (
    <>
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {catalog.error ? (
        <ErrorState title="The skills did not load" message={catalog.error} onRetry={catalog.reload} />
      ) : catalog.data === null ? (
        <LoadingState label="Loading the skills" />
      ) : open?.kind === "new" ? (
        <NewSkill
          workspaceId={workspaceId}
          scopes={catalog.data.writable_scopes}
          onCancel={() => setOpen(null)}
          onCreated={(name, scope) => {
            setNotice(`The skill ${name} is created.`);
            setFocusKey(`${scope}:${name}`);
            catalog.reload();
            setOpen({ kind: "skill", name, scope });
          }}
        />
      ) : open?.kind === "skill" ? (
        <SkillView
          key={`${open.scope}:${open.name}`}
          workspaceId={workspaceId}
          name={open.name}
          scope={open.scope}
          onBack={() => back(null)}
          onOpen={(scope) => {
            setFocusKey(`${scope}:${open.name}`);
            setOpen({ kind: "skill", name: open.name, scope });
          }}
          onArchived={() => back(`The skill ${open.name} is archived.`)}
          onSaved={(text) => {
            setNotice(text);
            if (text) catalog.reload();
          }}
        />
      ) : (
        <SkillsHome
          workspaceId={workspaceId}
          catalog={catalog.data}
          canCreate={canCreate}
          filter={filter}
          onFilter={setFilter}
          focusKey={focusKey}
          onNew={() => {
            setNotice(null);
            setFocusKey("new");
            setOpen({ kind: "new" });
          }}
          onOpen={(s) => {
            setNotice(null);
            setFocusKey(`${s.scope}:${s.name}`);
            setOpen({ kind: "skill", name: s.name, scope: s.scope });
          }}
          onRestored={(name) => {
            setNotice(`The skill ${name} is restored.`);
            catalog.reload();
          }}
        />
      )}
    </>
  );
}

function SkillsHome({
  workspaceId,
  catalog,
  canCreate,
  filter,
  onFilter,
  focusKey,
  onNew,
  onOpen,
  onRestored,
}: {
  workspaceId: string;
  catalog: SkillCatalog;
  canCreate: boolean;
  filter: string;
  onFilter: (text: string) => void;
  focusKey: string | null;
  onNew: () => void;
  onOpen: (skill: SkillSummary) => void;
  onRestored: (name: string) => void;
}) {
  const newButton = useRef<HTMLButtonElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const rows = useRef(new Map<string, HTMLButtonElement>());
  const registerRow = useCallback((key: string, button: HTMLButtonElement | null) => {
    if (button === null) rows.current.delete(key);
    else rows.current.set(key, button);
  }, []);

  // The list shows again after a skill or the new skill form closes. The focus goes back to
  // the button that opened it, or to the heading if that button is gone.
  // After a restore, the focus goes to the restored skill when the new list has it, else to the
  // heading. The Restore button that had the focus is gone.
  const restoring = useRef<{ key: string; from: SkillCatalog } | null>(null);
  useEffect(() => {
    const pending = restoring.current;
    if (pending === null || pending.from === catalog) return;
    restoring.current = null;
    (rows.current.get(pending.key) ?? heading.current)?.focus();
  }, [catalog]);

  useEffect(() => {
    if (focusKey === null) return;
    const target = focusKey === "new" ? newButton.current : rows.current.get(focusKey);
    (target ?? heading.current)?.focus();
    // Only when the list mounts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <h2 ref={heading} tabIndex={-1} className="sr-only">
        All skills
      </h2>
      <div className={styles.toolbar}>
        <label className={`field ${styles.grow}`}>
          <span className="label">Filter</span>
          <input
            className="input"
            type="search"
            value={filter}
            placeholder="Name, description, or tag"
            onChange={(e) => onFilter(e.target.value)}
          />
        </label>
        {catalog.writable_scopes.length > 0 && canCreate ? (
          <button ref={newButton} type="button" className="btn btn-primary" onClick={onNew}>
            New skill
          </button>
        ) : null}
      </div>
      {!canCreate ? (
        <p className={styles.meta}>Your role cannot write skills. An org admin can change this.</p>
      ) : null}
      <SkillList
        skills={filterSkills(catalog.skills, filter)}
        total={catalog.skills.length}
        registerRow={registerRow}
        onOpen={onOpen}
      />
      <ArchivedSkills
        workspaceId={workspaceId}
        onRestored={(skill) => {
          restoring.current = { key: `${skill.scope}:${skill.name}`, from: catalog };
          onRestored(skill.name);
        }}
      />
    </>
  );
}

function ArchivedSkills({
  workspaceId,
  onRestored,
}: {
  workspaceId: string;
  onRestored: (skill: ArchivedSkill) => void;
}) {
  const api = useApi();
  const load = useCallback(() => api.listArchivedSkills(workspaceId), [api, workspaceId]);
  const archived = useLoad(load, "Could not load the archived skills.");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now] = useState(() => Date.now());

  const restore = async (skill: ArchivedSkill) => {
    setBusy(skill.id);
    setError(null);
    try {
      await api.setSkillState(workspaceId, skill.name, skill.scope, "restore");
      archived.reload();
      onRestored(skill);
    } catch (e) {
      setError(errorText(e, "The skill was not restored."));
    } finally {
      setBusy(null);
    }
  };

  if (archived.error) {
    return <ErrorState title="The archived skills did not load" message={archived.error} onRetry={archived.reload} />;
  }
  if (archived.data === null) return <LoadingState label="Loading the archived skills" />;
  if (archived.data.length === 0) return null;
  return (
    <details className={`card ${styles.card}`}>
      <summary>Archived skills ({archived.data.length})</summary>
      <p className={styles.meta}>
        The agents cannot load an archived skill. The curator archives the skills that agents wrote
        and nobody used for 90 days. It never deletes a skill. Restore a skill to use it again.
      </p>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
      <ul className={styles.list} aria-label="Archived skills">
        {archived.data.map((s) => (
          <li key={s.id} className={styles.row}>
            <div className={styles.cardHead}>
              <h3 className={styles.heading}>{s.name}</h3>
              <div className={styles.name}>
                <span className="badge">{SCOPE_LABELS[s.scope]}</span>
                <span className={styles.meta}>archived {relativeTime(s.archived_at, now)}</span>
              </div>
            </div>
            <p className={styles.text}>{s.description}</p>
            {s.can_restore ? (
              <div className={styles.actions}>
                <button
                  type="button"
                  className="btn btn-small"
                  disabled={busy !== null}
                  onClick={() => void restore(s)}
                >
                  {busy === s.id ? "Restoring" : "Restore"}
                  <span className="sr-only"> {s.name}</span>
                </button>
              </div>
            ) : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

function SkillList({
  skills,
  total,
  registerRow,
  onOpen,
}: {
  skills: SkillSummary[];
  total: number;
  registerRow: (key: string, button: HTMLButtonElement | null) => void;
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
                <CuratorBadge skill={s} />
                <span className={styles.meta}>v{s.version}</span>
              </div>
            </div>
            <p className={styles.text}>{s.description}</p>
            {s.tags.length > 0 ? <p className={styles.meta}>Tags: {s.tags.join(", ")}</p> : null}
            <div className={styles.actions}>
              <button
                ref={(button) => registerRow(`${s.scope}:${s.name}`, button)}
                type="button"
                className="btn btn-small"
                onClick={() => onOpen(s)}
              >
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

function useWrite(workspaceId: string, onStart?: () => void) {
  const api = useApi();
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const write = async (label: string, body: SkillWriteBody) => {
    setBusy(label);
    setError(null);
    onStart?.();
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
  const nameField = useRef<HTMLInputElement>(null);
  useFocusWhen(true, nameField);

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
            ref={nameField}
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
  onSaved: (text: string | null) => void;
}) {
  const api = useApi();
  const load = useCallback(() => api.getSkill(workspaceId, name, scope), [api, workspaceId, name, scope]);
  const detail = useLoad(load, "Could not load the skill.");
  const [content, setContent] = useState<string | null>(null);
  const [summary, setSummary] = useState("");
  const [versionsTick, setVersionsTick] = useState(0);
  // A new write ends the notice of the last one, so an old success never sits beside a new error.
  const { busy, error, setError, write } = useWrite(workspaceId, () => onSaved(null));
  const [pinning, setPinning] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  // The skill is open when it has loaded: the focus goes to its name.
  useFocusWhen(detail.data !== null, heading);

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
  const writable = skill.can_edit;
  const text = content ?? skill.skill_md;

  // A save or a file change makes a new version. The files and the versions load again.
  const changed = (version: number, what: string) => {
    onSaved(`${what} Version ${version}.`);
    setVersionsTick((n) => n + 1);
    detail.reload();
  };

  // The new version has the text of the editor, so the draft ends only here.
  const markdownSaved = (version: number) => {
    changed(version, "The skill is saved.");
    setContent(null);
    setSummary("");
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
    if (done) markdownSaved(done.version);
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

  const pin = async () => {
    const action = skill.pinned ? "unpin" : "pin";
    setPinning(true);
    setError(null);
    onSaved(null);
    try {
      await api.setSkillState(workspaceId, skill.name, skill.scope as WritableSkillScope, action);
      onSaved(skill.pinned ? `The skill ${skill.name} is not pinned now.` : `The skill ${skill.name} is pinned.`);
      detail.reload();
    } catch (e) {
      setError(errorText(e, "The pin did not change."));
    } finally {
      setPinning(false);
    }
  };

  return (
    <>
      <BackButton onBack={onBack} />
      <section className={`card ${styles.card}`} aria-label={skill.name}>
        <div className={styles.cardHead}>
          <h2 ref={heading} tabIndex={-1} className={styles.heading}>
            {skill.name}
          </h2>
          <div className={styles.name}>
            <span className={skill.scope === "builtin" ? "badge" : "badge badge-accent"}>
              {SCOPE_LABELS[skill.scope]}
            </span>
            <CuratorBadge skill={skill} />
            <span className={styles.meta}>v{skill.version}</span>
          </div>
        </div>
        <p className={styles.text}>{skill.description}</p>
        {skill.status === "stale" && !skill.pinned ? (
          <p className="alert alert-warning" role="note">
            {STALE_TEXT}
          </p>
        ) : null}
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
              <button
                type="button"
                className="btn btn-quiet btn-small"
                disabled={busy !== null || pinning}
                aria-pressed={skill.pinned}
                onClick={() => void pin()}
              >
                {pinning ? "Saving" : skill.pinned ? "Unpin" : "Pin"}
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
        <Files
          skill={skill}
          writable={writable}
          workspaceId={workspaceId}
          onChanged={changed}
          onWriteStart={() => onSaved(null)}
        />
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

function CuratorBadge({ skill }: { skill: SkillSummary }) {
  const badge = curatorBadge(skill);
  return badge ? <span className={`badge badge-${badge.tone}`}>{badge.label}</span> : null;
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
  onChanged,
  onWriteStart,
}: {
  skill: SkillDetail;
  writable: boolean;
  workspaceId: string;
  onChanged: (version: number, what: string) => void;
  onWriteStart: () => void;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ path: string; content: string; isNew: boolean } | null>(null);
  const { busy, error, setError, write } = useWrite(workspaceId, onWriteStart);
  const pathField = useRef<HTMLInputElement>(null);
  const contentField = useRef<HTMLTextAreaElement>(null);
  const addButton = useRef<HTMLButtonElement>(null);
  // A new file starts with its path. An old file has its path, so it starts with its content.
  useFocusWhen(editing !== null, editing?.isNew ? pathField : contentField, addButton);
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
      onChanged(done.version, `The file ${editing.path} is saved.`);
    }
  };

  const remove = async (path: string) => {
    if (!window.confirm(`Remove the file ${path}?`)) return;
    const done = await write(`remove:${path}`, { action: "remove_file", name: skill.name, scope, file_path: path });
    if (done) onChanged(done.version, `The file ${path} is removed.`);
  };

  return (
    <div className={styles.section}>
      <div className={styles.row}>
        <h3 className={styles.subheading}>Files</h3>
        {writable && editing === null ? (
          <button
            ref={addButton}
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
              ref={pathField}
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
              ref={contentField}
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
  const versions = useLoad(load, "Could not load the versions.");
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
