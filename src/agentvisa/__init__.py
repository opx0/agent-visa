"""Agent Visa: a permissioned context protocol for AI agents.

An app requests exact passport fields with a purpose and a duration, the person approves,
and the agent reads scoped context. Every read leaves a receipt and any grant can be revoked.
A pass holder may issue a transit visa to a sub-agent: always a subset, always shorter-lived,
and revoked in cascade with its parent.

Start reading at policy.py. Every authorization decision is a pure function in that one file.
"""

__version__ = "0.1.0"
