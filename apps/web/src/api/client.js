const API_BASE = import.meta.env.VITE_API_BASE_URL || "";

async function parse(res) {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed (${res.status})`);
  }
  return res.json();
}

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
  return parse(res);
}

export async function fetchShipments(driverId) {
  const res = await fetch(`${API_BASE}/api/drivers/${driverId}/shipments`);
  return parse(res);
}

export async function submitLocation({
  exceptionId,
  shipmentId,
  latitude,
  longitude,
  accuracy,
  capturedAt,
  denied = false,
  error = null,
}) {
  const res = await fetch(`${API_BASE}/api/location`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exception_id: exceptionId,
      shipment_id: shipmentId || null,
      latitude: latitude ?? 0,
      longitude: longitude ?? 0,
      accuracy_m: accuracy ?? null,
      captured_at: capturedAt || new Date().toISOString(),
      denied,
      error,
    }),
  });
  return parse(res);
}

export async function declineLocation({ exceptionId, shipmentId }) {
  const res = await fetch(`${API_BASE}/api/location/decline`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exception_id: exceptionId,
      shipment_id: shipmentId || null,
    }),
  });
  return parse(res);
}

export async function runSchedule(facilityId) {
  const res = await fetch(`${API_BASE}/api/scheduling/facilities/${facilityId}/run`, {
    method: "POST",
  });
  return parse(res);
}

export async function fetchMetrics() {
  const res = await fetch(`${API_BASE}/api/metrics/summary`);
  return parse(res);
}
