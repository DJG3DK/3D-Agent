import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/* The settings page's layout invariants, read out of the stylesheet.
 *
 * This file used to check card-grid arithmetic: `repeat(auto-fit, minmax(M,
 * 1fr))` fits N tracks only when the container is at least N*M + (N-1)*gap,
 * and that had been got wrong twice. The grid is gone -- the page shows one
 * section at a time in a single column, which is what removed the whole class
 * of bug (eleven cards at three different widths, two-up rows whose heights
 * never matched, 4300px of scroll with no way to see what was in it).
 *
 * What can still silently break is the two-pane frame: a rail and a content
 * pane that between them must not exceed the window, and a breakpoint where
 * the rail stops being a column. jsdom does not do layout, so reading the
 * numbers back out of the CSS remains the cheapest check that would catch it.
 */

// Comments stripped first: a /* ... */ between two declarations otherwise
// sits where the matcher expects a semicolon, and the property reads as absent.
const css = readFileSync(join(__dirname, "SettingsPage.css"), "utf8")
  .replace(/\/\*[\s\S]*?\*\//g, "");

/** Escape a CSS selector for use inside a RegExp -- `>` and `*` are literal
 *  here, not operators. */
function esc(selector: string): string {
  return selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function decl(selector: string, prop: string): string {
  const rule = css.match(new RegExp(`${esc(selector)}\\s*\\{([^}]*)\\}`));
  if (!rule) throw new Error(`no rule for ${selector}`);
  const m = rule[1].match(new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`));
  if (!m) throw new Error(`${selector} has no ${prop}`);
  return m[1].trim();
}

function px(value: string): number {
  const m = value.match(/(\d+)px/);
  if (!m) throw new Error(`not a px value: ${value}`);
  return Number(m[1]);
}

describe("settings page frame", () => {
  it("is a two-pane grid: a fixed rail and a flexible content pane", () => {
    const cols = decl(".settings-page", "grid-template-columns");
    expect(cols).toContain("var(--settings-rail)");
    // minmax(0, 1fr), not 1fr: a bare 1fr floors at min-content, so one wide
    // table inside the pane would push the whole page sideways instead of
    // scrolling within its own container.
    expect(cols).toContain("minmax(0, 1fr)");
  });

  it("leaves the reading column narrower than the rail plus itself", () => {
    // The point of the cap is prose that does not run to 1400px. It only
    // works if the rail and the column together still fit a laptop.
    const rail = px(decl(":root", "--settings-rail"));
    const measure = px(decl(":root", "--settings-measure"));
    expect(rail + measure).toBeLessThanOrEqual(1280);
  });

  it("collapses to one column before the two panes get too narrow to use", () => {
    const m = css.match(/@media \(max-width: (\d+)px\)\s*\{\s*\.settings-page/);
    expect(m, "no single-column breakpoint for the settings frame").toBeTruthy();
    const breakpoint = Number(m![1]);
    const rail = px(decl(":root", "--settings-rail"));
    // Below the breakpoint the rail becomes a strip above the content. It has
    // to fire while the content pane is still wider than the rail beside it --
    // at 600px a 232px rail left 368px of content, which is what it looked
    // like before this existed.
    expect(breakpoint).toBeGreaterThan(rail * 2);
    expect(breakpoint).toBeLessThan(1024);
  });

  it("scrolls the content pane, not the whole page, on wide screens", () => {
    // A full-width scroller puts its scrollbar at the right edge of the
    // centred column rather than the window, and the wheel does nothing over
    // the margins either side.
    expect(decl(".settings-content", "overflow-y")).toBe("auto");
    expect(decl(".settings-page", "overflow")).toBe("hidden");
  });

  it("stacks sections in one column rather than tiling them", () => {
    expect(decl(".settings-stack", "flex-direction")).toBe("column");
  });

  it("gives dense sections more width than the reading measure", () => {
    // The GitHub per-project table needs the room; capping it at the reading
    // width hid its Budget column behind a scrollbar while 300px of screen
    // sat empty beside it.
    const measure = px(decl(":root", "--settings-measure"));
    const wide = px(decl(".settings-content--wide > *", "max-width"));
    expect(wide).toBeGreaterThan(measure);
  });
});
