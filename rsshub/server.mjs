import { createServer } from "node:http";
import { open, readFile, readdir, rename, rm, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";


const SUPPORTED_OPERATIONS = Object.freeze([
  "UserByScreenName",
  "UserTweets",
]);
const QUERY_ID = /^[A-Za-z0-9_-]{10,128}$/;
const MAX_PAGE_BYTES = 2 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 16 * 1024 * 1024;
const MAX_UTILS_BYTES = 16 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_URL_LENGTH = 2048;
const MAX_CONCURRENT_REQUESTS = 2;
const FETCH_TIMEOUT_MS = 15_000;
const ROUTE = /^\/twitter\/user\/[A-Za-z0-9_]{1,15}\/includeReplies=0&includeRts=0&count=[1-9][0-9]?&readable=1&showQuotedInTitle=0$/;


function requireQueryId(value) {
  if (typeof value !== "string" || !QUERY_ID.test(value)) {
    throw new Error("invalid query id");
  }
  return value;
}


export function extractQueryIds(bundle) {
  if (typeof bundle !== "string" || bundle.length > MAX_BUNDLE_BYTES) {
    throw new Error("invalid bundle");
  }
  const found = new Map(SUPPORTED_OPERATIONS.map((name) => [name, new Set()]));
  const patterns = [
    /queryId\s*:\s*["']([^"']+)["'][^{};]{0,400}?operationName\s*:\s*["']([^"']+)["']/g,
    /operationName\s*:\s*["']([^"']+)["'][^{};]{0,400}?queryId\s*:\s*["']([^"']+)["']/g,
  ];
  for (const [index, pattern] of patterns.entries()) {
    for (const match of bundle.matchAll(pattern)) {
      const operation = index === 0 ? match[2] : match[1];
      const queryId = index === 0 ? match[1] : match[2];
      if (found.has(operation)) {
        found.get(operation).add(queryId);
      }
    }
  }
  const result = {};
  for (const operation of SUPPORTED_OPERATIONS) {
    const values = [...found.get(operation)];
    if (values.length !== 1) {
      throw new Error("missing or ambiguous query id");
    }
    result[operation] = requireQueryId(values[0]);
  }
  return result;
}


async function readBounded(response, maximum) {
  if (!response.body) {
    throw new Error("missing response body");
  }
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maximum) {
        throw new Error("response too large");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: true }).decode(body);
}


function mainBundleUrl(page) {
  const matches = page.match(
    /https:\/\/abs\.twimg\.com\/responsive-web\/client-web\/main\.[A-Za-z0-9._-]+\.js/g,
  ) || [];
  const unique = [...new Set(matches)];
  if (unique.length !== 1) {
    throw new Error("missing or ambiguous main bundle");
  }
  const parsed = new URL(unique[0]);
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname !== "abs.twimg.com" ||
    !parsed.pathname.startsWith("/responsive-web/client-web/main.") ||
    !parsed.pathname.endsWith(".js")
  ) {
    throw new Error("invalid main bundle URL");
  }
  return parsed.href;
}


async function fetchCurrentQueryIds() {
  const authToken = process.env["TWITTER_AUTH_TOKEN"];
  if (!QUERY_ID.test(authToken || "")) {
    throw new Error("missing authentication token");
  }
  const commonHeaders = {
    "User-Agent": "Mozilla/5.0 codex-quota-monitor/0.1",
    Accept: "text/html,application/javascript",
  };
  const pageResponse = await fetch("https://x.com", {
    headers: { ...commonHeaders, Cookie: `auth_token=${authToken}` },
    redirect: "follow",
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!pageResponse.ok) {
    throw new Error("X page request failed");
  }
  const page = await readBounded(pageResponse, MAX_PAGE_BYTES);
  const bundleResponse = await fetch(mainBundleUrl(page), {
    headers: commonHeaders,
    redirect: "error",
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!bundleResponse.ok) {
    throw new Error("X bundle request failed");
  }
  return extractQueryIds(await readBounded(bundleResponse, MAX_BUNDLE_BYTES));
}


async function locateResolverFile(rsshubRoot) {
  const distLib = resolve(rsshubRoot, "dist-lib");
  const entries = await readdir(distLib, { withFileTypes: true });
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile() || !/^utils-.*\.mjs$/.test(entry.name)) continue;
    const path = resolve(distLib, entry.name);
    const details = await stat(path);
    if (details.size > MAX_UTILS_BYTES) {
      throw new Error("RSSHub utility file too large");
    }
    const contents = await readFile(path, "utf8");
    if (contents.includes("gql-id-resolver") && contents.includes("fallbackIds")) {
      candidates.push({ path, contents, mode: details.mode });
    }
  }
  if (candidates.length !== 1) {
    throw new Error("missing or ambiguous RSSHub resolver");
  }
  return candidates[0];
}


