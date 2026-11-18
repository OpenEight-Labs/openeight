"""Investigate and roll back the local sample service; no infrastructure is contacted."""

from dataclasses import asdict
import json
from uuid import uuid4

from contracts import Mutation
from simulator import IncidentSimulator, canonical, initial_snapshot

service = IncidentSimulator(initial=initial_snapshot(), known_releases=("v41", "v42"))
before = service.snapshot()
logs = service.read_logs(service_id=before.service_id)
metrics_before = service.read_metrics(service_id=before.service_id)
receipt = service.apply(
    Mutation(
        str(uuid4()),
        before.service_id,
        "rollback_release",
        before.revision,
        canonical({"target_release": "v41"}),
    )
)
print(
    json.dumps(
        {
            "mode": "local_business_sample",
            "governance": "not_exercised",
            "logs": json.loads(logs.facts_json),
            "metrics_before": json.loads(metrics_before.facts_json),
            "receipt": asdict(receipt),
            "metrics_after": json.loads(
                service.read_metrics(service_id=before.service_id).facts_json
            ),
        },
        indent=2,
    )
)
