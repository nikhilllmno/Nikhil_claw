"use client";

// The Tools section (M7.10): the tool groups of an agent, the policy of each tool, and the rules
// for every agent of the org. The tool policy routes write the audit rows.

import { useCallback, useMemo, useState } from "react";
import {
  type AgentProfile,
  type OrgToolPolicy,
  type PolicyAction,
  type ToolInfo,
  type ToolPolicy,
  type ToolPolicyRule,
  errorText,
} from "@/lib/api";
import { canManageAgents } from "@/lib/console";
import {
  POLICY_CHOICES,
  type PolicyChoice,
  type RuleRow,
  checkChangeCount,
  customRules,
  groupTools,
  newRuleRow,
  orgRuleFor,
  policyActionLabel,
  policyChanges,
  policyChoiceLabel,
  tableDraft,
  toRuleRows,
  validateRuleRows,
  wantedRules,
} from "@/lib/policy";
import { useApi } from "../../AuthProvider";
import { ErrorState, LoadingState, ToneBadge } from "../../ui";
import { useLoad } from "../../useLoad";
import admin from "../../Admin.module.css";
import styles from "../../Page.module.css";
import { CustomToolsPanel } from "../../CustomTools";
import { AgentProfileEditor, SectionHeader, useAgentChoice } from "../parts";
import own from "./Tools.module.css";

const ACTION_TONE = { allow: "accent", ask: "warning", deny: "danger" } as const;

function ActionBadge({ action }: { action: PolicyAction }) {
  return <ToneBadge tone={ACTION_TONE[action]}>{policyActionLabel(action)}</ToneBadge>;
}

const HARDLINE_NOTE =
  "The hardline deny list always applies. It blocks, for example, a recursive delete of the root folder. No rule can change it.";

export function ToolsSection() {
  const choice = useAgentChoice();
  const { org } = choice;
  const mayManage = canManageAgents(org);

  return (
    <>
      <SectionHeader
        id="tools"
        meta={mayManage ? null : "Only an org admin with the right to manage agents can change these settings."}
      />
      <AgentProfileEditor choice={choice}>
        {({ profile, readOnly, onSaved }) => (
          <AgentTools orgId={org.id} profile={profile} readOnly={readOnly} onSaved={onSaved} />
        )}
      </AgentProfileEditor>
      <OrgRules orgId={org.id} manage={mayManage} />
      <section className={`card ${styles.card}`} aria-label="Custom tools">
        <div className={styles.cardHead}>
          <h2 className={styles.subheading}>Custom tools</h2>
        </div>
        <p className={styles.text}>
          HTTP requests and Python or Node scripts that the agents find with tool search. A member adds tools for
          themselves. An admin adds the tools for the workspace and for the org.
        </p>
        <CustomToolsPanel key={choice.workspaceId} workspaceId={choice.workspaceId} />
      </section>
    </>
  );
}

// -- one agent ------------------------------------------------------------------------------------

function AgentTools({
  orgId,
  profile,
  readOnly,
  onSaved,
}: {
  orgId: string;
  profile: AgentProfile;
  readOnly: boolean;
  onSaved: (profile: AgentProfile) => void;
}) {
  const api = useApi();
  const profileId = profile.id;
  const loadPolicy = useCallback(() => api.toolPolicy(orgId, profileId), [api, orgId, profileId]);
  const { data: policy, setData: setPolicy, error, reload } = useLoad(loadPolicy, "Could not load the tool policy.");

  return (
    <>
      <ToolsetsCard
        orgId={orgId}
        profile={profile}
        readOnly={readOnly}
        onSaved={(saved) => {
          onSaved(saved);
          reload(); // The tools of a group that is off show as off.
        }}
      />
      {error ? (
        <ErrorState title="The tool policy did not load" message={error} onRetry={reload} />
      ) : policy === null ? (
        <LoadingState label="Loading the tool policy" />
      ) : (
        <PolicyCard
          orgId={orgId}
          profileId={profileId}
          tools={policy.tools}
          rules={policy.rules}
          orgRules={policy.org_rules}
          maxRules={policy.max_rules}
          readOnly={readOnly || !policy.editable}
          onSaved={setPolicy}
        />
      )}
    </>
  );
}

