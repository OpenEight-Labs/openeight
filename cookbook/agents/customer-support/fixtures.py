"""Synthetic retailer schema and seed data; never a real service adapter."""

from datetime import datetime, timedelta
from local import (
    DEPLOYMENT_SQL, connection, protected_directory,
    transaction, install_deployment, now,
)
from money import decimal_text, money

RETAILER_SQL = (
    DEPLOYMENT_SQL
    + """
CREATE TABLE customers (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL,
 created_at TEXT NOT NULL, name TEXT NOT NULL, recipient TEXT NOT NULL,
 PRIMARY KEY(deployment_id,id));
CREATE TABLE orders (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, created_at TEXT NOT NULL,
 customer_id TEXT NOT NULL, sku TEXT NOT NULL, product_category TEXT NOT NULL CHECK(product_category IN ('standard','perishable')),
 quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0), currency TEXT NOT NULL,
 total_amount TEXT NOT NULL, captured_amount TEXT NOT NULL, refunded_amount TEXT NOT NULL,
 replacement_value TEXT NOT NULL, fulfilment_status TEXT NOT NULL CHECK(fulfilment_status IN ('unfulfilled','packing','shipped','delivered','cancelled')),
 purchased_at TEXT NOT NULL, delivered_at TEXT, revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND 9007199254740991),
 allocated_action_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(deployment_id,id),
 FOREIGN KEY(deployment_id,customer_id) REFERENCES customers(deployment_id,id),
 FOREIGN KEY(deployment_id,allocated_action_id) REFERENCES actions(deployment_id,id));
CREATE TABLE cases (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, created_at TEXT NOT NULL,
 order_id TEXT NOT NULL, customer_report TEXT NOT NULL, verified_issue TEXT CHECK(verified_issue IN ('damaged','lost','wrong_item','quality','change_of_mind','billing','other')),
 status TEXT NOT NULL CHECK(status IN ('open','waiting_customer','waiting_approval','waiting_fulfilment','resolved','handed_off')),
 updated_at TEXT NOT NULL, PRIMARY KEY(deployment_id,id), FOREIGN KEY(deployment_id,order_id) REFERENCES orders(deployment_id,id));
CREATE UNIQUE INDEX unfinished_case ON cases(deployment_id,order_id) WHERE status NOT IN ('resolved','handed_off');
CREATE TABLE actions (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, created_at TEXT NOT NULL,
 case_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('refund','replacement','return','cancellation')),
 parent_action_id TEXT, amount TEXT, currency TEXT, shipping_method TEXT CHECK(shipping_method IN ('standard','expedited')),
 reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('confirmed','superseded','requested','dispatched','received','rejected','awaiting_refund','completed')),
 confirmation_command_id TEXT NOT NULL, confirmed_at TEXT NOT NULL, completed_at TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(deployment_id,id), UNIQUE(deployment_id,confirmation_command_id),
 FOREIGN KEY(deployment_id,case_id) REFERENCES cases(deployment_id,id),
 FOREIGN KEY(deployment_id,parent_action_id) REFERENCES actions(deployment_id,id),
 CHECK((kind IN ('refund','replacement'))=(amount IS NOT NULL AND currency IS NOT NULL)),
 CHECK((amount IS NULL)=(currency IS NULL)), CHECK((kind='replacement')=(shipping_method IS NOT NULL)),
 CHECK(parent_action_id IS NULL OR kind='refund'), CHECK((status='completed')=(completed_at IS NOT NULL)));
CREATE UNIQUE INDEX confirmed_primary ON actions(deployment_id,case_id) WHERE status='confirmed' AND parent_action_id IS NULL;
CREATE UNIQUE INDEX live_child ON actions(deployment_id,parent_action_id) WHERE parent_action_id IS NOT NULL AND status!='superseded';
CREATE TABLE receipts (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, created_at TEXT NOT NULL,
 case_id TEXT NOT NULL, action_id TEXT, kind TEXT NOT NULL, idempotency_key TEXT NOT NULL,
 request_json TEXT NOT NULL CHECK(json_valid(request_json)), result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 facts_json TEXT NOT NULL CHECK(json_valid(facts_json)), amount TEXT, currency TEXT,
 PRIMARY KEY(deployment_id,id), UNIQUE(deployment_id,idempotency_key), CHECK((amount IS NULL)=(currency IS NULL)),
 FOREIGN KEY(deployment_id,case_id) REFERENCES cases(deployment_id,id), FOREIGN KEY(deployment_id,action_id) REFERENCES actions(deployment_id,id));
CREATE UNIQUE INDEX action_transition ON receipts(deployment_id,action_id,kind) WHERE action_id IS NOT NULL AND kind IN ('refund','replacement','return','cancellation','dispatch','delivery','receipt','accept','reject');
CREATE UNIQUE INDEX case_closure ON receipts(deployment_id,case_id) WHERE kind='closure';
CREATE TABLE messages (
 deployment_id TEXT NOT NULL REFERENCES deployment(id), id TEXT NOT NULL, created_at TEXT NOT NULL,
 case_id TEXT NOT NULL, action_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('note','escalation')),
 body TEXT NOT NULL, recipient TEXT, receipt_id TEXT NOT NULL, PRIMARY KEY(deployment_id,id), UNIQUE(deployment_id,receipt_id),
 CHECK(recipient IS NULL), FOREIGN KEY(deployment_id,case_id) REFERENCES cases(deployment_id,id),
 FOREIGN KEY(deployment_id,action_id) REFERENCES actions(deployment_id,id), FOREIGN KEY(deployment_id,receipt_id) REFERENCES receipts(deployment_id,id));
CREATE TRIGGER permanent_allocation BEFORE UPDATE OF allocated_action_id ON orders
 WHEN OLD.allocated_action_id IS NOT NULL AND NEW.allocated_action_id IS NOT OLD.allocated_action_id
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER immutable_action_payload BEFORE UPDATE ON actions
 WHEN NEW.deployment_id IS NOT OLD.deployment_id OR NEW.id IS NOT OLD.id OR NEW.case_id IS NOT OLD.case_id
 OR NEW.kind IS NOT OLD.kind OR NEW.parent_action_id IS NOT OLD.parent_action_id OR NEW.amount IS NOT OLD.amount
 OR NEW.currency IS NOT OLD.currency OR NEW.shipping_method IS NOT OLD.shipping_method OR NEW.reason IS NOT OLD.reason
 OR NEW.confirmation_command_id IS NOT OLD.confirmation_command_id OR NEW.confirmed_at IS NOT OLD.confirmed_at
 OR NEW.created_at IS NOT OLD.created_at BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER immutable_order_facts BEFORE UPDATE ON orders
 WHEN NEW.customer_id IS NOT OLD.customer_id OR NEW.sku IS NOT OLD.sku OR NEW.product_category IS NOT OLD.product_category
 OR NEW.quantity IS NOT OLD.quantity OR NEW.currency IS NOT OLD.currency OR NEW.total_amount IS NOT OLD.total_amount
 OR NEW.replacement_value IS NOT OLD.replacement_value OR NEW.purchased_at IS NOT OLD.purchased_at
 OR NEW.created_at IS NOT OLD.created_at OR NEW.id IS NOT OLD.id OR NEW.deployment_id IS NOT OLD.deployment_id
 BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""
)

SCENARIOS = (
    ("small", 25, "delivered", "damaged", "A damaged small order"),
    ("damaged", 180, "delivered", "damaged", "An item arrived damaged"),
    ("excessive", 250, "delivered", "damaged", "An expensive damaged order"),
    ("missing", 80, "shipped", "lost", "A missing delivery"),
    ("delayed", 60, "shipped", "other", "A delayed delivery"),
    ("wrong", 120, "delivered", "wrong_item", "The wrong item delivered"),
    ("billing", 90, "delivered", "billing", "I was charged twice for the same order"),
    ("cancel", 70, "unfulfilled", "change_of_mind", "Cancel an order"),
    ("packing", 45, "packing", "change_of_mind", "An order being packed"),
    ("perishable", 50.01, "delivered", "quality", "A food quality complaint"),
    ("return31", 50, "delivered", "damaged", "A previously delivered order"),
    ("returnzero", 25, "delivered", "other", "A settled order return"),
    ("cancelzero", 25, "packing", "change_of_mind", "An uncaptured order"),
    ("jpy", 1250, "delivered", "damaged", "A Japanese yen order"),
    ("kwd", 50.125, "delivered", "damaged", "A Kuwaiti dinar order"),
)

BILLING_CAPTURES = (
    {"id": "capture_billing_1", "amount": 90, "currency": "USD"},
    {"id": "capture_billing_2", "amount": 90, "currency": "USD"},
)


def insert(db, table, values):
    db.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def seed_retailer(state, deployment):
    state = protected_directory(state)
    with connection(state / "retailer.sqlite") as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='deployment'"
        ).fetchone():
            db.executescript(RETAILER_SQL)
        with transaction(db):
            install_deployment(db, deployment)
            if not db.execute("SELECT 1 FROM cases").fetchone():
                ts = now()
                for label, total, fulfilment, issue, subject in SCENARIOS:
                    currency = {"jpy": "JPY", "kwd": "KWD"}.get(label, "USD")
                    amount = decimal_text(money(total, currency, positive=True))
                    common = {"deployment_id": deployment.id, "created_at": ts}
                    insert(
                        db,
                        "customers",
                        {
                            **common,
                            "id": "customer_" + label,
                            "name": "Synthetic " + label,
                            "recipient": label + "@example.invalid",
                        },
                    )
                    delivered = (
                        (
                            datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            - timedelta(days=31 if label == "return31" else 30)
                        ).isoformat()
                        if fulfilment == "delivered"
                        else None
                    )
                    insert(
                        db,
                        "orders",
                        {
                            **common,
                            "id": "order_" + label,
                            "customer_id": "customer_" + label,
                            "sku": "sku_" + label,
                            "product_category": "perishable"
                            if label == "perishable"
                            else "standard",
                            "quantity": 1,
                            "currency": currency,
                            "total_amount": amount,
                            "captured_amount": "0" if label == "cancelzero" else amount,
                            "refunded_amount": amount if label == "returnzero" else "0",
                            "replacement_value": amount,
                            "fulfilment_status": fulfilment,
                            "purchased_at": (
                                datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                - timedelta(days=40)
                            ).isoformat(),
                            "delivered_at": delivered,
                            "revision": 1,
                            "allocated_action_id": None,
                            "updated_at": ts,
                        },
                    )
                    insert(
                        db,
                        "cases",
                        {
                            **common,
                            "id": "case_" + label,
                            "order_id": "order_" + label,
                            "customer_report": subject,
                            "verified_issue": issue,
                            "status": "open",
                            "updated_at": ts,
                        },
                    )
        for table in ("deployment", "customers", "receipts", "messages"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable'); END"
                )
