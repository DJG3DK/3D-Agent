import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiffPanel } from "./DiffPanel";
import { LandingPage } from "./LandingPage";

const getTaskDiff = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getTaskDiff: (...a: unknown[]) => getTaskDiff(...a),
    decideMerge: vi.fn(async () => {}),
  };
});

beforeEach(() => getTaskDiff.mockReset());

describe("DiffPanel", () => {
  const props = () => ({
    taskId: "t1",
    open: true,
    onClose: vi.fn(),
    live: false,
    awaitingMerge: false,
    onDecided: vi.fn(),
  });

  it("fetches nothing while closed", () => {
    render(<DiffPanel {...props()} open={false} />);
    expect(getTaskDiff).not.toHaveBeenCalled();
  });

  it("reports how many files changed, pluralised", async () => {
    getTaskDiff.mockResolvedValue({ base: "abc1234567def", files: [{ path: "a.ts", additions: 1, deletions: 0, patch: "+x" }] });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText("1 file")).toBeInTheDocument();
  });

  it("pluralises correctly for more than one", async () => {
    getTaskDiff.mockResolvedValue({
      base: "abc1234567def",
      files: [
        { path: "a.ts", additions: 1, deletions: 0, patch: "+x" },
        { path: "b.ts", additions: 2, deletions: 1, patch: "+y" },
      ],
    });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText("2 files")).toBeInTheDocument();
  });

  it("says so explicitly when a task produced no diff", async () => {
    // An empty panel would read as "still loading" forever.
    getTaskDiff.mockResolvedValue({ files: [], base: "abc1234567def" });
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText(/no changes against abc1234567/i)).toBeInTheDocument();
  });

  it("surfaces a load failure rather than showing a blank panel", async () => {
    getTaskDiff.mockRejectedValue(new Error("diff unavailable"));
    render(<DiffPanel {...props()} />);
    expect(await screen.findByText(/diff unavailable/i)).toBeInTheDocument();
  });

  it("closes on request", async () => {
    getTaskDiff.mockResolvedValue({ files: [], base: "abc1234567def" });
    const p = props();
    render(<DiffPanel {...p} />);
    await userEvent.click(await screen.findByRole("button", { name: /close/i }));
    expect(p.onClose).toHaveBeenCalled();
  });
});

describe("LandingPage", () => {
  it("leads with what the product does", () => {
    render(<LandingPage onSignIn={vi.fn()} />);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/autonomous coding agent/i);
  });

  it("puts sign-in in the nav", async () => {
    const onSignIn = vi.fn();
    render(<LandingPage onSignIn={onSignIn} />);
    await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
    expect(onSignIn).toHaveBeenCalled();
  });

  it("has exactly one sign-in control", () => {
    // The closing section used to carry a second one, splitting the call to
    // action between signing in and reading the source.
    render(<LandingPage onSignIn={vi.fn()} />);
    const signIns = screen.getAllByRole("button").filter((b) => /sign in/i.test(b.textContent ?? ""));
    expect(signIns).toHaveLength(1);
  });

  it("links to the source, and only ever to that repo", () => {
    render(<LandingPage onSignIn={vi.fn()} />);
    const links = screen.getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("http"));
    expect(links.length).toBeGreaterThan(0);
    links.forEach((a) =>
      expect(a.getAttribute("href")).toBe("https://github.com/DJG3DK/3D-Agent"),
    );
  });

  it("opens external links safely", () => {
    // target=_blank without rel=noopener hands the opened page a reference
    // back to this one.
    render(<LandingPage onSignIn={vi.fn()} />);
    screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("target") === "_blank")
      .forEach((a) => expect(a.getAttribute("rel")).toMatch(/noopener/));
  });

  it("describes the licence as source-available, never as open source", () => {
    // PolyForm Noncommercial is not an OSI licence, and the README says so.
    render(<LandingPage onSignIn={vi.fn()} />);
    expect(screen.getByText(/source available under polyform noncommercial/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/open source/i);
  });

  it("marks the review gate as the stage that can send work back", () => {
    const { container } = render(<LandingPage onSignIn={vi.fn()} />);
    expect(container.querySelector(".lp-pipeline .is-gate")).toBeTruthy();
  });

  it("gives every screenshot alt text", () => {
    render(<LandingPage onSignIn={vi.fn()} />);
    screen.getAllByRole("img").forEach((img) => {
      expect(img.getAttribute("alt")).toBeTruthy();
    });
  });

  it("lazy-loads the images below the fold", () => {
    // Eight screenshots eagerly loaded would compete with the hero.
    const { container } = render(<LandingPage onSignIn={vi.fn()} />);
    const lazy = container.querySelectorAll('img[loading="lazy"]');
    expect(lazy.length).toBeGreaterThan(0);
  });
});
