import { useEffect, useMemo, useState } from "react";
import { getRuntimeSettings, saveRuntimeSettings, type RuntimeKnob } from "../api";
import "./RuntimeLimitsPanel.css";

/* The dials that decide how hard the agent tries before it gives up.
 *
 * Renders one settings-card PER GROUP rather than one tall card holding
 * everything: the page's own layout is an auto-fit grid, so separate cards
 * tile side by side and the section stays as short as the widest column
 * instead of as long as the sum of every knob.
 *
 * Help text is a tooltip, not a paragraph. Fourteen inline explanations make
 * a wall; the label plus the range hint carries the meaning at a glance, and
 * the full reasoning is one hover away for the one knob you are changing.
 *
 * Bounds, labels, help and units all come from the server's knob spec -- one
 * source, so a knob added on the backend appears here with no frontend edit.
 */

/** 1200 is not a legible number of seconds. Say what it means. */
function humanise(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const mins = seconds / 60;
  if (mins < 90) return `${Number.isInteger(mins) ? mins : mins.toFixed(1)} min`;
  const hours = mins / 60;
  return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} hr`;
}

export function RuntimeLimitsPanel() {
  const [knobs, setKnobs] = useState<Record<string, RuntimeKnob> | null>(null);
  const [values, setValues] = useState<Record<string, number>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [savingGroup, setSavingGroup] = useState<string | null>(null);
  const [savedGroup, setSavedGroup] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getRuntimeSettings()
      .then((d) => {
        if (cancelled) return;
        setKnobs(d.knobs);
        setValues(d.values);
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : "failed to load"));
    return () => {
      cancelled = true;
    };
  }, []);

  const groups = useMemo(() => {
    if (!knobs) return [] as [string, string[]][];
    const byGroup: Record<string, string[]> = {};
    for (const [name, k] of Object.entries(knobs)) (byGroup[k.group] ??= []).push(name);
    return Object.entries(byGroup);
  }, [knobs]);

  /** Each card saves only its own knobs, so the button means what it says. */
  async function saveGroup(group: string, names: string[]) {
    setSavingGroup(group);
    setError(null);
    setSavedGroup(null);
    try {
      const payload: Record<string, number> = {};
      for (const name of names) {
        if (!(name in edits)) continue;
        const n = Number(edits[name]);
        if (Number.isFinite(n)) payload[name] = n;
      }
      const res = await saveRuntimeSettings(payload);
      setValues(res.values);
      setEdits((p) => {
        const next = { ...p };
        for (const name of names) delete next[name];
        return next;
      });
      setSavedGroup(group);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSavingGroup(null);
    }
  }

  if (error && !knobs) {
    return (
      <section className="settings-card">
        <h2>Runtime limits</h2>
        <div className="settings-error">{error}</div>
      </section>
    );
  }
  if (!knobs) {
    return (
      <section className="settings-card">
        <h2>Runtime limits</h2>
        <p className="settings-hint">Loading…</p>
      </section>
    );
  }

  return (
    <>
      {groups.map(([group, names]) => {
        const dirty = names.filter((n) => n in edits && Number(edits[n]) !== (values[n] ?? knobs[n].default));
        return (
          <section key={group} className="settings-card">
            <h2>{group}</h2>
            <div className="rl-rows">
              {names.map((name) => {
                const k = knobs[name];
                const current = values[name] ?? k.default;
                const shown = edits[name] ?? String(current);
                const isDefault = current === k.default;
                return (
                  <div key={name} className="rl-row">
                    <label className="rl-label" htmlFor={`rl-${name}`} title={k.help}>
                      {k.label}
                      {!isDefault && <span className="rl-dot" title={`Default ${k.default}${k.unit}`} />}
                    </label>
                    <div className="rl-control">
                      <input
                        id={`rl-${name}`}
                        className="rl-input"
                        type="number"
                        inputMode="decimal"
                        min={k.min}
                        max={k.max}
                        title={k.help}
                        value={shown}
                        onChange={(e) => setEdits((p) => ({ ...p, [name]: e.target.value }))}
                      />
                      <span
                        className="rl-meta"
                        title={`Allowed ${k.min}–${k.max}${k.unit}. Ships as ${k.default}${k.unit}.`}
                      >
                        {k.unit === "s" ? humanise(Number(shown)) : k.unit}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="rl-actions">
              <button
                className="btn btn-primary"
                disabled={dirty.length === 0 || savingGroup === group}
                onClick={() => saveGroup(group, names)}
              >
                {savingGroup === group ? "Saving…" : dirty.length ? `Save ${dirty.length}` : "Save"}
              </button>
              {savedGroup === group && <span className="rl-saved">Applies from the next run.</span>}
            </div>
          </section>
        );
      })}
      {error && (
        <section className="settings-card">
          <div className="settings-error">{error}</div>
        </section>
      )}
    </>
  );
}
