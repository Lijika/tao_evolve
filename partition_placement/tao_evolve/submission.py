"""Idempotent submission driver via an injected, verified public-API transport.

This module does not guess the competition AgentContext method signatures.
Transport.lookup() must return None ONLY for a confirmed absent request, and
Transport.submit() must bind a caller-supplied request ID with server-side
idempotency. Client binding is a local integration task.
"""
from typing import Mapping, Optional, Protocol
from .journal import IntentJournal


class SubmissionTransport(Protocol):
    idempotent_submission_verified: bool

    def lookup(self, request_id: str) -> Optional[Mapping]:
        ...

    def submit(self, checkout_id: str, request_id: str) -> Mapping:
        ...


class SubmissionDriver:
    def __init__(self, journal: IntentJournal, transport: SubmissionTransport):
        if not transport.idempotent_submission_verified:
            raise ValueError("Public request-ID binding has not been verified")
        self.journal, self.transport = journal, transport

    def step(self, checkout_id: str, logical_key: str, workspace_digest: str):
        if len(workspace_digest) != 64 or any(c not in "0123456789abcdef" for c in workspace_digest):
            raise ValueError("A frozen local workspace SHA-256 is required")
        rid = self.journal.prepare("submit", logical_key, {"checkout_id": checkout_id, "workspace_digest": workspace_digest})
        local = self.journal.get(rid)
        if local["state"] in {"succeeded", "failed", "rejected"}:
            return local["response"]
        # Always reconcile first. Unknown/network failure is NOT confirmed absence.
        try:
            observed = self.transport.lookup(rid)
        except Exception:
            self.journal.record(rid, "uncertain")
            raise
        if observed is not None:
            self._accept(rid, observed)
            return observed
        # The only permitted retry repeats this ID and exactly the same checkout.
        self.journal.record(rid, "sending")
        try:
            receipt = self.transport.submit(checkout_id, rid)
        except Exception:
            self.journal.record(rid, "uncertain")
            raise
        self._accept(rid, receipt)
        return receipt

    def _accept(self, rid, receipt):
        if receipt.get("request_id") != rid:
            self.journal.record(rid, "uncertain")
            raise ValueError("Mismatched official receipt")
        state = receipt.get("status")
        if state not in {"accepted", "running", "uncertain", "reconciling", "succeeded", "failed", "rejected"}:
            self.journal.record(rid, "uncertain")
            raise ValueError("Unknown official status")
        self.journal.record(rid, state, receipt)
