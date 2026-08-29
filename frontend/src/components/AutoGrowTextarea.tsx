import { useLayoutEffect, useRef } from "react";

/**
 * A textarea that grows with its content instead of scrolling a fixed box,
 * with the browser's native writing aids (spellcheck / autocorrect /
 * sentence autocapitalisation) turned on.
 *
 * Every multi-line input in this app is somewhere a human types prose for an
 * agent to act on -- a nudge, an answer to a question, a rejection reason --
 * and each one was previously either a single-line `<input>` (no wrapping at
 * all: a long message scrolled sideways through a one-line slot, so you
 * could never see what you'd written) or a `rows={N}` textarea frozen at
 * two or three lines regardless of how much text it held.
 *
 * Height is driven off `scrollHeight` rather than counting `\n`s, so it's
 * correct for soft-wrapped long lines too, not just explicit newlines. It
 * recomputes on every value change -- including programmatic resets to ""
 * after a send, which is what shrinks it back down. Past `maxHeight` it
 * stops growing and scrolls, so one very long message can't push the send
 * button off-screen.
 */
interface Props {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Height floor, in CSS px, that the box never shrinks below. */
  minHeight?: number;
  /** Height ceiling; past this the textarea scrolls internally. */
  maxHeight?: number;
  className?: string;
  /**
   * Enter submits and Shift+Enter inserts a newline -- the chat convention,
   * and what these boxes did when they were single-line `<input>`s, so
   * muscle memory carries over. Omit for a plain multi-line box where
   * Enter should just be a newline (a long-form answer, say).
   */
  onSubmit?: () => void;
  ariaLabel?: string;
}

export function AutoGrowTextarea({
  value,
  onChange,
  placeholder,
  disabled,
  minHeight = 42,
  maxHeight = 220,
  className,
  onSubmit,
  ariaLabel,
}: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // useLayoutEffect, not useEffect: this runs before the browser paints, so
  // the box never renders one frame at the wrong height (a visible flicker
  // on every keystroke that useEffect would produce).
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Reset first -- scrollHeight only ever reports content height when the
    // element isn't already stretched to fit it, so without this the box
    // could grow but never shrink back.
    el.style.height = "auto";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, minHeight), maxHeight)}px`;
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [value, minHeight, maxHeight]);

  return (
    <textarea
      ref={ref}
      className={className}
      placeholder={placeholder}
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      rows={1}
      spellCheck
      autoCorrect="on"
      autoCapitalize="sentences"
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (onSubmit && e.key === "Enter" && !e.shiftKey) {
          e.preventDefault(); // otherwise the newline lands before the reset
          onSubmit();
        }
      }}
    />
  );
}
