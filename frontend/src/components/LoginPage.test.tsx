import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "./LoginPage";
import { user } from "../test/fixtures";

const login = vi.fn();
const verify2FA = vi.fn();
const forgotPassword = vi.fn();
const resetPassword = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    login: (...a: unknown[]) => login(...a),
    verify2FA: (...a: unknown[]) => verify2FA(...a),
    forgotPassword: (...a: unknown[]) => forgotPassword(...a),
    resetPassword: (...a: unknown[]) => resetPassword(...a),
  };
});

function renderLogin(over: { onLoggedIn?: () => void; onBack?: () => void } = {}) {
  const onLoggedIn = over.onLoggedIn ?? vi.fn();
  const onBack = over.onBack ?? vi.fn();
  render(<LoginPage onLoggedIn={onLoggedIn} onBack={onBack} />);
  return { onLoggedIn, onBack };
}

const emailBox = () => screen.getByLabelText(/email/i);
const passwordBox = () => screen.getByLabelText(/^password$/i);

beforeEach(() => {
  login.mockReset();
  verify2FA.mockReset();
  forgotPassword.mockReset();
  resetPassword.mockReset();
});

describe("LoginPage — signing in", () => {
  it("keeps submit disabled until both fields have content", async () => {
    renderLogin();
    const submit = screen.getByRole("button", { name: /^sign in$/i });
    expect(submit).toBeDisabled();
    await userEvent.type(emailBox(), "a@b.c");
    expect(submit).toBeDisabled();
    await userEvent.type(passwordBox(), "pw");
    expect(submit).toBeEnabled();
  });

  it("signs in and hands the user up when 2FA is not required", async () => {
    const me = user();
    login.mockResolvedValue({ requires_2fa: false, user: me });
    const { onLoggedIn } = renderLogin();

    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "pw");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(login).toHaveBeenCalledWith("a@b.c", "pw");
    expect(onLoggedIn).toHaveBeenCalledWith(me);
  });

  it("trims the email before sending it", async () => {
    login.mockResolvedValue({ requires_2fa: false, user: user() });
    renderLogin();
    await userEvent.type(emailBox(), "  a@b.c  ");
    await userEvent.type(passwordBox(), "pw");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(login).toHaveBeenCalledWith("a@b.c", "pw");
  });

  it("submits on Enter", async () => {
    login.mockResolvedValue({ requires_2fa: false, user: user() });
    renderLogin();
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "pw{Enter}");
    expect(login).toHaveBeenCalled();
  });

  it("shows the server's message on failure and stays on the form", async () => {
    login.mockRejectedValue(new Error("bad credentials"));
    const { onLoggedIn } = renderLogin();
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "nope");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(await screen.findByText("bad credentials")).toBeInTheDocument();
    expect(onLoggedIn).not.toHaveBeenCalled();
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });
});

describe("LoginPage — two-factor", () => {
  it("moves to the code screen when the server asks for one", async () => {
    login.mockResolvedValue({ requires_2fa: true, temp_token: "tmp-1" });
    const { onLoggedIn } = renderLogin();
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "pw");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));

    expect(await screen.findByRole("heading", { name: /two-factor/i })).toBeInTheDocument();
    expect(onLoggedIn).not.toHaveBeenCalled(); // not signed in until the code verifies
  });

  it("verifies the code against the temp token from the first step", async () => {
    const me = user();
    login.mockResolvedValue({ requires_2fa: true, temp_token: "tmp-1" });
    verify2FA.mockResolvedValue({ user: me });
    const { onLoggedIn } = renderLogin();

    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "pw");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    await screen.findByRole("heading", { name: /two-factor/i });

    await userEvent.type(screen.getByLabelText(/code/i), "123456");
    await userEvent.click(screen.getByRole("button", { name: /verify/i }));

    expect(verify2FA).toHaveBeenCalledWith("tmp-1", "123456");
    expect(onLoggedIn).toHaveBeenCalledWith(me);
  });

  it("reports an invalid code without losing the screen", async () => {
    login.mockResolvedValue({ requires_2fa: true, temp_token: "tmp-1" });
    verify2FA.mockRejectedValue(new Error("invalid code"));
    renderLogin();
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.type(passwordBox(), "pw");
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    await screen.findByRole("heading", { name: /two-factor/i });

    await userEvent.type(screen.getByLabelText(/code/i), "000000");
    await userEvent.click(screen.getByRole("button", { name: /verify/i }));
    expect(await screen.findByText("invalid code")).toBeInTheDocument();
  });
});

describe("LoginPage — password reset", () => {
  it("walks forgot -> code entry -> done", async () => {
    forgotPassword.mockResolvedValue(undefined);
    resetPassword.mockResolvedValue(undefined);
    renderLogin();

    await userEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.click(screen.getByRole("button", { name: /send reset code/i }));

    expect(forgotPassword).toHaveBeenCalledWith("a@b.c");
    await screen.findByRole("heading", { name: /check your email/i });

    await userEvent.type(screen.getByLabelText(/reset code/i), "123456");
    await userEvent.type(screen.getByLabelText(/^new password$/i), "Passw0rdPassw0rd");
    await userEvent.type(screen.getByLabelText(/confirm new password/i), "Passw0rdPassw0rd");
    await userEvent.click(screen.getByRole("button", { name: /^reset password$/i }));

    expect(resetPassword).toHaveBeenCalledWith("a@b.c", "123456", "Passw0rdPassw0rd");
    expect(await screen.findByRole("heading", { name: /password reset/i })).toBeInTheDocument();
  });

  it("refuses mismatched passwords without calling the server", async () => {
    forgotPassword.mockResolvedValue(undefined);
    renderLogin();
    await userEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    await userEvent.type(emailBox(), "a@b.c");
    await userEvent.click(screen.getByRole("button", { name: /send reset code/i }));
    await screen.findByRole("heading", { name: /check your email/i });

    await userEvent.type(screen.getByLabelText(/reset code/i), "123456");
    await userEvent.type(screen.getByLabelText(/^new password$/i), "Passw0rdPassw0rd");
    await userEvent.type(screen.getByLabelText(/confirm new password/i), "different-one");
    await userEvent.click(screen.getByRole("button", { name: /^reset password$/i }));

    expect(await screen.findByText(/don't match/i)).toBeInTheDocument();
    expect(resetPassword).not.toHaveBeenCalled();
  });

  it("does not reveal whether the address has an account", async () => {
    // The copy is deliberately conditional ("If <email> has an account"), so
    // a reset request cannot be used to enumerate users.
    forgotPassword.mockResolvedValue(undefined);
    renderLogin();
    await userEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    await userEvent.type(emailBox(), "stranger@nowhere.test");
    await userEvent.click(screen.getByRole("button", { name: /send reset code/i }));
    expect(await screen.findByText(/if stranger@nowhere\.test has an account/i)).toBeInTheDocument();
  });
});

describe("LoginPage — navigation", () => {
  it("returns to sign in from a sub-screen", async () => {
    renderLogin();
    await userEvent.click(screen.getByRole("button", { name: /forgot password/i }));
    await screen.findByRole("heading", { name: /reset your password/i });
    await userEvent.click(screen.getByRole("button", { name: /back to sign in/i }));
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("offers a way back out to the landing page", async () => {
    const { onBack } = renderLogin();
    await userEvent.click(screen.getByRole("button", { name: /back to overview/i }));
    expect(onBack).toHaveBeenCalled();
  });

  it("carries no link to the source — that lives on the landing page now", () => {
    renderLogin();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
