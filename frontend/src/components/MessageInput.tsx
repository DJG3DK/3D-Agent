import { useState } from "react";
import { sendTaskMessage } from "../api";
import { AutoGrowTextarea } from "./AutoGrowTextarea";
import "./MessageInput.css";

interface Props {
  taskId: string;
  running: boolean;
}

export function MessageInput({ taskId, running }: Props) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  async function handleSend() {
    if (!text.trim() || sending) return;
    setSending(true);
    try {
      await sendTaskMessage(taskId, text.trim());
      setText("");
    } catch {
      // Best-effort — the input just keeps whatever was typed so the user can retry.
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="message-input">
      <AutoGrowTextarea
        ariaLabel="Message the agent"
        minHeight={84}
        maxHeight={260}
        placeholder={running ? "Nudge the agent — picked up at the start of its next turn... (Shift+Enter for a new line)" : "Task isn't running — nothing to nudge"}
        value={text}
        disabled={!running}
        onChange={setText}
        onSubmit={handleSend}
      />
      <button onClick={handleSend} disabled={!running || !text.trim() || sending}>
        {sending ? "…" : "➤"}
      </button>
    </div>
  );
}
