"""A minimal stdio MCP server for tests: answers initialize, then
gmail_send_email with (argv[1]) "ok" -> success text, "error" -> isError,
"hang" -> never answers the tool call."""
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

for line in sys.stdin:
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in req:
        continue  # notification
    if req["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {},
               "serverInfo": {"name": "fake", "version": "0"}}
    elif req["method"] == "tools/call":
        args = req["params"]["arguments"]
        if len(sys.argv) > 2 and sys.argv[2] == "single":
            assert "userId" not in args, args
        else:
            assert args.get("userId") == "user_1", args
        if mode == "hang":
            time.sleep(60)
            continue
        if mode == "error":
            res = {"isError": True, "content": [{"type": "text", "text": "❌ nope"}]}
        else:
            res = {"content": [{"type": "text", "text": f"✅ sent to {args['to']}"}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}) + "\n")
    sys.stdout.flush()
