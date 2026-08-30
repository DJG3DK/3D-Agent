import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/* Whether the settings cards tile three-up is arithmetic, not taste, and it is
 * arithmetic that has been got wrong twice. `repeat(auto-fit, minmax(M, 1fr))`
 * fits N tracks only when the container is at least N*M + (N-1)*gap. The
 * runtime-limits grid shipped with M=420 inside a content column capped at
 * 1080px, needing 1296px it could never have, so it rendered two columns and a
 * permanently empty fourth cell -- and nothing failed, because the panel was
 * only ever eyeballed OUTSIDE the page that imposes the cap.
 *
 * Reading the numbers back out of the stylesheet is the cheapest thing that
 * would actually have caught it. jsdom does not do layout, so a rendering test
 * could not have.
 */

const css = readFileSync(join(__dirname, "SettingsPage.css"), "utf8");

/** The width the page's own padding leaves for content on a wide screen. */
function contentWidth(): number {
  const m = css.match(/calc\(\(100% - (\d+)px\) \/ 2\)/);
  if (!m) throw new Error(".settings-page no longer caps its content width");
  return Number(m[1]);
}

function rule(selector: string): string {
  const m = css.match(new RegExp(`\\${selector}\\s*\\{([^}]*)\\}`));
  if (!m) throw new Error(`no rule for ${selector}`);
  return m[1];
}

function trackMin(selector: string): number {
  const body = rule(selector);
  const m = body.match(/minmax\((\d+)px,\s*1fr\)/);
  if (!m) throw new Error(`${selector} is no longer an auto-fit minmax grid`);
  return Number(m[1]);
}

function gap(): number {
  const m = rule(".settings-grid").match(/gap:\s*(\d+)px/);
  if (!m) throw new Error(".settings-grid has no gap");
  return Number(m[1]);
}

/** Widest container that auto-fit will still split into `n` tracks. */
function needed(n: number, min: number, g: number): number {
  return n * min + (n - 1) * g;
}

describe("settings page column arithmetic", () => {
  it("fits three runtime-limit cards in one row", () => {
    // The whole point of the wide grid: three groups, one row, no dead cell.
    expect(needed(3, trackMin(".settings-grid--wide"), gap())).toBeLessThanOrEqual(contentWidth());
  });

  it("fits two of the ordinary cards in one row", () => {
    expect(needed(2, trackMin(".settings-grid"), gap())).toBeLessThanOrEqual(contentWidth());
  });

  it("collapses both grids to one column before the track minimum overflows", () => {
    // A track minimum is a hard floor: below it the grid overflows the page
    // sideways rather than shrinking. So the breakpoint has to be wide enough
    // to catch that -- and narrow enough not to hide a second column that
    // would have fitted on its own.
    const m = css.match(/@media \(max-width: (\d+)px\)\s*\{\s*\.settings-grid,\s*\.settings-grid--wide/);
    expect(m, "no shared single-column breakpoint").toBeTruthy();
    const breakpoint = Number(m![1]);
    const widest = Math.max(trackMin(".settings-grid"), trackMin(".settings-grid--wide"));
    expect(breakpoint).toBeGreaterThanOrEqual(widest);
    expect(breakpoint).toBeLessThan(needed(2, widest, gap()));
  });
});
