import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BalanceStrip } from "./BalanceStrip";

const getRouterBalance = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getRouterBalance: () => getRouterBalance() };
});

beforeEach(() => getRouterBalance.mockReset());

describe("BalanceStrip", () => {
  it("renders nothing for a non-admin and never calls the admin-only endpoint", () => {
    // Polling it as a normal user produced a silent 403 every 30 seconds.
    const { container } = render(<BalanceStrip isAdmin={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(getRouterBalance).not.toHaveBeenCalled();
  });

  it("shows the remaining credit", async () => {
    getRouterBalance.mockResolvedValue({ totalCredits: 215, totalUsage: 199.87, remaining: 15.13 });
    render(<BalanceStrip isAdmin />);
    expect(await screen.findByText("$15.13")).toBeInTheDocument();
    expect(screen.getByText(/\$199\.87 used of \$215/)).toBeInTheDocument();
  });

  it("flags a low balance below 15%", async () => {
    getRouterBalance.mockResolvedValue({ totalCredits: 215, totalUsage: 199.87, remaining: 15.13 });
    const { container } = render(<BalanceStrip isAdmin />);
    await screen.findByText("$15.13");
    expect(container.querySelector(".balance-strip-amount.low")).toBeTruthy();
  });

  it("does not flag a healthy balance", async () => {
    getRouterBalance.mockResolvedValue({ totalCredits: 215, totalUsage: 10, remaining: 205 });
    const { container } = render(<BalanceStrip isAdmin />);
    await screen.findByText("$205.00");
    expect(container.querySelector(".balance-strip-amount.low")).toBeNull();
  });

  it("survives a 200 carrying an unexpected body", async () => {
    // Regression: `!balance` passed for {}, then .toFixed threw. This renders
    // inside the Sidebar, so the throw blanked the entire console.
    getRouterBalance.mockResolvedValue({});
    const { container } = render(<BalanceStrip isAdmin />);
    await waitFor(() => expect(getRouterBalance).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("survives a failed request", async () => {
    getRouterBalance.mockRejectedValue(new Error("503"));
    const { container } = render(<BalanceStrip isAdmin />);
    await waitFor(() => expect(getRouterBalance).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("avoids dividing by zero when no credits are configured", async () => {
    getRouterBalance.mockResolvedValue({ totalCredits: 0, totalUsage: 0, remaining: 0 });
    render(<BalanceStrip isAdmin />);
    expect(await screen.findByText("$0.00")).toBeInTheDocument();
  });
});
