"""server_mcp.py — alias entry point.

The design/README refer to ``server_mcp.py``; the implementation lives in
``server.py``. This thin shim lets either path work as the MCP command:

    python3.12 /ABS/PATH/toss-mcp/server_mcp.py
    python3.12 /ABS/PATH/toss-mcp/server.py
"""

from server import main

if __name__ == "__main__":
    main()
