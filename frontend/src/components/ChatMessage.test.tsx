import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChatMessage } from "./ChatMessage";
import type { LogEntry } from "../types";

describe("ChatMessage", () => {
  it("renders nothing, and does not throw, for an entry with no text", () => {
    // 2026-09-09: a heartbeat wrapped as a log entry reached the page with no
    // summary; classify() called startsWith on undefined and the error
    // boundary replaced the whole planning view.
    const ping = { type: "ping" } as unknown as LogEntry;
    const { container } = render(<ChatMessage entry={ping} />);
    expect(container.textContent).toBe("");
  });

  it("still renders an ordinary agent line", () => {
    const entry = { node: "planner", summary: "Reading the map", detail: "Reading the map", timestamp: new Date().toISOString() } as unknown as LogEntry;
    const { container } = render(<ChatMessage entry={entry} />);
    expect(container.textContent).toContain("Reading the map");
  });
});
