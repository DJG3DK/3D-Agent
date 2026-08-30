import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RuntimeLimitsPanel } from "./RuntimeLimitsPanel";
import { SettingsSaveBar, SettingsSaveProvider } from "./SettingsSaveBar";
import type { RuntimeKnob } from "../api";

const getRuntimeSettings = vi.fn();
const saveRuntimeSettings = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getRuntimeSettings: () => getRuntimeSettings(),
    saveRuntimeSettings: (...a: unknown[]) => saveRuntimeSettings(...a),
  };
});

const knob = (over: Partial<RuntimeKnob> = {}): RuntimeKnob => ({
  label: "Planning stall timeout",
  help: "how long a turn may produce no output",
  unit: "s",
  default: 1200,
  min: 120,
  max: 7200,
  group: "Budgets & loop limits",
  ...over,
});

const KNOBS: Record<string, RuntimeKnob> = {
  planning_stall_timeout_s: knob(),
  model_call_timeout_s: knob({
    label: "Model call timeout",
    default: 180,
    group: "Model & sandbox timeouts",
  }),
};

function mount() {
  return render(
    <SettingsSaveProvider>
      <RuntimeLimitsPanel />
      <SettingsSaveBar />
    </SettingsSaveProvider>,
  );
}

beforeEach(() => {
  getRuntimeSettings.mockReset();
  saveRuntimeSettings.mockReset();
  getRuntimeSettings.mockResolvedValue({
    knobs: KNOBS,
    values: { planning_stall_timeout_s: 1200, model_call_timeout_s: 180 },
  });
});

describe("RuntimeLimitsPanel", () => {
  it("puts each knob in the card for its group", async () => {
    mount();
    expect(await screen.findByText("Budgets & loop limits")).toBeInTheDocument();
    expect(screen.getByText("Model & sandbox timeouts")).toBeInTheDocument();
  });

  it("says what a raw number of seconds actually means", async () => {
    // 1200 is not a legible timeout; "20 min" is the whole reason the operator
    // can tell at a glance whether the value is sane.
    mount();
    expect(await screen.findByText("20 min")).toBeInTheDocument();
    expect(screen.getByText("3 min")).toBeInTheDocument();
  });

  it("stays clean until a value genuinely differs", async () => {
    mount();
    const input = (await screen.findByLabelText(/Planning stall timeout/)) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "1200");
    // Typed straight back to what it was: nothing to save, so nothing to nag about.
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("sends only the knobs that changed", async () => {
    saveRuntimeSettings.mockResolvedValue({
      values: { planning_stall_timeout_s: 1800, model_call_timeout_s: 180 },
    });
    mount();
    const input = (await screen.findByLabelText(/Planning stall timeout/)) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "1800");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(saveRuntimeSettings).toHaveBeenCalled());
    expect(saveRuntimeSettings.mock.calls[0][0]).toEqual({ planning_stall_timeout_s: 1800 });
  });

  it("shows a rejected save instead of pretending it landed", async () => {
    saveRuntimeSettings.mockRejectedValue(new Error("unknown setting"));
    mount();
    const input = (await screen.findByLabelText(/Planning stall timeout/)) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "1800");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    expect(await screen.findByText(/unknown setting/)).toBeInTheDocument();
  });

  it("does not take down the settings page when the endpoint is unreachable", async () => {
    getRuntimeSettings.mockRejectedValue(new Error("503"));
    mount();
    expect(await screen.findByText(/503/)).toBeInTheDocument();
  });
});
