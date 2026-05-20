"""
Unique tenant slug generation to avoid collisions.
"""
import re
from django.utils.text import slugify
from tenants.models import Tenant


def generate_unique_slug(base_name, max_length=50):
    """
    Generate unique slug from base name.
    
    Examples:
        "Acme Corp" → "acme-corp"
        "Acme Corp" (exists) → "acme-corp-abc123"
        "Acme Corp" (exists) → "acme-corp-xyz789"
    
    Args:
        base_name: Base name to slug
        max_length: Maximum slug length
        
    Returns:
        str: Unique slug
    """
    # Create initial slug
    slug = slugify(base_name)[:max_length]
    
    # Check if slug exists
    if not Tenant.objects.filter(slug=slug).exists():
        return slug
    
    # Slug exists - append random suffix
    import secrets
    suffix = secrets.token_hex(3)  # 6 character hex
    
    # Ensure total length doesn't exceed max
    available_length = max_length - len(suffix) - 1  # -1 for hyphen
    slug = slugify(base_name)[:available_length]
    
    new_slug = f"{slug}-{suffix}"
    
    # Double-check for collision (extremely unlikely)
    while Tenant.objects.filter(slug=new_slug).exists():
        suffix = secrets.token_hex(3)
        new_slug = f"{slug}-{suffix}"
    
    return new_slug