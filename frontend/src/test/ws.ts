/**
 * MockWebSocket — a reusable WebSocket test double (testing.md: "mock the
 * WebSocket with the helper in src/test/ws.ts").
 *
 * NOTE: this file fulfils platform-foundation's testing-infra contract. It was
 * created here, during dataset-library task 5, because the referenced helper
 * did not yet exist. It is deliberately generic (not task-specific) so later
 * specs can reuse it.
 *
 * What it provides:
 * - {@link installMockWebSocket} / {@link uninstallMockWebSocket}: swap the
 *   global `WebSocket` for `MockWebSocket` and restore it afterwards.
 * - A registry of every instance a test created ({@link mockWebSockets},
 *   {@link lastMockWebSocket}), so a test can reach the socket the code under
 *   test opened without the code exposing it.
 * - Per-instance helpers to drive the connection from the "server" side:
 *   `simulateOpen`, `simulateMessage` (server→client frame), `simulateError`,
 *   `simulateClose`, plus a `sent` log of frames the client wrote.
 * - `simulateDrop` to model an unexpected disconnect, so reconnect/backoff and
 *   refetch-on-reconnect logic can be exercised with fake timers.
 *
 * The implementation matches the subset of the browser `WebSocket` API the app
 * uses: the `url`, `readyState`, the `WebSocket.CONNECTING|OPEN|CLOSING|CLOSED`
 * constants, `onopen`/`onmessage`/`onerror`/`onclose` handler properties,
 * `addEventListener`/`removeEventListener`, `send`, and `close`.
 */

type Listener = (event: Event) => void;

/** The four standard `readyState` values. */
export const WS_CONNECTING = 0;
export const WS_OPEN = 1;
export const WS_CLOSING = 2;
export const WS_CLOSED = 3;

/**
 * A minimal, test-controllable stand-in for the browser `WebSocket`.
 *
 * Construction does NOT auto-open: a test (or the `install` helper's default)
 * decides when the socket opens, so connection timing is explicit. Call
 * {@link simulateOpen} to fire `open`, or construct via
 * {@link installMockWebSocket} with `autoOpen` to open on the next microtask.
 */
export class MockWebSocket {
  static readonly CONNECTING = WS_CONNECTING;
  static readonly OPEN = WS_OPEN;
  static readonly CLOSING = WS_CLOSING;
  static readonly CLOSED = WS_CLOSED;

  readonly CONNECTING = WS_CONNECTING;
  readonly OPEN = WS_OPEN;
  readonly CLOSING = WS_CLOSING;
  readonly CLOSED = WS_CLOSED;

  readonly url: string;
  readyState: number = WS_CONNECTING;

  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  /** Every frame the client wrote via {@link send}, in order. */
  readonly sent: string[] = [];

  private readonly listeners: Record<string, Set<Listener>> = {
    open: new Set(),
    message: new Set(),
    error: new Set(),
    close: new Set(),
  };

  constructor(url: string | URL) {
    this.url = String(url);
    mockWebSockets.push(this);
  }

  addEventListener(type: string, listener: Listener): void {
    (this.listeners[type] ??= new Set()).add(listener);
  }

  removeEventListener(type: string, listener: Listener): void {
    this.listeners[type]?.delete(listener);
  }

  /** Record an outbound client frame. */
  send(data: string): void {
    this.sent.push(typeof data === "string" ? data : String(data));
  }

  /**
   * Client-initiated close. Mirrors the browser: moves to CLOSING, then fires a
   * `close` event (which {@link simulateClose} delivers).
   */
  close(code = 1000, reason = ""): void {
    if (this.readyState === WS_CLOSED || this.readyState === WS_CLOSING) return;
    this.readyState = WS_CLOSING;
    this.simulateClose(code, reason, true);
  }

  // ── server-side drivers (used by tests) ────────────────────────────────────

  /** Fire `open`; the socket becomes OPEN. */
  simulateOpen(): void {
    this.readyState = WS_OPEN;
    this.dispatch("open", new Event("open"));
  }

