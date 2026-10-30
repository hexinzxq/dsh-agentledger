"""AgentLedger — local-first session memory for coding agents.

The package is deliberately small: storage + extraction + retrieval + a thin
server. Nothing here talks to the network unless you explicitly configure a
remote embedding model.
"""

__version__ = "0.1.0"
