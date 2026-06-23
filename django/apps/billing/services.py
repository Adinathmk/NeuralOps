# Compatibility shim: razorpay<=1.4.2 does `from pkg_resources import ...`
# at module load time, which fails on python:3.12-slim where setuptools
# (and thus pkg_resources) is not present. We inject a minimal shim into
# sys.modules BEFORE importing the SDK so the import succeeds.
import sys
import importlib.metadata

if "pkg_resources" not in sys.modules:
    import types as _types

    class _DistributionNotFound(Exception):
        pass

    class _Dist:
        def __init__(self, name):
            try:
                self.version = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                self.version = "0.0.0"

    _shim = _types.ModuleType("pkg_resources")
    _shim.DistributionNotFound = _DistributionNotFound
    _shim.get_distribution = _Dist
    _shim.require = lambda *a, **kw: None
    sys.modules["pkg_resources"] = _shim

import razorpay
from django.conf import settings

client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

def get_or_create_customer(tenant):
    if tenant.razorpay_customer_id:
        return tenant.razorpay_customer_id

    customer_data = {
        "name": tenant.name,
        "notes": {"tenant_id": str(tenant.id)}
    }
    customer = client.customer.create(data=customer_data)
    tenant.razorpay_customer_id = customer['id']
    tenant.save(update_fields=['razorpay_customer_id'])
    return customer['id']

def create_subscription(tenant, plan_id, plan_tier):
    customer_id = get_or_create_customer(tenant)
    subscription_data = {
        "plan_id": plan_id,
        "customer_id": customer_id,
        "total_count": 120,  # Arbitrary large number for recurring subscription
        "notes": {
            "tenant_id": str(tenant.id),
            "plan_tier": plan_tier
        }
    }
    return client.subscription.create(data=subscription_data)

def verify_payment_signature(payment_id, subscription_id, signature):
    params_dict = {
        'razorpay_payment_id': payment_id,
        'razorpay_subscription_id': subscription_id,
        'razorpay_signature': signature
    }
    return client.utility.verify_payment_signature(params_dict)
