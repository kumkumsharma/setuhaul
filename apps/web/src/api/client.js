const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

export async function sendChat({ driverId, message, exceptionId, shipmentId, idempotencyKey }) {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      driver_id: driverId,
      message,
      exception_id: exceptionId || null,
      shipment_id: shipmentId || null,
      idempotency_key: idempotencyKey || null,
    }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Chat failed (${res.status})`);
  }
  return res.json();
}

export async function fetchShipments(driverId) {
  const res = await fetch(`${API_BASE}/api/drivers/${driverId}/shipments`);
  if (!res.ok) throw new Error("Failed to load shipments");
  return res.json();
}
