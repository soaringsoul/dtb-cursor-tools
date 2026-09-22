#!/usr/bin/env node
"use strict";

const http2 = require("http2");
const path = require("path");
const crypto = require("crypto");

process.env.SAND_RPC_TEST = "1";
require(path.join(__dirname, "sand_rpc.js"));

const t = global.__sandTest;
const AGENT_RUN_PATH = "/agent.v1.AgentService/Run";
const HOST = "api2.cursor.sh";

function connectFrame(body) {
  const buf = Buffer.alloc(5 + body.length);
  buf[0] = 0;
  buf.writeUInt32BE(body.length, 1);
  Buffer.from(body).copy(buf, 5);
  return buf;
}

function parseConnectStream(buffer, onMessage) {
  let offset = 0;
  while (offset + 5 <= buffer.length) {
    const flag = buffer[offset];
    const length = buffer.readUInt32BE(offset + 1);
    offset += 5;
    const chunk = buffer.subarray(offset, offset + length);
    offset += length;
    if (flag & 0x02) {
      onMessage({ trailer: chunk });
      continue;
    }
    if (!chunk.length) {
      continue;
    }
    onMessage({ data: chunk });
  }
  return buffer.subarray(offset);
}

function buildAgentMessage(modelId, maxMode, prompt, conversationId) {
  return {
    runRequest: {
      conversationId,
      requestedModel: {
        modelId,
        maxMode,
        parameters: [{ id: "effort", value: "high" }],
      },
      action: {
        case: "userMessageAction",
        value: {
          userMessageAction: {
            userMessage: { text: prompt },
          },
        },
      },
    },
  };
}

function probeAgentRun(options) {
  const {
    jwt,
    modelId,
    clientType,
    version,
    commit,
    maxMode,
    prompt,
    timeoutMs,
  } = options;
  const conversationId = options.conversationId || crypto.randomUUID();
  const agentMsg = buildAgentMessage(modelId, maxMode, prompt, conversationId);
  const spec = t.spec(agentMsg);
  spec.messageId = crypto.randomUUID();
  const started = Date.now();

  const headers = {
    ":method": "POST",
    ":path": AGENT_RUN_PATH,
    ":scheme": "https",
    ":authority": HOST,
    authorization: `Bearer ${jwt}`,
    "content-type": "application/connect+proto",
    "connect-protocol-version": "1",
    "x-cursor-client-type": clientType || "ide",
    "x-cursor-client-version": version,
    "x-cursor-streaming": "true",
    "user-agent": "sand-verify/agent-run-probe",
  };
  if (commit) {
    headers["x-cursor-client-commit"] = commit;
  }
  if (clientType === "sand") {
    headers["x-sand-box-namespace"] = "prod";
  }
  if (process.env.SAND_PROBE_COOKIE) {
    headers.cookie = process.env.SAND_PROBE_COOKIE;
  }

  return new Promise((resolve) => {
    const client = http2.connect(`https://${HOST}`);
    let settled = false;
    let ttfbMs = null;
    let text = "";
    let think = "";
    let error = "";
    let connectCode = "";
    let leftover = Buffer.alloc(0);
    let contextReplied = false;
    let turnEnded = false;
    let httpStatus = 0;

    const finish = (result) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      try {
        req.close();
      } catch (_) {}
      try {
        client.close();
      } catch (_) {}
      resolve(result);
    };

    const timer = setTimeout(() => {
      finish({
        ok: false,
        path: "AgentService/Run",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "timeout",
        detail: contextReplied
          ? `timed out after ${timeoutMs}ms (requestContext 已回，仍无输出)`
          : `timed out after ${timeoutMs}ms (未收到 requestContextArgs)`,
      });
    }, timeoutMs);

    client.on("error", (err) => {
      finish({
        ok: false,
        path: "AgentService/Run",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "network_error",
        detail: String(err.message || err),
      });
    });

    const req = client.request(headers);
    req.setEncoding("binary");

    req.on("response", (responseHeaders) => {
      httpStatus = Number(responseHeaders[":status"] || 0);
      if (httpStatus && httpStatus !== 200) {
        finish({
          ok: false,
          path: "AgentService/Run",
          ttfb_ms: ttfbMs,
          text_preview: "",
          error_code: `http_${httpStatus}`,
          detail: `HTTP ${httpStatus}`,
        });
      }
    });

    req.on("data", (chunk) => {
      if (ttfbMs === null) {
        ttfbMs = Date.now() - started;
      }
      leftover = Buffer.concat([leftover, Buffer.from(chunk, "binary")]);
      leftover = parseConnectStream(leftover, (frame) => {
        if (frame.trailer) {
          try {
            const trailer = JSON.parse(frame.trailer.toString("utf8"));
            const err = trailer.error || {};
            connectCode = String(err.code || connectCode || "");
            const details = err.details || [];
            if (details.length && details[0] && details[0].debug) {
              const dbg = details[0].debug;
              error =
                String(dbg.error || dbg.details || err.message || error) ||
                error;
            } else if (err.message) {
              error = String(err.message);
            }
          } catch (_) {
            error = frame.trailer.toString("utf8", 0, 400);
          }
          return;
        }
        const decoded = t.decAgent(frame.data);
        if (decoded.requestContext && !contextReplied) {
          contextReplied = true;
          req.write(
            connectFrame(
              Buffer.from(
                t.encAgentContext(decoded.execId || 0, decoded.execUuid || "")
              )
            )
          );
        }
        if (decoded.text) {
          text += decoded.text;
        }
        if (decoded.think) {
          think += decoded.think;
        }
        if (decoded.turnEnded) {
          turnEnded = true;
          req.end();
        }
      });
    });

    req.on("end", () => {
      const ok = Boolean(text.trim()) && !error;
      finish({
        ok,
        path: "AgentService/Run",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code:
          error ||
          connectCode ||
          (ok ? "" : turnEnded ? "empty_response" : "no_output"),
        detail: error || connectCode || "",
        context_replied: contextReplied,
        turn_ended: turnEnded,
        http_status: httpStatus,
        think_preview: think.slice(0, 80),
      });
    });

    req.on("error", (err) => {
      finish({
        ok: false,
        path: "AgentService/Run",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "read_error",
        detail: String(err.message || err),
      });
    });

    req.write(connectFrame(Buffer.from(t.encAgentRun(spec))));
  });
}

async function main() {
  const [
    jwt,
    modelId,
    clientType,
    version,
    commit,
    maxModeArg,
    prompt,
    timeoutArg,
  ] = process.argv.slice(2);
  if (!jwt || !modelId) {
    console.error(
      "usage: node agent_run_probe.js <jwt> <model> <clientType> <version> <commit> <maxMode> <prompt> [timeoutMs]"
    );
    process.exit(2);
  }
  const result = await probeAgentRun({
    jwt,
    modelId,
    clientType: clientType || "ide",
    version: version || "0.44.0",
    commit: commit || "",
    maxMode: maxModeArg === "true",
    prompt: prompt || "Reply with exactly: OK",
    timeoutMs: Number(timeoutArg || 45000),
  });
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) {
  main().catch((err) => {
    process.stdout.write(
      JSON.stringify({
        ok: false,
        path: "AgentService/Run",
        error_code: "probe_error",
        detail: String(err.message || err),
      })
    );
    process.exit(1);
  });
}

module.exports = { probeAgentRun };
