import { useEffect, useState } from "react";
import { getRouterBalance } from "../api";
import type { RouterBalance } from "../types";
import "./BalanceStrip.css";

/* Router credit balance. `isAdmin` is required because the endpoint behind it
   is admin-only: polling it as a normal user produced a silent 403 every 30
   seconds forever, filling the network log with failures that looked like an
   outage. Non-admins render nothing. */
export function BalanceStrip({ isAdmin }: { isAdmin: boolean }) {
  const [balance, setBalance] = useState<RouterBalance | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    const load = () => getRouterBalance().then(setBalance).catch(() => {});
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [isAdmin]);

  if (!isAdmin) return null;

  // Presence is not enough. `.catch(() => {})` above swallows a failed
  // request, but a 200 carrying an unexpected body (a proxy error page, a
  // changed field name) passes a bare `!balance` check and then throws on
  // .toFixed below -- and this renders inside the Sidebar, so the
  // ErrorBoundary turns that into a blank console for what is only a
  // decorative strip. Validate the shape, and render nothing if it is wrong.
  const usable =
    balance !== null &&
    [balance.totalCredits, balance.totalUsage, balance.remaining].every(
      (n) => typeof n === "number" && Number.isFinite(n),
    );
  if (!usable) return null;
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
