import { useMemo, useState } from "react";
import { fetchShipments, sendChat } from "./api/client.js";

const DEMO_DRIVERS = [
  { id: "DRV-027", label: "Ravi Kumar (tyre / Neemrana)" },
  { id: "DRV-EVE-00", label: "Evening Driver 00 (contention)" },
  { id: "DRV-MULTI", label: "Karan Singh (multi-shipment)" },
  { id: "DRV-NOP", label: "No-slot driver (escalate)" },
  { id: "DRV-HIPRI", label: "Priority late entrant" },
];

function lifecycleClass(lifecycle) {
  return `life life-${lifecycle || "shown"}`;
}

export default function App() {
  const [driverId, setDriverId] = useState("DRV-027");
  const [shipmentId, setShipmentId] = useState("SHP-1042");
  const [exceptionId, setExceptionId] = useState(null);
  const [shipments, setShipments] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [messages, setMessages] = useState([
    {
      role: "system",
      text: "SetuHaul exception chat. Capacity comes only from the allocation engine — shown ≠ held ≠ confirmed.",
    },
  ]);
  const [options, setOptions] = useState([]);
  const [status, setStatus] = useState("idle");
  const [hold, setHold] = useState(null);
  const [tools, setTools] = useState([]);

  const statusLabel = useMemo(() => status, [status]);

  async function loadShipments(id = driverId) {
    try {
      const rows = await fetchShipments(id);
      setShipments(rows);
      if (rows.length === 1) setShipmentId(rows[0].shipment_id);
    } catch (err) {
      setError(err.message);
    }
  }

  async function onSend(text) {
    const message = (text ?? input).trim();
    if (!message || busy) return;
    setBusy(true);
    setError("");
    setMessages((m) => [...m, { role: "driver", text: message }]);
    setInput("");
    try {
      const idempotencyKey = `${driverId}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const res = await sendChat({
        driverId,
        message,
        exceptionId,
        shipmentId: shipmentId || null,
        idempotencyKey,
      });
      setExceptionId(res.exception_id || exceptionId);
      if (res.shipment_id) setShipmentId(res.shipment_id);
      setStatus(res.status);
      setOptions(res.options || []);
      setHold(res.hold || null);
      setTools(res.tools_used || []);
      setMessages((m) => [...m, { role: "agent", text: res.reply, escalated: res.escalated }]);
      if (res.needs_shipment_choice?.length) {
        setShipments(res.needs_shipment_choice);
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="top">
        <div>
          <p className="brand">SetuHaul</p>
          <h1>Driver exception desk</h1>
          <p className="sub">
            Conversational coordination over deterministic dock allocation. Phase 1 MVP.
          </p>
        </div>
        <div className="meta">
          <span className="pill">status: {statusLabel}</span>
          {hold ? <span className="pill hold">hold: {hold.slot_id}</span> : null}
          {exceptionId ? <span className="pill">exc: {exceptionId}</span> : null}
        </div>
      </header>

      <section className="controls">
        <label>
          Driver
          <select
            value={driverId}
            onChange={(e) => {
              setDriverId(e.target.value);
              setExceptionId(null);
              setOptions([]);
              setHold(null);
              setStatus("idle");
              const next = e.target.value;
              if (next === "DRV-027") setShipmentId("SHP-1042");
              else if (next === "DRV-NOP") setShipmentId("SHP-NOP");
              else if (next === "DRV-HIPRI") setShipmentId("SHP-HIPRI");
              else if (next === "DRV-MULTI") setShipmentId("");
              else if (next.startsWith("DRV-EVE")) setShipmentId(next.replace("DRV", "SHP"));
            }}
          >
            {DEMO_DRIVERS.map((d) => (
              <option key={d.id} value={d.id}>
                {d.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Shipment
          <input
            value={shipmentId}
            onChange={(e) => setShipmentId(e.target.value)}
            placeholder="SHP-..."
          />
        </label>
        <button type="button" className="ghost" onClick={() => loadShipments()}>
          Load shipments
        </button>
      </section>

      {shipments.length > 1 ? (
        <div className="choice-row">
          {shipments.map((s) => (
            <button
              key={s.shipment_id}
              type="button"
              onClick={() => {
                setShipmentId(s.shipment_id);
                onSend(s.shipment_id);
              }}
            >
              {s.shipment_id} → {s.destination_id}
            </button>
          ))}
        </div>
      ) : null}

      <main className="layout">
        <div className="chat">
          <div className="transcript">
            {messages.map((m, idx) => (
              <div key={idx} className={`bubble ${m.role} ${m.escalated ? "escalated" : ""}`}>
                <span className="who">{m.role}</span>
                <pre>{m.text}</pre>
              </div>
            ))}
          </div>
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              onSend();
            }}
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder='e.g. "Tyre damaged near Neemrana. Slot after 7 PM?"'
              disabled={busy}
            />
            <button type="submit" disabled={busy || !input.trim()}>
              Send
            </button>
          </form>
          <div className="quick">
            {[
              "Tyre damaged near Neemrana. Repair may take 45 minutes. Can I get a slot after 7 PM?",
              "What are the next slots after 7 PM?",
              "1",
              "confirm",
              "Has it been confirmed?",
              "cancel",
            ].map((q) => (
              <button key={q} type="button" onClick={() => onSend(q)} disabled={busy}>
                {q.length > 42 ? `${q.slice(0, 42)}…` : q}
              </button>
            ))}
          </div>
          {error ? <p className="error">{error}</p> : null}
        </div>

        <aside className="side">
          <h2>Options from allocation engine</h2>
          <p className="hint">Lifecycle is explicit. Chat never invents availability.</p>
          {options.length === 0 ? (
            <p className="empty">No options shown yet.</p>
          ) : (
            <ul className="options">
              {options.map((o) => (
                <li key={`${o.slot_id}-${o.rank}`}>
                  <div>
                    <strong>
                      {o.rank}. {o.label}
                    </strong>
                    <div className="muted">{o.slot_id}</div>
                  </div>
                  <span className={lifecycleClass(o.lifecycle)}>{o.lifecycle}</span>
                  <button type="button" onClick={() => onSend(String(o.rank))} disabled={busy}>
                    Hold
                  </button>
                </li>
              ))}
            </ul>
          )}

          <h2>Tools used</h2>
          <ul className="tools">
            {tools.length === 0 ? <li className="muted">—</li> : tools.map((t) => <li key={t}>{t}</li>)}
          </ul>
        </aside>
      </main>
    </div>
  );
}
