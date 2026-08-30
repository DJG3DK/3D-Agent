import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// RTL does not auto-clean when globals are enabled via config rather than
// imported, so unmount between tests. Without this, queries match nodes left
// behind by an earlier test and failures point at the wrong one.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// jsdom implements neither of these, and both are used by components under
// test (AutoGrowTextarea measures itself; JumpToBottom watches for the
// bottom sentinel). Without stubs the component throws on mount and the
// test failure describes the missing API rather than the behaviour.
class MockObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}
vi.stubGlobal("ResizeObserver", MockObserver);
vi.stubGlobal("IntersectionObserver", MockObserver);

// scrollIntoView / scrollTo are no-ops in jsdom but are called on mount by
// the streaming panes.
Element.prototype.scrollIntoView = vi.fn();
window.scrollTo = vi.fn();
