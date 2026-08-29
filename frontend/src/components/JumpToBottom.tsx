import { useCallback, useEffect, useState } from "react";
import "./JumpToBottom.css";

/** Floating "jump to newest" arrow for a scrolling log. Appears once the
 * viewer has scrolled meaningfully away from the bottom, disappears when the
 * log is (near) caught up. Purely presentational — attach it inside any
 * relatively-positioned wrapper around the scroll container. */
export function JumpToBottom({ containerRef }: { containerRef: React.RefObject<HTMLDivElement | null> }) {
  const [show, setShow] = useState(false);

  useEffect(() => {
    const c = containerRef.current;
    if (!c) return;
    const onScroll = () => {
      setShow(c.scrollHeight - c.scrollTop - c.clientHeight > 300);
    };
    onScroll();
    c.addEventListener("scroll", onScroll, { passive: true });
    return () => c.removeEventListener("scroll", onScroll);
  }, [containerRef]);

  const jump = useCallback(() => {
    containerRef.current?.scrollTo({ top: containerRef.current.scrollHeight, behavior: "smooth" });
  }, [containerRef]);

  if (!show) return null;
  return (
    <button className="jump-to-bottom" title="Jump to newest" onClick={jump}>
      ↓
    </button>
  );
}