  /** Deliver a server→client message. Objects are JSON-stringified. */
  simulateMessage(data: unknown): void {
    const payload = typeof data === "string" ? data : JSON.stringify(data);
    this.dispatch("message", new MessageEvent("message", { data: payload }));
  }

  /** Fire an `error` event (does not close the socket on its own). */
  simulateError(): void {
    this.dispatch("error", new Event("error"));
  }

  /** Fire a `close` event and mark the socket CLOSED. */
  simulateClose(code = 1006, reason = "", wasClean = false): void {
    this.readyState = WS_CLOSED;
    this.dispatch("close", makeCloseEvent(code, reason, wasClean));
  }

  /**
   * Model an unexpected drop (e.g. network loss): an `error` followed by a
   * non-clean `close`, exactly as a browser reports a lost connection. Reconnect
   * and backoff logic should trigger off this.
   */
  simulateDrop(code = 1006): void {
    this.simulateError();
    this.simulateClose(code, "", false);
  }

  private dispatch(type: string, event: Event): void {
    const prop = (
      {
        open: "onopen",
        message: "onmessage",
        error: "onerror",
        close: "onclose",
      } as const
    )[type as "open" | "message" | "error" | "close"];
    const handler = this[prop] as ((event: Event) => void) | null;
    handler?.call(this, event);
    for (const listener of this.listeners[type] ?? []) listener.call(this, event);
  }
}

/** Build a `CloseEvent`, falling back to a plain `Event` under jsdom quirks. */
function makeCloseEvent(code: number, reason: string, wasClean: boolean): CloseEvent {
  try {
    return new CloseEvent("close", { code, reason, wasClean });
  } catch {
    const event = new Event("close") as Event & {
      code?: number;
      reason?: string;
      wasClean?: boolean;
    };
    event.code = code;
    event.reason = reason;
    event.wasClean = wasClean;
    return event as CloseEvent;
  }
}

/** Every `MockWebSocket` created since the last {@link resetMockWebSockets}. */
export const mockWebSockets: MockWebSocket[] = [];

/** The most recently constructed `MockWebSocket`, or undefined if none. */
export function lastMockWebSocket(): MockWebSocket | undefined {
  return mockWebSockets[mockWebSockets.length - 1];
}

/** Clear the instance registry (call between cases to isolate tests). */
export function resetMockWebSockets(): void {
  mockWebSockets.length = 0;
}

type GlobalWithWebSocket = typeof globalThis & { WebSocket?: unknown };

let originalWebSocket: unknown;
let installed = false;

/**
 * Assign the global `WebSocket`. jsdom defines it as a non-writable property, so
 * a plain assignment throws; `defineProperty` replaces it regardless.
 */
function setGlobalWebSocket(value: unknown): void {
  Object.defineProperty(globalThis, "WebSocket", {
    value,
    writable: true,
    configurable: true,
  });
}

export interface InstallOptions {
  /**
   * When true, each new socket fires `open` on the next microtask, so code that
   * expects an eventual connection works without the test driving every open.
   * Defaults to false — the test calls `simulateOpen()` explicitly.
   */
  autoOpen?: boolean;
}

/**
 * Replace the global `WebSocket` with {@link MockWebSocket} and reset the
 * instance registry. Returns the class so a test can read its constants.
 */
export function installMockWebSocket(options: InstallOptions = {}): typeof MockWebSocket {
  const globalRef = globalThis as GlobalWithWebSocket;
  if (!installed) {
    originalWebSocket = globalRef.WebSocket;
    installed = true;
  }
  resetMockWebSockets();

  if (options.autoOpen) {
    class AutoOpenWebSocket extends MockWebSocket {
      constructor(url: string | URL) {
        super(url);
        queueMicrotask(() => {
          if (this.readyState === WS_CONNECTING) this.simulateOpen();
        });
      }
    }
    setGlobalWebSocket(AutoOpenWebSocket);
    return AutoOpenWebSocket;
  }

  setGlobalWebSocket(MockWebSocket);
  return MockWebSocket;
}

/** Restore the real global `WebSocket` and clear the registry. */
export function uninstallMockWebSocket(): void {
  if (!installed) return;
  setGlobalWebSocket(originalWebSocket);
  installed = false;
  resetMockWebSockets();
}
