import { useEffect, useMemo, useState } from "react";
import { getRuntimeSettings, saveRuntimeSettings, type RuntimeKnob } from "../api";
import "./RuntimeLimitsPanel.css";

/* The dials that decide how hard the agent tries before it gives up.
 *
 * One card per group in the page's own auto-fit grid, so the cards tile
 * instead of stacking and the section is as tall as the tallest card rather
 * than the sum of every knob.
 *
 * Saving is ONE sticky bar, not a button per card. Per-card buttons meant
 * five controls that were disabled almost all of the time, and an edit in a
 * card you had scrolled past was easy to leave unsaved. A single bar that
 * appears only when something is dirty says exactly how many changes are
 * pending and cannot be scrolled away from.
 *
 * Help text is a tooltip on the label and the input rather than a paragraph
 * per row: fourteen inline explanations were a wall, and the label plus the
 * unit hint already carries the meaning at a glance.
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
  const [saving, setSaving] = useState(false);
  const [justSaved, setJustSaved] = useState(false);
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

  /** Only genuine differences count: typing a value back to what it already
   *  was should not leave the save bar hanging around asking to be dismissed. */
  const dirty = useMemo(() => {
    if (!knobs) return [] as string[];
    return Object.keys(edits).filter((n) => {
      const current = values[n] ?? knobs[n]?.default;
      const next = Number(edits[n]);
      return Number.isFinite(next) && next !== current;
    });
  }, [edits, values, knobs]);

  async function save() {
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, number> = {};
      for (const name of dirty) payload[name] = Number(edits[name]);
      const res = await saveRuntimeSettings(payload);
      setValues(res.values);
      setEdits({});
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 4000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
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
      {groups.map(([group, names]) => (
        <section key={group} className="settings-card">
          <h2>{group}</h2>
          <div className="rl-rows">
            {names.map((name) => {
              const k = knobs[name];
              const current = values[name] ?? k.default;
              const shown = edits[name] ?? String(current);
              const changedHere = dirty.includes(name);
              return (
                <div key={name} className={`rl-row ${changedHere ? "rl-row--dirty" : ""}`}>
                  <label className="rl-label" htmlFor={`rl-${name}`} title={k.help}>
                    {k.label}
                    {current !== k.default && !changedHere && (
                      <span className="rl-dot" title={`Default is ${k.default}${k.unit}`} />
                    )}
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
        </section>
      ))}

      {(dirty.length > 0 || error || justSaved) && (
        <div className="rl-savebar" role="status">
          {error ? (
            <span className="rl-savebar-error">{error}</span>
          ) : justSaved && dirty.length === 0 ? (
            <span className="rl-savebar-ok">Saved — applies from the next run.</span>
          ) : (
            <>
              <span className="rl-savebar-count">
                {dirty.length} unsaved change{dirty.length === 1 ? "" : "s"}
              </span>
              <button className="rl-savebar-discard" onClick={() => setEdits({})} disabled={saving}>
                Discard
              </button>
            </>
          )}
          {dirty.length > 0 && (
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          )}
        </div>
      )}
    </>
  );
}
