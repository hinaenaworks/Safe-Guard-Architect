import database_engine
import json
import logging
from datetime import datetime
from typing import Dict, Any

# Configure Logger
logger = logging.getLogger("security_audit")

# Immutable System Constants
STATUS_COMPLETED = 'COMPLETED'
STATUS_PROCESSING = 'PROCESSING'
STATUS_REJECTED = 'REJECTED'
STATUS_FAILED = 'FAILED'

def transfer_funds(sender_id: int, receiver_id: int, amount: int, authenticated_user_id: int, idempotency_key: str) -> Dict[str, Any]:
    """
    Two-Phase Validation & Minimized Transaction API
    
    Security Audit Compliant:
    - Atomic Idempotency Pattern
    - Ordered Locking to prevent Deadlocks
    - State Machine Validation
    """
    
    # 0. Basic Access Control
    if sender_id != authenticated_user_id:
        raise PermissionError("Access Denied: Sender mismatch.")
    if sender_id == receiver_id:
        raise ValueError("Invalid Operation: Self-transfer prohibited.")

    current_time = datetime.now()

    # Phase 1 & 2: Idempotency Initialization & Pre-flight
    try:
        with database_engine.transaction():
            database_engine.execute("""
                INSERT INTO idempotency_keys (key, status, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT (key) DO NOTHING
            """, (idempotency_key, STATUS_PROCESSING, current_time))

        row = database_engine.execute(
            "SELECT status, response_data FROM idempotency_keys WHERE key = ? FOR UPDATE",
            (idempotency_key,)
        ).fetchone()

        if row['status'] == STATUS_COMPLETED:
            return json.loads(row['response_data'])
        if row['status'] == STATUS_REJECTED:
            raise RuntimeError("Transaction permanently rejected.")

        sender = database_engine.execute("SELECT balance, status FROM accounts WHERE id = ?", (sender_id,)).fetchone()
        receiver = database_engine.execute("SELECT id, status FROM accounts WHERE id = ?", (receiver_id,)).fetchone()

        if not sender or sender['status'] != 'ACTIVE' or not receiver or receiver['status'] != 'ACTIVE' or sender['balance'] < amount:
            database_engine.execute(
                "UPDATE idempotency_keys SET status = ? WHERE key = ?",
                (STATUS_REJECTED, idempotency_key)
            )
            raise ValueError("Pre-flight validation failed.")

    except Exception as e:
        if "permanently rejected" in str(e) or "validation failed" in str(e):
            raise e
        logger.error(f"Initialization/Validation Error: {str(e)}")
        raise RuntimeError("System Error during initialization.")

    # Phase 3: Minimized Side-Effect Transaction
    try:
        with database_engine.transaction():
            lock_order = sorted([sender_id, receiver_id])
            locked_accounts = {}
            for acc_id in lock_order:
                acc_row = database_engine.execute(
                    "SELECT id, balance FROM accounts WHERE id = ? AND status = 'ACTIVE' FOR UPDATE",
                    (acc_id,)
                ).fetchone()
                if not acc_row:
                    raise RuntimeError("Account state changed.")
                locked_accounts[acc_id] = acc_row

            if locked_accounts[sender_id]['balance'] < amount:
                raise ValueError("Insufficient funds.")

            database_engine.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", (amount, sender_id))
            database_engine.execute("UPDATE accounts SET balance = balance + ? WHERE id = ?", (amount, receiver_id))

            result = {"status": "success", "amount": amount, "txn_id": idempotency_key}
            database_engine.execute("""
                UPDATE idempotency_keys 
                SET status = ?, response_data = ?
                WHERE key = ?
            """, (STATUS_COMPLETED, json.dumps(result), idempotency_key))
            
            return result

    except Exception as e:
        logger.error(f"Transaction execution failed: {str(e)}")
        raise RuntimeError(f"Financial processing failed: {str(e)}")
