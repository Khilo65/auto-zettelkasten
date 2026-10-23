from decimal import Decimal

import pytest

from auto_zettelkasten.jev_batches import BatchRequest, JevBatchClient, batch_cache_key
from auto_zettelkasten.jev_billing import BATCH_BILLING_POLICY
from auto_zettelkasten.jev_decisions import JevConfig
from test_jev_batches import _pairs, _store


@pytest.mark.parametrize("count", [65, 256])
def test_approval_allowlist_supports_bounded_relationship_jobs(count):
    keys = tuple(f"{index:064x}" for index in range(count))
    JevConfig(approved_cache_keys=keys).validate()


def test_approval_allowlist_rejects_more_than_one_bounded_job():
    keys = tuple(f"{index:064x}" for index in range(257))
    with pytest.raises(ValueError, match="at most 256"):
        JevConfig(approved_cache_keys=keys).validate()


def test_larger_allowlist_does_not_enable_paid_calls(tmp_path):
    request = BatchRequest((_pairs()[0],))
    keys = (batch_cache_key(request), *(f"{index:064x}" for index in range(255)))
    config = JevConfig(
        enabled=True, api_key="synthetic-test-only", budget_usd=Decimal("0.05"),
        data_sharing_confirmed=True, paid_call_approved=False,
        approved_billing_policy_identity=BATCH_BILLING_POLICY.identity,
        approved_cache_keys=keys,
    )
    client = JevBatchClient(config, _store(tmp_path),
                            transport=lambda *args, **kwargs: pytest.fail("paid approval remains required"))
    result = client.assess(request, dry_run=False)
    assert result["status"] == "blocked"
    assert result["provider_calls"] == 0
    assert "paid-call approval" in result["reason"]