export function replaceFallbackIds(contents, queryIds) {
  const blocks = [
    ...contents.matchAll(/const fallbackIds\s*=\s*\{[\s\S]*?\n\s*\};/g),
  ];
  if (blocks.length !== 1) {
    throw new Error("missing or ambiguous fallback block");
  }
  let block = blocks[0][0];
  for (const operation of SUPPORTED_OPERATIONS) {
    const queryId = requireQueryId(queryIds[operation]);
    const pattern = new RegExp(`(\\b${operation}\\s*:\\s*["'])([^"']+)(["'])`, "g");
    const matches = [...block.matchAll(pattern)];
    if (matches.length !== 1 || !QUERY_ID.test(matches[0][2])) {
      throw new Error("missing or invalid fallback id");
    }
    block = block.replace(pattern, `$1${queryId}$3`);
  }
  return (
    contents.slice(0, blocks[0].index) +
    block +
    contents.slice(blocks[0].index + blocks[0][0].length)
  );
}


async function writeAtomic(path, contents, mode) {
  const temporary = resolve(
    dirname(path),
    `.${path.split("/").pop()}.${process.pid}.tmp`,
  );
  let handle;
  try {
    handle = await open(temporary, "wx", mode & 0o777);
    await handle.writeFile(contents, "utf8");
    await handle.sync();
    // The refresh unit uses UMask=0077. Restore the root-owned dependency's
    // original read permissions explicitly before the atomic rename so the
    // unprivileged RSSHub process can import it afterwards.
    await handle.chmod(mode & 0o777);
    await handle.close();
    handle = undefined;
    await rename(temporary, path);
  } finally {
    if (handle) await handle.close().catch(() => {});
    await rm(temporary, { force: true }).catch(() => {});
  }
}


async function patchRssHubQueryIds(queryIds) {
  const packageEntry = import.meta.resolve("rsshub");
  const rsshubRoot = resolve(dirname(fileURLToPath(packageEntry)), "..");
  const resolver = await locateResolverFile(rsshubRoot);
  const patched = replaceFallbackIds(resolver.contents, queryIds);
  await writeAtomic(resolver.path, patched, resolver.mode);
}


export async function refreshRssHubQueryIds() {
  await patchRssHubQueryIds(await fetchCurrentQueryIds());
}


function sendJson(response, status, value) {
  const body = JSON.stringify(value);
  if (Buffer.byteLength(body) > MAX_RESPONSE_BYTES) {
    response.writeHead(502, { "Content-Type": "application/json" });
    response.end('{"error":"upstream response too large"}');
    return;
  }
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  response.end(body);
}


async function start() {
  const port = Number(process.env.PORT || "1200");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("invalid port");
  }
  // RSSHub's public package API still resolves some runtime metadata from the
  // current directory. Keep that legacy assumption contained in this adapter.
  process.chdir(dirname(fileURLToPath(import.meta.url)));
  const { init, request } = await import("rsshub");
  await init(process.env);

  let activeRequests = 0;
  const server = createServer(async (incoming, response) => {
    if (
      incoming.method !== "GET" ||
      incoming.url === undefined ||
      incoming.url.length > MAX_URL_LENGTH ||
      incoming.headers["transfer-encoding"] !== undefined ||
      Number(incoming.headers["content-length"] || "0") !== 0
    ) {
      sendJson(response, 400, { error: "invalid request" });
      return;
    }
    let parsed;
    try {
      parsed = new URL(incoming.url, `http://127.0.0.1:${port}`);
    } catch {
      sendJson(response, 400, { error: "invalid request" });
      return;
    }
    if (parsed.pathname === "/healthz" && parsed.search === "") {
      sendJson(response, 200, { status: "ok" });
      return;
    }
    if (!parsed.pathname.startsWith("/twitter/user/") || !ROUTE.test(parsed.pathname) || parsed.search) {
      sendJson(response, 404, { error: "not found" });
      return;
    }
    if (activeRequests >= MAX_CONCURRENT_REQUESTS) {
      sendJson(response, 429, { error: "busy" });
      return;
    }
    activeRequests += 1;
    try {
      const result = await request(parsed.pathname);
      sendJson(response, 200, result);
    } catch {
      sendJson(response, 502, { error: "upstream request failed" });
    } finally {
      activeRequests -= 1;
    }
  });
  server.requestTimeout = 20_000;
  server.headersTimeout = 5_000;
  server.keepAliveTimeout = 1_000;
  server.maxHeadersCount = 32;
  server.listen({ port, host: "127.0.0.1" });
}


const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  const action = process.argv[2];
  let operation;
  if (process.argv[2] === "--refresh-query-ids") {
    operation = refreshRssHubQueryIds();
  } else if (action === undefined) {
    operation = start();
  } else {
    operation = Promise.reject(new Error("unsupported action"));
  }
  operation.catch(() => {
    console.error(
      action === "--refresh-query-ids"
        ? "RSSHub query ID refresh failed"
        : "RSSHub adapter failed to start",
    );
    process.exitCode = 1;
  });
}
