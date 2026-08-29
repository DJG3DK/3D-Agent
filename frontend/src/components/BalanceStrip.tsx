import { useEffect, useState } from "react";
import { getRouterBalance } from "../api";
import type { RouterBalance } from "../types";
import "./BalanceStrip.css";

export function BalanceStrip() {
  const [balance, setBalance] = useState<RouterBalance | null>(null);

  useEffect(() => {
    const load = () => getRouterBalance().then(setBalance).catch(() => {});
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, []);

  if (!balance) return null;
  const pctUsed = balance.totalCredits > 0 ? (balance.totalUsage / balance.totalCredits) * 100 : 0;
  const low = balance.remaining < balance.totalCredits * 0.15;

  return (
    <div className="balance-strip">
      <div className="balance-strip-row">
        <span className="balance-strip-label">API Balance</span>
        <span className={`balance-strip-amount ${low ? "low" : ""}`}>${balance.remaining.toFixed(2)}</span>
      </div>
      <div className="balance-strip-bar">
        <div
          className={`balance-strip-fill ${low ? "low" : ""}`}
          style={{ width: `${Math.min(100, pctUsed)}%` }}
        />
      </div>
      <div className="balance-strip-sub">
        ${balance.totalUsage.toFixed(2)} used of ${balance.totalCredits.toFixed(0)}
      </div>
    </div>
  );
}
