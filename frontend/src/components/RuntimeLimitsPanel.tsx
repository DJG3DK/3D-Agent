import { useEffect, useMemo, useState } from "react";
import { getRuntimeSettings, saveRuntimeSettings, type RuntimeKnob } from "../api";
import "./RuntimeLimitsPanel.css";

/* The dials that decide how hard the agent tries before giving up.
 *
 * These were module constants and env vars, so changing one meant editing a
 * file and restarting -- and a restart is precisely what you cannot do while
 * the thing you want to retune is running. They are stored now, and every one
 * is read at the point of use, so a change lands on the NEXT turn or task and
 * never mutates something already in flight.
 *
 * Bounds, labels, help text and units all come from the server's own knob
 * spec rather than being duplicated here: one source, so a knob added on the
 * backend appears without a frontend change.
 */

/** 1200 is not a legible number of seconds. Show what it means. */
function humanise(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 90) return `${Math.round(seconds)} sec`;
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
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

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
    if (!knobs) return [];
    const byGroup: Record<string, string[]> = {};
    for (const [name, k] of Object.entries(knobs)) (byGroup[k.group] ??= []).push(name);
    return Object.entries(byGroup);
  }, [knobs]);

  const dirty = Object.keys(edits).length > 0;

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const payload: Record<string, number> = {};
      for (const [name, raw] of Object.entries(edits)) {
        const n = Number(raw);
        if (Number.isFinite(n)) payload[name] = n;
      }
      const res = await saveRuntimeSettings(payload);
      setValues(res.values);
      setEdits({});
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  if (error && !knobs) return <section className="settings-card"><h2>Runtime limits</h2><div className="settings-error">{error}</div></section>;
  if (!knobs) return <section className="settings-card"><h2>Runtime limits</h2><p className="settings-hint">Loading…</p></section>;

  return (
    <section className="settings-card">
      <h2>Runtime limits</h2>
      <p className="settings-hint">
        How hard the agent tries before it gives up. Changes apply to the next turn or task —
        anything already running keeps the limits it started with. Budget is the only thing that
        stops work abruptly; the rest end a unit of work cleanly and tell you why.
      </p>

      {groups.map(([group, names]) => (
        <div key={group} className="rl-group">
          <h3 className="rl-group-title">{group}</h3>
          {names.map((name) => {
            const k = knobs[name];
            const current = values[name] ?? k.default;
            const shown = edits[name] ?? String(current);
            const changed = name in edits && Number(edits[name]) !== current;
            return (
              <div key={name} className={`rl-row ${changed ? "rl-row--changed" : ""}`}>
                <div className="rl-head">
                  <label className="rl-label" htmlFor={`rl-${name}`} title={k.help}>{k.label}</label>
                  {current !== k.default && (
                    <span className="rl-badge" title={`Default is ${k.default}${k.unit}`}>
                      changed from default
                    </span>
                  )}
                </div>
                <p className="rl-help">{k.help}</p>
                <div className="rl-input-row">
                  <input
                    id={`rl-${name}`}
                    className="rl-input"
                    type="number"
                    inputMode="decimal"
                    min={k.min}
                    max={k.max}
                    value={shown}
                    onChange={(e) => setEdits((p) => ({ ...p, [name]: e.target.value }))}
                  />
                  <span className="rl-unit">{k.unit}</span>
                  {k.unit === "s" && (
                    <span className="rl-human" title="What this value means in plain units">
                      = {humanise(Number(shown))}
                    </span>
                  )}
                  <span
                    className="rl-range"
                    title={`Allowed ${k.min}–${k.max}${k.unit}. Ships as ${k.default}${k.unit}.`}
                  >
                    {k.min}–{k.max}, default {k.default}
                    {k.unit === "s" ? ` (${humanise(k.default)})` : ""}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      ))}

      {error && <div className="settings-error">{error}</div>}
      <div className="rl-actions">
        <button className="btn btn-primary" disabled={!dirty || saving} onClick={save}>
          {saving ? "Saving…" : dirty ? `Save ${Object.keys(edits).length} change${Object.keys(edits).length > 1 ? "s" : ""}` : "Save changes"}
        </button>
        {saved && <span className="rl-saved">Saved — applies from the next turn or task.</span>}
      </div>
    </section>
  );
}
