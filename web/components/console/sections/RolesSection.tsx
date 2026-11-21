"use client";

// The Roles section: the members of the org with their roles and workspaces, the invites, and
// a table of what each role can do. The owner always has every permission. The owner can change
// the Admin column. An owner or an admin can change the Member column.

import { useCallback, useState } from "react";
import { type EditableRole, errorText, type RolePermissions } from "@/lib/api";
import { can } from "@/lib/console";
import {
  type PermissionDraft,
  appliesTo,
  cellLabel,
  cellValue,
  changedCount,
  changedPermissions,
  EDITABLE_ROLE_LABELS,
  EDITABLE_ROLES,
  permissionNote,
  revokedLabels,
  toggleDraft,
} from "@/lib/rolePermissions";
import { useApi } from "../../AuthProvider";
import { useData } from "../../DataProvider";
import { OrgMembers } from "../../Members";
import { ErrorState, LoadingState } from "../../ui";
import { useLoad } from "../../useLoad";
import { useConsole } from "../Console";
import { SectionHeader } from "../parts";
import styles from "../../Page.module.css";
import own from "../../Members.module.css";
import roles from "./Roles.module.css";

export function RolesSection() {
  const { org } = useConsole();
  return (
    <>
      <SectionHeader id="roles" meta={`${org.name} · You are ${org.role}.`} />
      <OrgMembers org={org} canInvite={can(org, "members.invite")} />
      <PermissionEditor />
    </>
  );
}

type Drafts = Record<EditableRole, PermissionDraft>;
const NO_DRAFTS: Drafts = { admin: {}, member: {} };

function PermissionEditor() {
  const api = useApi();
  const { org } = useConsole();
  const { reloadMe } = useData();
  const load = useCallback(() => api.rolePermissions(org.id), [api, org.id]);
  const permissions = useLoad(load, "Could not load the permissions.");
  const [drafts, setDrafts] = useState<Drafts>(NO_DRAFTS);
  const [saving, setSaving] = useState<EditableRole | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const save = async (data: RolePermissions, role: EditableRole) => {
    const changed = changedPermissions(data, role, drafts[role]);
    const lost = revokedLabels(data, role, drafts[role]);
    if (
      lost.length > 0 &&
      !window.confirm(`Take these permissions from the role ${EDITABLE_ROLE_LABELS[role]}? ${lost.join(", ")}.`)
    ) {
      return;
    }
    setSaving(role);
    setError(null);
    setNotice(null);
    try {
      permissions.setData(await api.setRolePermissions(org.id, role, changed));
      setDrafts((d) => ({ ...d, [role]: {} }));
      setNotice(`The permissions of the role ${EDITABLE_ROLE_LABELS[role]} are saved.`);
      // The user can hold the role that changed, so the checks of the console change too.
      reloadMe();
    } catch (e) {
      // The draft stays, so the user can try again.
      setError(errorText(e, "The permissions were not saved."));
    } finally {
      setSaving(null);
    }
  };

  return (
    <section className={`card ${styles.card}`} aria-labelledby="roles-permissions">
      <h2 id="roles-permissions" className={styles.heading}>
        What each role can do
      </h2>
      {permissions.error ? (
        <ErrorState title="The permissions did not load" message={permissions.error} onRetry={permissions.reload} />
      ) : permissions.data === null ? (
        <LoadingState label="Loading the permissions" />
      ) : (
        <PermissionTable
          data={permissions.data}
          drafts={drafts}
          saving={saving}
          notice={notice}
          error={error}
          onToggle={(role, key, value) => {
            setNotice(null);
            setDrafts((d) => ({
              ...d,
              [role]: toggleDraft(permissions.data!, role, d[role], key, value),
            }));
          }}
          onDiscard={(role) => setDrafts((d) => ({ ...d, [role]: {} }))}
          onSave={(role) => void save(permissions.data!, role)}
        />
      )}
    </section>
  );
}

function PermissionTable({
  data,
  drafts,
  saving,
  notice,
  error,
  onToggle,
  onDiscard,
  onSave,
}: {
  data: RolePermissions;
  drafts: Drafts;
  saving: EditableRole | null;
  notice: string | null;
  error: string | null;
  onToggle: (role: EditableRole, key: RolePermissions["permissions"][number]["key"], value: boolean) => void;
  onDiscard: (role: EditableRole) => void;
  onSave: (role: EditableRole) => void;
}) {
  const editableRoles = EDITABLE_ROLES.filter((r) => data.editable[r]);
  return (
    <>
      <p className={styles.text}>
        The owner always has every permission. A dash means that the role cannot have the permission.
      </p>
      <div className={styles.scroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Permission</th>
              <th scope="col" className={roles.roleHead}>Owner</th>
              {EDITABLE_ROLES.map((role) => (
                <th key={role} scope="col" className={roles.roleHead}>
                  {EDITABLE_ROLE_LABELS[role]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.permissions.map((info) => (
              <tr key={info.key}>
                <th scope="row">
                  <div className={own.who}>
                    <span>{info.label}</span>
                    <span className={styles.meta}>{info.description}</span>
                  </div>
                </th>
                <td>Yes</td>
                {EDITABLE_ROLES.map((role) => (
                  <td key={role}>
                    {appliesTo(info, role) ? (
                      <input
                        type="checkbox"
                        aria-label={cellLabel(info, role)}
                        checked={cellValue(data, role, info.key, drafts[role])}
                        disabled={!data.editable[role] || saving !== null}
                        onChange={(e) => onToggle(role, info.key, e.target.checked)}
                      />
                    ) : (
                      <>
                        <span aria-hidden="true">—</span>
                        <span className="sr-only">{`${cellLabel(info, role)}: not available`}</span>
                      </>
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className={styles.meta}>{permissionNote(data.editable)}</p>
      {editableRoles.length > 0 ? (
        <div className={styles.actions}>
          {editableRoles.map((role) => {
            const count = changedCount(data, role, drafts[role]);
            return (
              <span key={role} className={styles.actions}>
                <button
                  type="button"
                  className="btn btn-primary btn-small"
                  disabled={count === 0 || saving !== null}
                  onClick={() => onSave(role)}
                >
                  {saving === role ? "Saving…" : `Save ${EDITABLE_ROLE_LABELS[role]}`}
                </button>
                {count > 0 ? (
                  <button
                    type="button"
                    className="btn btn-quiet btn-small"
                    disabled={saving !== null}
                    onClick={() => onDiscard(role)}
                  >
                    {`Discard ${EDITABLE_ROLE_LABELS[role]}`}
                  </button>
                ) : null}
              </span>
            );
          })}
        </div>
      ) : null}
      {notice ? (
        <p className="alert alert-success" role="status">
          {notice}
        </p>
      ) : null}
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : null}
    </>
  );
}
