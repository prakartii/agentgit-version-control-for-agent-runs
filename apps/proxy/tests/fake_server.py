"""A scriptable stdlib-only stdio "MCP server" for proxy tests.

It is deliberately not built on the MCP SDK: tests need to control the exact
bytes it writes. Behaviour is chosen per request through ``params.arguments``:

  reply_raw   write this exact string (plus newline) as the response line
  sleep       seconds to wait before replying
  crash       exit with code 3 without replying
  progress    send a progress notification (using the request's progressToken)
  is_error    reply with a result whose isError is true
  error       reply with a JSON-RPC error

Environment:
  RECEIVED_LOG  file that receives every raw line read from stdin, verbatim
  IGNORE_EOF    if set, keep running after stdin closes (to test termination)
"""

import json
import os
import sys
import time

received_log = os.environ.get("RECEIVED_LOG")
print("fake-server-started", file=sys.stderr, flush=True)

out = sys.stdout.buffer


def send(obj: object) -> None:
    out.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
    out.flush()


for raw in sys.stdin.buffer:
    if received_log:
        with open(received_log, "ab") as log:
            log.write(raw)
    try:
        msg = json.loads(raw)
    except ValueError:
        continue
    if not isinstance(msg, dict) or "method" not in msg or "id" not in msg:
        continue  # notifications and junk get no reply

    params = msg.get("params") or {}
    args = params.get("arguments") or {}
    msg_id = msg["id"]

    if args.get("crash"):
        sys.exit(3)
    if args.get("sleep"):
        time.sleep(float(args["sleep"]))
    if args.get("progress"):
        token = (params.get("_meta") or {}).get("progressToken")
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "progress": 1, "total": 2},
            }
        )
    if "reply_raw" in args:
        out.write(args["reply_raw"].encode() + b"\n")
        out.flush()
    elif args.get("error"):
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": "bad"}})
    else:
        send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": "echo:" + str(params.get("name"))}],
                    "isError": bool(args.get("is_error")),
                    "resultType": "complete",
                },
            }
        )

if os.environ.get("IGNORE_EOF"):
    while True:
        time.sleep(0.1)
