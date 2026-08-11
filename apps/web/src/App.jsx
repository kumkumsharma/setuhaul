import { useMemo, useState } from "react";
import {
  declineLocation,
  fetchMetrics,
  fetchShipments,
  runSchedule,
  sendChat,
  submitLocation,
} from "./api/client.js";

const DEMO_DRIVERS = [
  { id: "DRV-027", label: "Ravi Kumar (tyre / Neemrana)" },
  { id: "DRV-EVE-00", label: "Evening Driver 00 (contention)" },
  { id: "DRV-MULTI", label: "Karan Singh (multi-shipment)" },
  { id: "DRV-NOP", label: "No-slot driver (escalate)" },
  { id: "DRV-HIPRI", label: "Priority late entrant" },
];

const DEMO_NEEMRANA = { lat: 27.9889, lon: 76.3881 };

function lifecycleClass(lifecycle) {
  return `life life-${lifecycle || "shown"}`;
}

function applyChatResult(res, setters) {
  const {
    setExceptionId,
    setShipmentId,
    setStatus,
    setOptions,
    setHold,
    setTools,
    setMessages,
    setShipments,
    setClientAction,
    setEtaComparison,
  } = setters;
  setExceptionId(res.exception_id || null);
  if (res.shipment_id) setShipmentId(res.shipment_id);
  setStatus(res.status);
  setOptions(res.options || []);
  setHold(res.hold || null);
  setTools(res.tools_used || []);
  setClientAction(res.client_action || null);
  setEtaComparison(res.eta_comparison || null);
  setMessages((m) => [...m, { role: "agent", text: res.reply, escalated: res.escalated }]);
  if (res.needs_shipment_choice?.length) setShipments(res.needs_shipment_choice);
}

