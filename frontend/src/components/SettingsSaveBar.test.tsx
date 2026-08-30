import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SettingsSaveBar, SettingsSaveProvider, useSettingsSave } from "./SettingsSaveBar";

/* The bar is the ONLY way to commit a settings change, so the properties that
 * matter are: it stays out of the way until there is something to save, it
 * counts across every panel rather than per card, a failure is visible instead
 * of being swallowed, and discard actually restores the panels. */

function Panel({
  id,
  onSave,
}: {
  id: string;
  onSave: (n: number) => Promise<void> | void;
}) {
  const [count, setCount] = useState(0);
  useSettingsSave(id, count, async () => {
    await onSave(count);
    setCount(0);
  }, () => setCount(0));
  return (
    <div>
      <span data-testid={`count-${id}`}>{count}</span>
      <button onClick={() => setCount((c) => c + 1)}>edit {id}</button>
    </div>
  );
}

function mount(panels: { id: string; onSave: (n: number) => Promise<void> | void }[]) {
  return render(
    <SettingsSaveProvider>
      {panels.map((p) => (
        <Panel key={p.id} {...p} />
      ))}
      <SettingsSaveBar />
    </SettingsSaveProvider>,
  );
}

describe("the settings page's single save bar", () => {
  it("stays hidden while nothing is dirty", () => {
    mount([{ id: "a", onSave: vi.fn() }]);
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("appears as soon as a panel reports an edit", async () => {
    mount([{ id: "a", onSave: vi.fn() }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
  });

  it("counts changes across every panel, not per card", async () => {
    // The reason there is one bar at all: an edit in a card you scrolled past
    // must still be visible from wherever you are on the page.
    mount([{ id: "a", onSave: vi.fn() }, { id: "b", onSave: vi.fn() }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: "edit b" }));
    await userEvent.click(screen.getByRole("button", { name: "edit b" }));
    expect(screen.getByText("3 unsaved changes")).toBeInTheDocument();
  });

  it("commits every dirty panel and only the dirty ones", async () => {
    const a = vi.fn();
    const b = vi.fn();
    mount([{ id: "a", onSave: a }, { id: "b", onSave: b }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(a).toHaveBeenCalledWith(1));
    expect(b).not.toHaveBeenCalled();
  });

  it("goes away once saved", async () => {
    mount([{ id: "a", onSave: vi.fn() }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText("Saved.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("reports a failure rather than implying the change landed", async () => {
    mount([{ id: "a", onSave: () => { throw new Error("store is down"); } }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText(/store is down/)).toBeInTheDocument();
    // Still dirty, so the operator can retry rather than losing the edit.
    expect(screen.getByTestId("count-a").textContent).toBe("1");
  });

  it("discards every panel's edits at once", async () => {
    mount([{ id: "a", onSave: vi.fn() }, { id: "b", onSave: vi.fn() }]);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: "edit b" }));
    await userEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(screen.getByTestId("count-a").textContent).toBe("0");
    expect(screen.getByTestId("count-b").textContent).toBe("0");
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("stops counting a panel that unmounts", async () => {
    // Runtime limits are admin-only; a non-admin unmounting it must not leave
    // a phantom pending change pinned to the corner of the screen.
    function Host() {
      const [show, setShow] = useState(true);
      return (
        <SettingsSaveProvider>
          {show && <Panel id="a" onSave={vi.fn()} />}
          <button onClick={() => setShow(false)}>hide</button>
          <SettingsSaveBar />
        </SettingsSaveProvider>
      );
    }
    render(<Host />);
    await userEvent.click(screen.getByRole("button", { name: "edit a" }));
    await userEvent.click(screen.getByRole("button", { name: "hide" }));
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });
});
