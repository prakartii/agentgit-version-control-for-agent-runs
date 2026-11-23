"""A minimal stdio JSON-RPC server: answers every request with a fixed small result.

It does no work, so any time measured around it is the cost of the transport
(and, in the proxy benchmark, of the proxy).
"""

import json
import sys

REPLY = {"content": [{"type": "text", "text": "ok"}]}

for line in sys.stdin.buffer:
    message = json.loads(line)
    if "id" in message:
        out = {"jsonrpc": "2.0", "id": message["id"], "result": REPLY}
        sys.stdout.buffer.write(json.dumps(out, separators=(",", ":")).encode() + b"\n")
        sys.stdout.buffer.flush()
