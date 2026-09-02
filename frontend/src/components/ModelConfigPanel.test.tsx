import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelConfigPanel } from "./ModelConfigPanel";
import type { ModelPin } from "../types";

/* The model page commits through the same sticky bar as the settings page.
 * What matters: no bar until a pin is actually changed, changing a pin back
 * to what it was clears it, Save calls the API and lands the new pins, and
 * Discard drops the edit without a request. */

const api = vi.hoisted(() => ({
  getModelConfig: vi.fn(),
  getModelCatalog: vi.fn(),
  saveModelConfig: vi.fn(),
  saveProviderPins: vi.fn(),
  getModelEndpoints: vi.fn(),
  probeForcedToolCall: vi.fn(),
  restartLlmRouter: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, ...api };
});

const pin = (model: string): ModelPin => ({
  label: "Coder",
  model,
  input_cost_per_token: 1e-6,
  output_cost_per_token: 2e-6,
  tools: true,
});

const catalog = {
  models: [
    { id: "a/one", name: "One", input_cost_per_token: 1e-6, output_cost_per_token: 2e-6 },
    { id: "b/two", name: "Two", input_cost_per_token: 1e-6, output_cost_per_token: 2e-6 },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getModelConfig.mockResolvedValue({ roles: { "agent-coder": pin("a/one") } });
  api.getModelCatalog.mockResolvedValue(catalog);
  api.saveModelConfig.mockResolvedValue({ roles: { "agent-coder": pin("b/two") } });
});

async function mountAndPick(name: string) {
  render(<ModelConfigPanel />);
  const row = (await screen.findByText("agent-coder")).closest(".model-config-row") as HTMLElement;
  await userEvent.click(within(row).getByRole("button", { name: /One|Two/ }));
  await userEvent.click(within(row).getByRole("button", { name: new RegExp(`^${name}`) }));
  return row;
}

describe("model configuration save bar", () => {
  it("shows no save control until a pin changes", async () => {
    render(<ModelConfigPanel />);
    await screen.findByText("agent-coder");
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("counts a changed pin and saves it through the bar", async () => {
    await mountAndPick("Two");
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(api.saveModelConfig).toHaveBeenCalledWith({ "agent-coder": "b/two" }));
    expect(api.saveProviderPins).not.toHaveBeenCalled();
    await screen.findByText(/Saved to config\.yaml/);
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
  });

  it("clears the bar when a pin is put back to what it was", async () => {
    const row = await mountAndPick("Two");
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: /^Two/ }));
    await userEvent.click(within(row).getByRole("button", { name: /^One/ }));
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
  });

  it("discards an edit without a request", async () => {
    const row = await mountAndPick("Two");
    await userEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
    expect(api.saveModelConfig).not.toHaveBeenCalled();
    expect(within(row).getByRole("button", { name: /^One/ })).toBeInTheDocument();
  });

  it("surfaces a failed save in the bar and keeps the edit", async () => {
    api.saveModelConfig.mockRejectedValueOnce(new Error("config.yaml is read-only"));
    await mountAndPick("Two");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await screen.findByText("config.yaml is read-only");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeInTheDocument();
  });
});
