#!/usr/bin/env node
"use strict";

const http2 = require("http2");
const path = require("path");

process.env.SAND_RPC_TEST = "1";
require(path.join(__dirname, "sand_rpc.js"));

const t = global.__sandTest;
const RUN_INFERENCE_PATH = "/aiserver.v1.InferenceService/RunInference";
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

function probeRunInference(options) {
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
  const conversationId = options.conversationId || require("crypto").randomUUID();
  const invocationId = options.invocationId || require("crypto").randomUUID();
  const agentMsg = buildAgentMessage(modelId, maxMode, prompt, conversationId);
  const spec = t.spec(agentMsg);
  const started = Date.now();

  const headers = {
    ":method": "POST",
    ":path": RUN_INFERENCE_PATH,
    ":scheme": "https",
    ":authority": HOST,
    authorization: `Bearer ${jwt}`,
    "content-type": "application/connect+proto",
    "connect-protocol-version": "1",
    "x-cursor-client-type": clientType,
    "x-cursor-client-version": version,
    "x-cursor-streaming": "true",
    "user-agent": "sand-verify/run-inference-probe",
  };
  if (commit) {
    headers["x-cursor-client-commit"] = commit;
  }
  if (clientType === "sand") {
    headers["x-sand-box-namespace"] = "prod";
  }

  return new Promise((resolve) => {
    const client = http2.connect(`https://${HOST}`);
    let settled = false;
    let ttfbMs = null;
    let text = "";
    let error = "";
    let connectCode = "";
    let leftover = Buffer.alloc(0);
    let runReady = false;
    let invokeSent = false;
    let finishSent = false;

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
        path: "InferenceService/RunInference",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "timeout",
        detail: `timed out after ${timeoutMs}ms`,
      });
    }, timeoutMs);

    client.on("error", (err) => {
      finish({
        ok: false,
        path: "InferenceService/RunInference",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "network_error",
        detail: String(err.message || err),
      });
    });

    const req = client.request(headers);
    req.setEncoding("binary");

    req.on("response", (responseHeaders) => {
      const status = Number(responseHeaders[":status"] || 0);
      if (status && status !== 200) {
        finish({
          ok: false,
          path: "InferenceService/RunInference",
          ttfb_ms: ttfbMs,
          text_preview: "",
          error_code: `http_${status}`,
          detail: `HTTP ${status}`,
        });
      }
    });

    req.on("data", (chunk) => {
      if (ttfbMs === null) {
        ttfbMs = Date.now() - started;
      }
      leftover = Buffer.concat([
        leftover,
        Buffer.from(chunk, "binary"),
      ]);
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
                String(dbg.error || dbg.details || err.message || error) || error;
            } else if (err.message) {
              error = String(err.message);
            }
          } catch (_) {
            error = frame.trailer.toString("utf8", 0, 400);
          }
          return;
        }
        const decoded = t.decRun(frame.data);
        if (decoded.runReady && !runReady) {
          runReady = true;
          if (!invokeSent) {
            invokeSent = true;
            req.write(
              connectFrame(Buffer.from(t.encInvoke(spec, invocationId)))
            );
          }
        }
        if (decoded.text) {
          text += decoded.text;
        }
        if (decoded.err) {
          error = decoded.err;
        }
        if (runReady && invokeSent && !finishSent && (text || error)) {
          finishSent = true;
          req.write(connectFrame(Buffer.from(t.encFinish())));
          req.end();
        }
      });
    });

    req.on("end", () => {
      const ok = Boolean(text.trim()) && !error;
      finish({
        ok,
        path: "InferenceService/RunInference",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: error || connectCode || (ok ? "" : "empty_response"),
        detail: error || connectCode || "",
      });
    });

    req.on("error", (err) => {
      finish({
        ok: false,
        path: "InferenceService/RunInference",
        ttfb_ms: ttfbMs,
        text_preview: text.slice(0, 200),
        error_code: "read_error",
        detail: String(err.message || err),
      });
    });

    req.write(connectFrame(Buffer.from(t.encRun(spec))));
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
      "usage: node run_inference_probe.js <jwt> <model> <clientType> <version> <commit> <maxMode> <prompt> [timeoutMs]"
    );
    process.exit(2);
  }
  const result = await probeRunInference({
    jwt,
    modelId,
    clientType: clientType || "sand",
    version: version || "0.44.0",
    commit: commit || "",
    maxMode: maxModeArg === "true",
    prompt: prompt || "Reply with exactly: pong",
    timeoutMs: Number(timeoutArg || 45000),
  });
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) {
  main().catch((err) => {
    process.stdout.write(
      JSON.stringify({
        ok: false,
        path: "InferenceService/RunInference",
        error_code: "probe_error",
        detail: String(err.message || err),
      })
    );
    process.exit(1);
  });
}

module.exports = { probeRunInference };
