import { createHash, X509Certificate } from "node:crypto";
import { spawn } from "node:child_process";
import tls from "node:tls";


const delay = (milliseconds) => new Promise((resolve) => {
  setTimeout(resolve, milliseconds);
});


async function waitFor(predicate, label, timeoutMilliseconds = 20_000) {
  const deadline = Date.now() + timeoutMilliseconds;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) {
      return value;
    }
    await delay(50);
  }
  throw new Error(`timed_out_waiting_for_${label}`);
}


async function currentSpki(host, port) {
  return new Promise((resolve, reject) => {
    const socket = tls.connect({
      host,
      port,
      rejectUnauthorized: false,
      servername: "localhost",
    });
    socket.once("secureConnect", () => {
      try {
        const peer = socket.getPeerCertificate(true);
        if (!peer || !peer.raw) {
          throw new Error("runtime_certificate_unavailable");
        }
        const certificate = new X509Certificate(peer.raw);
        const spki = certificate.publicKey.export({
          format: "der",
          type: "spki",
        });
        resolve(createHash("sha256").update(spki).digest("base64"));
      } catch (error) {
        reject(error);
      } finally {
        socket.destroy();
      }
    });
    socket.once("error", reject);
  });
}


class DevToolsSession {
  constructor(webSocket) {
    this.webSocket = webSocket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    webSocket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== undefined) {
        const pending = this.pending.get(message.id);
        if (pending) {
          this.pending.delete(message.id);
          if (message.error) {
            pending.reject(
              new Error(
                `devtools_command_failed_${pending.method}_${message.error.code}`,
              ),
            );
          } else {
            pending.resolve(message.result || {});
          }
        }
        return;
      }
      const listeners = this.listeners.get(message.method) || [];
      for (const listener of listeners) {
        listener(message.params || {});
      }
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { method, resolve, reject });
      this.webSocket.send(JSON.stringify({ id, method, params }));
    });
  }
}


async function connectDevTools(debugPort) {
  const targets = await waitFor(async () => {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
      if (!response.ok) {
        return null;
      }
      return response.json();
    } catch (_error) {
      return null;
    }
  }, "chrome_devtools");
  const resolvedTargets = await targets;
  const page = resolvedTargets.find((target) => target.type === "page");
  if (!page || !page.webSocketDebuggerUrl) {
    throw new Error("chrome_page_target_unavailable");
  }
  const webSocket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    webSocket.addEventListener("open", resolve, { once: true });
    webSocket.addEventListener("error", reject, { once: true });
  });
  return new DevToolsSession(webSocket);
}


function headerValue(headers, name) {
  const wanted = name.toLowerCase();
  for (const [candidate, value] of Object.entries(headers || {})) {
    if (candidate.toLowerCase() === wanted) {
      return String(value);
    }
  }
  return null;
}


function isPinnedProviderAuthorizationUrl(value) {
  try {
    const target = new URL(value);
    return (
      target.origin === "https://accounts.google.com"
      && target.pathname === "/o/oauth2/v2/auth"
      && target.search.length > 1
      && target.hash === ""
    );
  } catch (_error) {
    return false;
  }
}


async function evaluate(session, expression) {
  const result = await session.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error("browser_evaluation_failed");
  }
  return result.result ? result.result.value : undefined;
}


