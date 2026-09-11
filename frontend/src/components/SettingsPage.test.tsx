import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "./SettingsPage";
import type { CurrentUser } from "../types";

const setAutoApprove = vi.fn();
const listProjectsConfig = vi.fn();
const getTelegramSettings = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    setAutoApprove: (...a: unknown[]) => setAutoApprove(...a),
    listProjectsConfig: () => listProjectsConfig(),
    getTelegramSettings: () => getTelegramSettings(),
  };
});

function user(over: Partial<CurrentUser> = {}): CurrentUser {
  return {
    id: 1, email: "dev@example.com", role: "user", allowed_repos: ["sandbox", "production"],
    totp_enabled: true, must_change_password: false, require_totp_setup: false,
    auto_approve_commands: false, auto_approve_repos: [], require_merge_review: true,
    ...over,
  };
}

describe("auto mode is chosen per project", () => {
  beforeEach(() => {
    setAutoApprove.mockReset();
    setAutoApprove.mockResolvedValue({ auto_approve_repos: ["sandbox"] });
    listProjectsConfig.mockReset();
    listProjectsConfig.mockResolvedValue({ projects: { sandbox: {}, production: {} } });
    getTelegramSettings.mockReset();
    getTelegramSettings.mockResolvedValue({ configured: false, chat_id: "", bot_token_hint: "" });
  });

  it("will not turn on until a project is ticked", async () => {
    render(<SettingsPage user={user()} onUserChanged={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: /Turn auto mode on/ }));
    await waitFor(() => expect(screen.getByLabelText("sandbox")).toBeTruthy());

    const confirm = screen.getByRole("button", { name: /Turn it on for no projects/ });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(setAutoApprove).not.toHaveBeenCalled();
  });

  it("sends exactly the projects that were ticked", async () => {
    const onUserChanged = vi.fn();
    render(<SettingsPage user={user()} onUserChanged={onUserChanged} />);
    await userEvent.click(screen.getByRole("button", { name: /Turn auto mode on/ }));
    await waitFor(() => expect(screen.getByLabelText("sandbox")).toBeTruthy());

    await userEvent.click(screen.getByLabelText("sandbox"));
    await userEvent.click(screen.getByRole("button", { name: /Turn it on for 1 project$/ }));

    expect(setAutoApprove).toHaveBeenCalledWith(true, ["sandbox"]);
    expect(onUserChanged).toHaveBeenCalledWith(
      expect.objectContaining({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] }),
    );
  });

  it("names the projects it covers once it is on", async () => {
    render(<SettingsPage user={user({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] })}
                         onUserChanged={() => {}} />);
    const covers = await screen.findByText(/Covers:/);
    expect(covers.parentElement?.textContent).toMatch(/Covers:\s*sandbox/);
  });

  it("says plainly when it is on but covers nothing", async () => {
    render(<SettingsPage user={user({ auto_approve_commands: true, auto_approve_repos: [] })}
                         onUserChanged={() => {}} />);
    expect(await screen.findByText(/auto mode is on but applies nowhere/i)).toBeTruthy();
  });

  it("turning it off does not resend the project list", async () => {
    render(<SettingsPage user={user({ auto_approve_commands: true, auto_approve_repos: ["sandbox"] })}
                         onUserChanged={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: /Turn auto mode off/ }));
    expect(setAutoApprove).toHaveBeenCalledWith(false, undefined);
  });
});
