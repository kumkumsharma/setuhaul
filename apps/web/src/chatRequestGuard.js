/**
 * Guard for in-flight chat responses vs the active driver/case UI context.
 * Bump `epoch` on driver switch / case reset; capture both values when sending.
 */
export function shouldApplyInFlightChatResult(requestCtx, currentCtx) {
  return (
    requestCtx.epoch === currentCtx.epoch &&
    requestCtx.driverId === currentCtx.driverId
  );
}
