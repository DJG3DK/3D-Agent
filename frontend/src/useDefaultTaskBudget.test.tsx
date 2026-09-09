import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { _resetDefaultTaskBudgetCache, useBudgetInput, useDefaultTaskBudget } from "./useDefaultTaskBudget";

const getRuntimeSettings = vi.fn();
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, getRuntimeSettings: (...a: unknown[]) => getRuntimeSettings(...a) };
});

function Probe() {
  const v = useDefaultTaskBudget();
  return <span data-testid="v">{v}</span>;
}

function Input() {
  const [budget, setBudget] = useBudgetInput();
  return <input data-testid="b" type="number" value={budget} onChange={(e) => setBudget(parseFloat(e.target.value) || 0)} />;
}

describe("default task budget", () => {
  beforeEach(() => { _resetDefaultTaskBudgetCache(); getRuntimeSettings.mockReset(); });

  it("shows the configured default once settings load", async () => {
    getRuntimeSettings.mockResolvedValue({ knobs: {}, values: { default_task_budget_usd: 10 } });
    render(<Probe />);
    await waitFor(() => expect(screen.getByTestId("v").textContent).toBe("10"));
  });

  it("falls back to $2 when settings cannot be read", async () => {
    getRuntimeSettings.mockRejectedValue(new Error("403"));
    render(<Probe />);
    await waitFor(() => expect(getRuntimeSettings).toHaveBeenCalled());
    expect(screen.getByTestId("v").textContent).toBe("2");
  });

  it("a value the user typed is not overwritten when the default arrives", async () => {
    let resolve!: (v: unknown) => void;
    getRuntimeSettings.mockReturnValue(new Promise((r) => { resolve = r; }));
    render(<Input />);
    const input = screen.getByTestId("b") as HTMLInputElement;
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(input, { target: { value: "7.5" } });
    resolve({ knobs: {}, values: { default_task_budget_usd: 10 } });
    await waitFor(() => expect(getRuntimeSettings).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(input.value).toBe("7.5");
  });
});