async function main() {
  const [
    chromeExecutable,
    loginUrl,
    runtimeHost,
    runtimePortText,
    userDataDirectory,
    debugPortText,
  ] = process.argv.slice(2);
  const invitation = process.env.WAHOJOBS_SYNTHETIC_INVITATION;
  if (
    !chromeExecutable
    || !loginUrl
    || !runtimeHost
    || !runtimePortText
    || !userDataDirectory
    || !debugPortText
    || !invitation
  ) {
    throw new Error("invalid_chrome_driver_arguments");
  }
  const runtimePort = Number(runtimePortText);
  const debugPort = Number(debugPortText);
  const origin = new URL(loginUrl).origin;
  const loginStartUrl = `${origin}/auth/google/start`;
  const providerPrefix = "https://accounts.google.com/";
  const spki = await currentSpki(runtimeHost, runtimePort);
  const chrome = spawn(chromeExecutable, [
    `--user-data-dir=${userDataDirectory}`,
    "--disable-extensions",
    "--disable-component-extensions-with-background-pages",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--remote-debugging-address=127.0.0.1",
    `--remote-debugging-port=${debugPort}`,
    `--ignore-certificate-errors-spki-list=${spki}`,
    "about:blank",
  ], {
    stdio: ["ignore", "ignore", "ignore"],
    windowsHide: true,
  });

  let session;
  try {
    session = await connectDevTools(debugPort);
    const observed = {
      cspFormActionViolationCount: 0,
      cspSecurityLogCount: 0,
      loginGetCount: 0,
      loginStatus: null,
      responseContentSecurityPolicy: null,
      responseReferrerPolicy: null,
      startPostCount: 0,
      startRequestId: null,
      startStatus: null,
      cdpOrigin: null,
      startRedirectStatus: null,
      providerBlockedCount: 0,
      providerFailures: [],
      providerTargetMatchesPinned: false,
    };
    const extraHeaders = new Map();

    session.on("Network.requestWillBeSent", (event) => {
      if (event.request.url === loginUrl && event.request.method === "GET") {
        observed.loginGetCount += 1;
      }
      if (event.request.url === loginStartUrl && event.request.method === "POST") {
        observed.startPostCount += 1;
        observed.startRequestId = event.requestId;
        observed.cdpOrigin = headerValue(event.request.headers, "Origin");
      }
      if (
        event.request.url.startsWith(providerPrefix)
        && event.redirectResponse
      ) {
        observed.startRedirectStatus = event.redirectResponse.status;
      }
    });
    session.on("Network.requestWillBeSentExtraInfo", (event) => {
      extraHeaders.set(event.requestId, event.headers || {});
      if (event.requestId === observed.startRequestId) {
        observed.cdpOrigin = (
          headerValue(event.headers, "Origin") || observed.cdpOrigin
        );
      }
    });
    session.on("Network.responseReceived", (event) => {
      if (event.response.url === loginUrl && event.type === "Document") {
        observed.loginStatus = event.response.status;
        observed.responseContentSecurityPolicy = headerValue(
          event.response.headers,
          "Content-Security-Policy",
        );
        observed.responseReferrerPolicy = headerValue(
          event.response.headers,
          "Referrer-Policy",
        );
      }
      if (event.response.url === loginStartUrl) {
        observed.startStatus = event.response.status;
      }
    });
    session.on("Log.entryAdded", (event) => {
      const entry = event.entry || {};
      const text = String(entry.text || "");
      if (entry.source === "security") {
        observed.cspSecurityLogCount += 1;
      }
      if (
        entry.source === "security"
        && text.includes("Content Security Policy")
        && text.includes("form-action")
        && text.includes("'self'")
      ) {
        observed.cspFormActionViolationCount += 1;
      }
    });
    session.on("Fetch.requestPaused", (event) => {
      if (
        event.request.url === loginStartUrl
        && event.responseStatusCode !== undefined
      ) {
        observed.startStatus = event.responseStatusCode;
        session.send("Fetch.continueRequest", {
          requestId: event.requestId,
        }).catch((error) => {
          observed.providerFailures.push(error.message);
        });
        return;
      }
      if (event.request.url.startsWith(providerPrefix)) {
        observed.providerBlockedCount += 1;
        observed.providerTargetMatchesPinned = (
          isPinnedProviderAuthorizationUrl(event.request.url)
        );
        session.send("Fetch.failRequest", {
          requestId: event.requestId,
          errorReason: "BlockedByClient",
        }).catch((error) => {
          observed.providerFailures.push(error.message);
        });
      }
    });

    await session.send("Page.enable");
    await session.send("Runtime.enable");
    await session.send("Network.enable");
    await session.send("Log.enable");
    await session.send("Fetch.enable", {
      patterns: [
        {
          urlPattern: loginStartUrl,
          requestStage: "Response",
        },
        {
          urlPattern: "https://accounts.google.com/*",
          requestStage: "Request",
        },
      ],
    });
    await session.send("Page.navigate", { url: loginUrl });
    await waitFor(
      () => observed.loginStatus === 200 && observed.loginGetCount === 1,
      "login_document",
    );
    const document = await evaluate(session, `(() => {
      const form = document.querySelector("form[action='/auth/google/start']");
      const invitationField = document.querySelector("input[name='invitation']");
      return {
        url: location.href,
        origin: location.origin,
        formMethod: form ? form.method.toUpperCase() : null,
        formAction: form ? form.action : null,
        invitationFieldPresent: Boolean(invitationField),
        secureContext: window.isSecureContext,
      };
    })()`);
    if (
      document.url !== loginUrl
      || document.origin !== origin
      || document.formMethod !== "POST"
      || document.formAction !== loginStartUrl
      || document.invitationFieldPresent !== true
      || document.secureContext !== true
    ) {
      throw new Error("login_document_contract_failed");
    }

    await evaluate(session, `(() => {
      const form = document.querySelector("form[action='/auth/google/start']");
      const invitationField = document.querySelector("input[name='invitation']");
      if (!form || !invitationField) {
        throw new Error("login_form_unavailable");
      }
      invitationField.value = ${JSON.stringify(invitation)};
      form.requestSubmit();
      return true;
    })()`);
    await waitFor(
      () => observed.startPostCount === 1,
      "login_start_post",
    );
    await waitFor(
      () => observed.startStatus === 303,
      "login_start_response",
    );
    await delay(1_000);
    if (observed.startRequestId && extraHeaders.has(observed.startRequestId)) {
      observed.cdpOrigin = (
        headerValue(extraHeaders.get(observed.startRequestId), "Origin")
        || observed.cdpOrigin
      );
    }
    if (observed.providerFailures.length !== 0) {
      throw new Error("provider_interception_failed");
    }
    if (observed.providerBlockedCount > 1) {
      throw new Error("unexpected_duplicate_provider_navigation");
    }

    process.stdout.write(`${JSON.stringify({
      chromePid: chrome.pid,
      cdpOrigin: observed.cdpOrigin,
      cspFormActionViolation: observed.cspFormActionViolationCount > 0,
      cspFormActionViolationCount: observed.cspFormActionViolationCount,
      cspSecurityLogCount: observed.cspSecurityLogCount,
      documentOrigin: document.origin,
      documentContentSecurityPolicy: observed.responseContentSecurityPolicy,
      documentReferrerPolicy: observed.responseReferrerPolicy,
      finalLoginUrl: document.url,
      globalCertificateErrorsIgnored: false,
      loginGetCount: observed.loginGetCount,
      loginStatus: observed.loginStatus,
      narrowSpkiAllowlistUsed: true,
      providerBlockedBeforeNetwork: observed.providerBlockedCount,
      providerTargetMatchesPinned: observed.providerTargetMatchesPinned,
      responseReferrerPolicy: observed.responseReferrerPolicy,
      secureContext: document.secureContext,
      startPostCount: observed.startPostCount,
      startRedirectStatus: observed.startRedirectStatus,
      startStatus: observed.startStatus,
    })}\n`);
  } finally {
    if (session) {
      try {
        await session.send("Browser.close");
      } catch (_error) {
        // The fallback below still terminates only this task-created browser.
      }
    }
    await Promise.race([
      new Promise((resolve) => chrome.once("exit", resolve)),
      delay(5_000),
    ]);
    if (chrome.exitCode === null) {
      chrome.kill();
      await Promise.race([
        new Promise((resolve) => chrome.once("exit", resolve)),
        delay(5_000),
      ]);
    }
  }
}


main().catch((error) => {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
});
