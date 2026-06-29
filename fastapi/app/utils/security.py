from urllib.parse import urlparse
import ipaddress

ALLOWED_DESTINATION_DOMAINS = {
    "events.pagerduty.com",
    "hooks.slack.com",
}

def is_safe_webhook_url(url: str) -> bool:
    """
    Validates that a webhook URL is safe to dispatch HTTP requests to.
    Prevents SSRF vulnerabilities by enforcing an allowlist of domains
    and blocking resolution to loopback or private IPs.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
        
    if parsed.scheme not in {"http", "https"}:
        return False

    if parsed.hostname not in ALLOWED_DESTINATION_DOMAINS:
        return False

    # Block private/loopback IPs even if hostname resolves to one.
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            return False
    except ValueError:
        pass
        
    return True