function ToolsetsCard({
  orgId,
  profile,
  readOnly,
  onSaved,
}: {
  orgId: string;
  profile: AgentProfile;
  readOnly: boolean;
  onSaved: (profile: AgentProfile) => void;
}) {
  const api = useApi();
  const [selected, setSelected] = useState<string[]>(profile.toolsets);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const dirty =
    selected.length !== profile.toolsets.length || selected.some((name) => !profile.toolsets.includes(name));

  const toggle = (name: string) => {
    setSelected((list) => (list.includes(name) ? list.filter((n) => n !== name) : [...list, name]));
    setSaved(false);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty || readOnly) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api.updateAgentProfile(orgId, profile.id, { toolsets: selected });
      onSaved(result);
      setSelected(result.toolsets);
      setSaved(true);
    } catch (e) {
      setError(errorText(e, "The tool groups were not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={save} aria-labelledby="tools-groups">
      <h2 id="tools-groups" className={styles.heading}>
        Tool groups
      </h2>
      <p className="hint">
        The groups of tools that the agent can use. A group that you turn off stops at the next tool call in every
        session. A risky tool still asks for approval.
      </p>
      <fieldset className={admin.choices} disabled={readOnly || saving}>
        <legend className="sr-only">Tool groups</legend>
        {profile.options.toolsets.map((set) => (
          <label key={set.name} className={admin.choice}>
            <input type="checkbox" checked={selected.includes(set.name)} onChange={() => toggle(set.name)} />
            <span className={admin.choiceText}>
              <strong>{set.name}</strong>
              <span className="hint">{set.tools.join(", ")}</span>
            </span>
          </label>
        ))}
      </fieldset>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : saved ? (
        <p className="alert alert-success" role="status">
          Saved. The change applies at the next tool call.
        </p>
      ) : null}
      {readOnly ? null : (
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary btn-small" disabled={!dirty || saving}>
            {saving ? "Saving…" : "Save tool groups"}
          </button>
          <button
            type="button"
            className="btn btn-small"
            disabled={!dirty || saving}
            onClick={() => setSelected(profile.toolsets)}
          >
            Discard
          </button>
        </div>
      )}
    </form>
  );
}

