import { useEffect, useMemo, useState } from "react";
import { getRuntimeSettings, saveRuntimeSettings, type RuntimeKnob } from "../api";
import { useSettingsSave } from "./SettingsSaveBar";
import "./RuntimeLimitsPanel.css";

/* The dials that decide how hard the agent tries before it gives up.
 *
 * One card per group, dropped straight into the page's own auto-fit grid so
 * the cards tile three-up instead of stacking, and the section is as tall as
 * the tallest card rather than the sum of every knob.
 *
 * There is no Save button here: the panel reports its pending edits to the
 * page's single sticky bar (see SettingsSaveBar) and that commits them.
 *
 * Help text is a tooltip on the label and the input rather than a paragraph
 * per row -- fourteen inline explanations were a wall, and the label plus the
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
    const payload: Record<string, number> = {};
    for (const name of dirty) payload[name] = Number(edits[name]);
    // Errors deliberately propagate: the save bar owns reporting them, so a
    // failure is shown next to the button that caused it rather than in a
    // card the operator may have scrolled past.
    const res = await saveRuntimeSettings(payload);
    setValues(res.values);
    setEdits({});
  }

  useSettingsSave("runtime-limits", dirty.length, save, () => setEdits({}));

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
    </>
  );
}
