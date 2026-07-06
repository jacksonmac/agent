"""Execute -> review -> retry agent harness for a local Ollama server.

Usage:
    python3 agent.py -g "A script fizzbuzz.py that prints FizzBuzz for 1-30"
    python3 agent.py -sg "I need a folder called output with a readme in it"
    python3 agent.py -g "..." --model qwen3.5:9b --attempts 3 --num-ctx 32768
    python3 agent.py -g "..." --full-context --num-ctx 65536
    python3 agent.py -g "..." --mcp                    # + Docker MCP Toolkit tools
    python3 agent.py -g "..." --mcp --mcp-profile dev  # a specific Toolkit profile

The implementation lives in the harness/ package; this file is just the
entry-point shim so the historical invocation keeps working.
"""

import os
import sys

# make `python3 path/to/agent.py` work from any directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness.cli import main

if __name__ == "__main__":
    main()