function PolicyCard({
  orgId,
  profileId,
  tools,
  rules,
  orgRules,
  maxRules,
  readOnly,
  onSaved,
}: {
  orgId: string;
  profileId: string;
  tools: ToolInfo[];
  rules: ToolPolicyRule[];
  orgRules: ToolPolicyRule[];
  maxRules: number;
  readOnly: boolean;
  onSaved: (policy: ToolPolicy) => void;
}) {
  const api = useApi();
  const [table, setTable] = useState<Record<string, PolicyChoice>>(() => tableDraft(rules, tools));
  const [rows, setRows] = useState<RuleRow[]>(() => toRuleRows(customRules(rules, tools)));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // An edit while a save runs would be lost when the answer comes, so the form locks.
  const locked = readOnly || saving;

  const groups = useMemo(() => groupTools(tools), [tools]);
  const names = useMemo(() => new Set(tools.map((t) => t.name)), [tools]);
  const changes = useMemo(() => {
    if (validateRuleRows(rows, names) !== null) return [];
    return policyChanges(rules, wantedRules(table, rows));
  }, [rules, table, rows, names]);
  const problem = validateRuleRows(rows, names) ?? checkChangeCount(changes, maxRules);
  const dirty = changes.length > 0;

  const choose = (tool: string, choice: PolicyChoice) => {
    setTable((t) => ({ ...t, [tool]: choice }));
    setSaved(false);
  };
  const updateRow = (uid: number, change: Partial<RuleRow>) => {
    setRows((list) => list.map((r) => (r.uid === uid ? { ...r, ...change } : r)));
    setSaved(false);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty || problem !== null || readOnly) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api.setToolPolicy(orgId, profileId, changes);
      onSaved(result);
      setTable(tableDraft(result.rules, result.tools));
      setRows(toRuleRows(customRules(result.rules, result.tools)));
      setSaved(true);
    } catch (e) {
      setError(errorText(e, "The tool policy was not saved."));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className={`card ${styles.card}`} onSubmit={save} aria-labelledby="tools-policy">
      <h2 id="tools-policy" className={styles.heading}>
        Tool policy of the agent
      </h2>
      <p className="hint">
        A rule for the agent decides what happens when it calls a tool: allow it, ask a person, or deny it. Default
        means no rule for the agent. {HARDLINE_NOTE}
      </p>
      <div className={styles.scroll}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Tool</th>
              <th scope="col">Default</th>
              <th scope="col">Rule for every agent</th>
              <th scope="col">Rule for this agent</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <GroupRows
                key={group.toolset}
                group={group}
                table={table}
                orgRules={orgRules}
                readOnly={locked}
                onChoose={choose}
              />
            ))}
          </tbody>
        </table>
      </div>

      <h3 className={styles.subheading}>Custom rules</h3>
      <p className="hint">
        A rule for a prefix ends with a star, for example mcp__github__*. It covers each tool that starts with the
        prefix.
      </p>
      {rows.length === 0 ? <p className="hint">No custom rules.</p> : null}
      <ul className={own.rules}>
        {rows.map((row) => (
          <li key={row.uid} className={own.rule}>
            <label className={`field ${own.ruleTool}`}>
              <span className="label">Tool or prefix</span>
              <input
                className="input"
                value={row.tool}
                maxLength={200}
                readOnly={locked}
                onChange={(e) => updateRow(row.uid, { tool: e.target.value })}
              />
            </label>
            <label className={`field ${own.ruleAction}`}>
              <span className="label">Rule</span>
              <select
                className="input"
                value={row.action}
                disabled={locked}
                onChange={(e) => updateRow(row.uid, { action: e.target.value as PolicyAction })}
              >
                {(["allow", "ask", "deny"] as const).map((a) => (
                  <option key={a} value={a}>
                    {policyActionLabel(a)}
                  </option>
                ))}
              </select>
            </label>
            {readOnly ? null : (
              <button
                type="button"
                className="btn btn-small btn-danger"
                disabled={saving}
                onClick={() => {
                  setRows((list) => list.filter((r) => r.uid !== row.uid));
                  setSaved(false);
                }}
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
      {readOnly ? null : (
        <div className={styles.actions}>
          <button
            type="button"
            className="btn btn-small"
            disabled={saving}
            onClick={() => {
              setRows((list) => [...list, newRuleRow()]);
              setSaved(false);
            }}
          >
            Add a custom rule
          </button>
        </div>
      )}

      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : problem ? (
        <p className="alert alert-warning">{problem}</p>
      ) : saved ? (
        <p className="alert alert-success" role="status">
          Saved. The rules apply at the next tool call.
        </p>
      ) : null}
      {readOnly ? (
        <p className="alert" role="note">
          You can read the policy but not change it. Only an org admin with the right to manage agents can.
        </p>
      ) : (
        <div className={styles.actions}>
          <button type="submit" className="btn btn-primary" disabled={!dirty || saving || problem !== null}>
            {saving ? "Saving…" : dirty ? `Save ${changes.length} ${changes.length === 1 ? "change" : "changes"}` : "Save changes"}
          </button>
          <button
            type="button"
            className="btn"
            disabled={!dirty || saving}
            onClick={() => {
              setTable(tableDraft(rules, tools));
              setRows(toRuleRows(customRules(rules, tools)));
              setError(null);
            }}
          >
            Discard
          </button>
        </div>
      )}
    </form>
  );
}

function GroupRows({
  group,
  table,
  orgRules,
  readOnly,
  onChoose,
}: {
  group: { toolset: string; enabled: boolean; tools: ToolInfo[] };
  table: Record<string, PolicyChoice>;
  orgRules: ToolPolicyRule[];
  readOnly: boolean;
  onChoose: (tool: string, choice: PolicyChoice) => void;
}) {
  return (
    <>
      <tr className={own.groupRow}>
        <th scope="colgroup" colSpan={4}>
          <span className={own.groupHead}>
            {group.toolset}
            <ToneBadge tone={group.enabled ? "accent" : "neutral"}>
              {group.enabled ? "Group on" : "Group off"}
            </ToneBadge>
          </span>
        </th>
      </tr>
      {group.tools.map((tool) => {
        const org = orgRuleFor(tool.name, orgRules);
        return (
          <tr key={tool.name}>
            <td>
              <span className={own.tool}>{tool.name}</span>
            </td>
            <td>
              <span className={own.verdict}>
                <ActionBadge action={tool.default} />
                {tool.always_ask ? <span className="hint">Always asks a person</span> : null}
              </span>
            </td>
            <td>
              {org ? (
                <span className={own.verdict}>
                  <ActionBadge action={org.action} />
                  {org.via === tool.name ? null : <span className="hint">{org.via}</span>}
                </span>
              ) : (
                <span className="hint">None</span>
              )}
            </td>
            <td>
              <select
                className="input"
                aria-label={`Rule of the agent for ${tool.name}`}
                value={table[tool.name] ?? "default"}
                disabled={readOnly}
                onChange={(e) => onChoose(tool.name, e.target.value as PolicyChoice)}
              >
                {POLICY_CHOICES.map((choice) => (
                  <option key={choice} value={choice}>
                    {policyChoiceLabel(choice)}
                  </option>
                ))}
              </select>
            </td>
          </tr>
        );
      })}
    </>
  );
}

// -- the rules of the org -------------------------------------------------------------------------

function OrgRules({ orgId, manage }: { orgId: string; manage: boolean }) {
  const api = useApi();
  const load = useCallback(() => api.orgToolPolicy(orgId), [api, orgId]);
  const { data: policy, setData: setPolicy, error, reload } = useLoad(load, "Could not load the rules of the org.");

  return (
    <section className={`card ${styles.card}`} aria-labelledby="tools-org">
      <h2 id="tools-org" className={styles.heading}>
        Rules for every agent
      </h2>
      <p className="hint">
        These rules apply to each agent of the org. A rule for one agent can add to them. {HARDLINE_NOTE}
      </p>
      {error ? (
        <ErrorState title="The rules did not load" message={error} onRetry={reload} />
      ) : policy === null ? (
        <LoadingState label="Loading the rules" />
      ) : (
        <OrgRulesForm
          orgId={orgId}
          rules={policy.rules}
          maxRules={policy.max_rules}
          readOnly={!policy.editable || !manage}
          onSaved={setPolicy}
        />
      )}
    </section>
  );
}

function OrgRulesForm({
  orgId,
  rules,
  maxRules,
  readOnly,
  onSaved,
}: {
  orgId: string;
  rules: ToolPolicyRule[];
  maxRules: number;
  readOnly: boolean;
  onSaved: (policy: OrgToolPolicy) => void;
}) {
  const api = useApi();
  const [rows, setRows] = useState<RuleRow[]>(() => toRuleRows(rules));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  // An edit while a save runs would be lost when the answer comes, so the form locks.
  const locked = readOnly || saving;

  const rowsProblem = validateRuleRows(rows);
  const changes = useMemo(
    () => (rowsProblem === null ? policyChanges(rules, wantedRules({}, rows)) : []),
    [rules, rows, rowsProblem],
  );
  const problem = rowsProblem ?? checkChangeCount(changes, maxRules);
  const dirty = changes.length > 0;

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!dirty || problem !== null || readOnly) return;
    setSaving(true);
    setError(null);
    try {
      const result = await api.setOrgToolPolicy(orgId, changes);
      onSaved(result);
      setRows(toRuleRows(result.rules));
      setSaved(true);
    } catch (e) {
      setError(errorText(e, "The rules were not saved."));
    } finally {
      setSaving(false);
    }
  };

  const updateRows = (change: (list: RuleRow[]) => RuleRow[]) => {
    setRows(change);
    setSaved(false);
  };
  const updateRow = (uid: number, change: Partial<RuleRow>) =>
    updateRows((list) => list.map((r) => (r.uid === uid ? { ...r, ...change } : r)));

  return (
    <form className={styles.form} onSubmit={save}>
      {rows.length === 0 ? <p className="hint">The org has no rule for every agent.</p> : null}
      <ul className={own.rules}>
        {rows.map((row) => (
          <li key={row.uid} className={own.rule}>
            <label className={`field ${own.ruleTool}`}>
              <span className="label">Tool or prefix</span>
              <input
                className="input"
                value={row.tool}
                maxLength={200}
                readOnly={locked}
                onChange={(e) => updateRow(row.uid, { tool: e.target.value })}
              />
            </label>
            <label className={`field ${own.ruleAction}`}>
              <span className="label">Rule</span>
              <select
                className="input"
                value={row.action}
                disabled={locked}
                onChange={(e) => updateRow(row.uid, { action: e.target.value as PolicyAction })}
              >
                {(["allow", "ask", "deny"] as const).map((a) => (
                  <option key={a} value={a}>
                    {policyActionLabel(a)}
                  </option>
                ))}
              </select>
            </label>
            {readOnly ? null : (
              <button
                type="button"
                className="btn btn-small btn-danger"
                disabled={saving}
                onClick={() => updateRows((list) => list.filter((r) => r.uid !== row.uid))}
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
      {error ? (
        <p className="alert alert-error" role="alert">
          {error}
        </p>
      ) : problem ? (
        <p className="alert alert-warning">{problem}</p>
      ) : saved ? (
        <p className="alert alert-success" role="status">
          Saved. The rules apply at the next tool call.
        </p>
      ) : null}
      {readOnly ? (
        <p className="alert" role="note">
          You can read the rules but not change them. Only an org admin with the right to manage agents can.
        </p>
      ) : (
        <div className={styles.actions}>
          <button type="button" className="btn btn-small" disabled={saving} onClick={() => updateRows((list) => [...list, newRuleRow()])}>
            Add a rule
          </button>
          <button type="submit" className="btn btn-primary btn-small" disabled={!dirty || saving || problem !== null}>
            {saving ? "Saving…" : "Save rules"}
          </button>
        </div>
      )}
    </form>
  );
}
