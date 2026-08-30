import re
import dns.resolver

def validate_email(email: str) -> str:
    """
    Validates an email address.
    Checks:
    1. Syntax (regex check)
    2. Domain MX (Mail Exchange) record resolution
    
    Returns: "Valid", "Invalid", or "N/A"
    """
    if not email or not isinstance(email, str) or not email.strip():
        return "N/A"
    if email.strip().upper() in ["N/A", "NONE", "UNKNOWN", "NULL", "UNDEFINED"]:
        return "N/A"
        
    # 1. Advanced Sanitization
    email = email.strip()
    
    # Strip enclosing angle or square brackets
    if email.startswith("<") and email.endswith(">"):
        email = email[1:-1].strip()
    if email.startswith("[") and email.endswith("]"):
        email = email[1:-1].strip()
        
    # Extract from mailto: protocol
    if email.lower().startswith("mailto:"):
        email = email[7:].strip()
        
    # Extract from markdown link formatting: [label](mailto:email)
    markdown_match = re.match(r'\[.*?\]\(mailto:(.*?)\)', email, re.IGNORECASE)
    if markdown_match:
        email = markdown_match.group(1).strip()
        
    # Remove accidental trailing punctuation (dots, commas, colons)
    email = re.sub(r'[\.,;:]+$', '', email).strip()
    
    # 2. Syntax Regex Check
    syntax_regex = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    if not re.match(syntax_regex, email):
        return "Invalid"
        
    # 3. DNS MX / A Record Check
    domain = email.split("@")[-1]
    try:
        # Resolve Mail Exchanger (MX) records
        answers = dns.resolver.resolve(domain, 'MX')
        if answers:
            return "Valid"
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN, dns.exception.Timeout):
        # Fallback: check if the domain resolves to an IP address (A record)
        try:
            a_answers = dns.resolver.resolve(domain, 'A')
            if a_answers:
                return "Valid"
        except Exception:
            return "Invalid"
    except Exception:
        # For other errors (like network/DNS resolution issues), return N/A rather than marking valid/invalid
        return "N/A"
        
    return "Invalid"