export default function App() {
  const [tab, setTab] = useState("chat");
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
      text: "SetuHaul exception chat. Capacity comes only from the allocation engine — shown ≠ held ≠ confirmed. Location sharing is optional.",
    },
  ]);
  const [options, setOptions] = useState([]);
  const [status, setStatus] = useState("idle");
  const [hold, setHold] = useState(null);
  const [tools, setTools] = useState([]);
  const [clientAction, setClientAction] = useState(null);
  const [etaComparison, setEtaComparison] = useState(null);
  const [schedule, setSchedule] = useState(null);
  const [metrics, setMetrics] = useState(null);

  const setters = useMemo(
    () => ({
      setExceptionId,
      setShipmentId,
      setStatus,
      setOptions,
      setHold,
      setTools,
      setMessages,
      setShipments,
      setClientAction,
      setEtaComparison,
    }),
    []
  );

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
      applyChatResult(res, setters);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function onShareLocation(useDemo = false) {
    if (!exceptionId || busy) return;
    setBusy(true);
    setError("");
    try {
      let coords;
      if (useDemo) {
        coords = {
          latitude: DEMO_NEEMRANA.lat,
          longitude: DEMO_NEEMRANA.lon,
          accuracy: 25,
          capturedAt: new Date().toISOString(),
        };
      } else {
        const pos = await new Promise((resolve, reject) => {
          if (!navigator.geolocation) {
            reject(new Error("Geolocation not available in this browser"));
            return;
          }
          navigator.geolocation.getCurrentPosition(resolve, reject, {
            enableHighAccuracy: true,
            timeout: 10000,
          });
        });
        coords = {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
          accuracy: pos.coords.accuracy,
          capturedAt: new Date(pos.timestamp).toISOString(),
        };
      }
      setMessages((m) => [
        ...m,
        {
          role: "driver",
          text: useDemo
            ? `Shared demo Neemrana location (${coords.latitude.toFixed(4)}, ${coords.longitude.toFixed(4)})`
            : `Shared browser location (${coords.latitude.toFixed(4)}, ${coords.longitude.toFixed(4)})`,
        },
      ]);
      const res = await submitLocation({
        exceptionId,
        shipmentId,
        latitude: coords.latitude,
        longitude: coords.longitude,
        accuracy: coords.accuracy,
        capturedAt: coords.capturedAt,
      });
      applyChatResult(res, setters);
    } catch (err) {
      const res = await submitLocation({
        exceptionId,
        shipmentId,
        latitude: 0,
        longitude: 0,
        denied: true,
        error: err.message || "geolocation_failed",
      });
      applyChatResult(res, setters);
    } finally {
      setBusy(false);
    }
  }

  async function onDeclineLocation() {
    if (!exceptionId || busy) return;
    setBusy(true);
    try {
      setMessages((m) => [...m, { role: "driver", text: "I do not want to share location." }]);
      const res = await declineLocation({ exceptionId, shipmentId });
      applyChatResult(res, setters);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function onRunSchedule() {
    setBusy(true);
    setError("");
    try {
      const res = await runSchedule("FAC-JPR-01");
      setSchedule(res);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function onLoadMetrics() {
    setBusy(true);
    setError("");
    try {
      setMetrics(await fetchMetrics());
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
            Phase 2 — location-aware ETA, facility schedule tool, and before/after metrics on top of
            Phase 1 allocation.
          </p>
        </div>
        <div className="meta">
          <span className="pill">status: {statusLabel}</span>
          {hold ? <span className="pill hold">hold: {hold.slot_id}</span> : null}
          {exceptionId ? <span className="pill">exc: {exceptionId}</span> : null}
        </div>
      </header>

      <nav className="tabs">
        {[
          ["chat", "Chat"],
          ["schedule", "Facility schedule"],
          ["metrics", "Before / after"],
        ].map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={tab === id ? "tab active" : "tab"}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "chat" ? (
        <>
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
                  setClientAction(null);
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
              {clientAction === "REQUEST_BROWSER_LOCATION" || exceptionId ? (
                <div className="location-bar">
                  <button type="button" onClick={() => onShareLocation(false)} disabled={busy || !exceptionId}>
                    Share location
                  </button>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => onShareLocation(true)}
                    disabled={busy || !exceptionId}
                  >
                    Demo: Neemrana pin
                  </button>
                  <button type="button" className="ghost" onClick={onDeclineLocation} disabled={busy || !exceptionId}>
                    Decline location
                  </button>
                </div>
              ) : null}
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
                  "yes",
                  "What are the next slots after 7 PM?",
                  "1",
                  "confirm",
                  "Has it been confirmed?",
                  "no",
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
              {etaComparison?.route_eta ? (
                <div className="eta-box">
                  <div>Declared/planned: {String(etaComparison.driver_declared_or_planned_eta)}</div>
                  <div>Route ETA: {String(etaComparison.route_eta)}</div>
                  <div>Delta: {etaComparison.delta_minutes} min ({etaComparison.provider})</div>
                </div>
              ) : null}
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
        </>
      ) : null}

      {tab === "schedule" ? (
        <section className="panel">
          <h2>Jaipur facility scheduling tool</h2>
          <p className="hint">
            Rule-based engine (explicit scores). Does not confirm bookings — Phase 1 allocator remains
            capacity truth.
          </p>
          <button type="button" onClick={onRunSchedule} disabled={busy}>
            Run schedule for FAC-JPR-01
          </button>
          {schedule ? (
            <div className="schedule-out">
              <p>
                <strong>{schedule.run_id}</strong> · objective: {schedule.objective}
              </p>
              <pre className="block">{schedule.explanation}</pre>
              <ul>
                {(schedule.proposal?.assignments || []).map((a) => (
                  <li key={`${a.shipment_id}-${a.start}`}>
                    {a.shipment_id} → {a.dock_id} {a.start}–{a.end}
                    {a.fixed ? " (fixed)" : ""} score={a.score ?? "—"}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>
      ) : null}

      {tab === "metrics" ? (
        <section className="panel">
          <h2>Before vs after</h2>
          <p className="hint">
            Before = seeded manual baseline. After = live CaseMetric rows from this workflow.
          </p>
          <button type="button" onClick={onLoadMetrics} disabled={busy}>
            Refresh metrics
          </button>
          {metrics ? (
            <div className="metrics-grid">
              <div>
                <h3>Manual process</h3>
                {(metrics.before_manual || []).map((b) => (
                  <ul key={b.label}>
                    <li>Resolution: {b.avg_resolution_minutes} min</li>
                    <li>Human help: {(b.human_help_rate * 100).toFixed(0)}%</li>
                    <li>ETA error: {b.avg_eta_error_minutes} min</li>
                    <li>n={b.sample_size}</li>
                  </ul>
                ))}
              </div>
              <div>
                <h3>With solution</h3>
                <ul>
                  <li>Cases: {metrics.after_solution.cases}</li>
                  <li>Confirmed: {metrics.after_solution.confirmed}</li>
                  <li>Escalated: {metrics.after_solution.escalated}</li>
                  <li>Avg resolution: {metrics.after_solution.avg_resolution_minutes ?? "—"} min</li>
                  <li>
                    Human help:{" "}
                    {metrics.after_solution.human_help_rate != null
                      ? `${(metrics.after_solution.human_help_rate * 100).toFixed(0)}%`
                      : "—"}
                  </li>
                  <li>Self-service: {metrics.after_solution.self_service_rate ?? "—"}</li>
                  <li>ETA error: {metrics.after_solution.avg_eta_error_minutes ?? "—"} min</li>
                  <li>First-option accept: {metrics.after_solution.first_option_accept_rate ?? "—"}</li>
                  <li>Wait reduced: {metrics.after_solution.avg_wait_reduced_minutes ?? "—"} min</li>
                </ul>
              </div>
            </div>
          ) : null}
          {metrics?.comparison_note ? <p className="hint">{metrics.comparison_note}</p> : null}
        </section>
      ) : null}
    </div>
  );
}
