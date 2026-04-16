"""Regression tests for CiscoConfigSanitizer.

The sanitizer must (a) redact secret material without leaking it into any output
group and (b) leave non-secret lines alone even when they contain words like
"password", "key", or "secret" in descriptions or command names.
"""
import re

import pytest

from sanitizer import CiscoConfigSanitizer, load_rules


RULES = {
    "sanitize": {
        "hostname": False,
        "ip_addresses": True,
        "usernames": True,
        "snmp_communities": True,
        "enable_secrets": True,
        "tacacs_radius_keys": True,
        "crypto_keys": True,
        "email_addresses": True,
    }
}

# Canonical synthetic hex key material used across multiple tests — if any
# test output still contains this string, the sanitizer leaked it. This value
# is deliberately not a valid Cisco type-7 ciphertext; the tests only care
# that the exact string never appears in the output.
LEAK_MARKER = "DEADBEEFCAFEBABEDEADBEEFCAFEBABEDEADBEEF"


@pytest.fixture
def sanitizer():
    return CiscoConfigSanitizer(RULES)


def _run(sanitizer, line):
    return sanitizer.sanitize(line)


# ---------------------------------------------------------------------------
# Leak regression: every secret-bearing line must be sanitized such that the
# original secret value never appears in the output.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "enable secret 5 $1$abcd$xyzLEAKxyz",
    "enable password 7 14141B180F0B",
    "username admin privilege 15 password 7 14141B180F0B",
    "username admin secret 5 $1$abcd$xyzLEAKxyz",
    "snmp-server community PUBLICSTRING RO 48",
    "snmp-server host 192.0.2.1 version 2c MYCOMMUNITY udp-port 5146",
    "tacacs-server key 7 ABCDEF0123",
    "radius-server key 7 ABCDEF0123",
    "neighbor 192.0.2.1 password 7 ABCDEF0123",
    " ip ospf authentication-key 7 ABCDEF0123",
    " ip ospf message-digest-key 1 md5 7 ABCDEF0123",
    "crypto isakmp key MYPSKVALUE address 192.0.2.1",
    " ppp chap password 7 ABCDEF0123",
    " wpa-psk ascii 7 ABCDEF0123",
    "  key-string 7 ABCDEF0123",
])
def test_secret_value_never_appears_in_output(sanitizer, line):
    """The actual secret string must not survive to the output. IP
    substitution is allowed; we strip IP tokens before checking for the secret."""
    out = _run(sanitizer, line)
    # Remove IP tokens so their substitution doesn't hide what we're checking.
    out_sans_ip = re.sub(r"IP_\d+", "", out)
    # Extract every literal "secret-ish" token from the input (anything
    # that looks like a password/key/community — excluding Cisco keywords).
    secrets_in_input = re.findall(
        r"\$1\$\S+|[A-Za-z0-9_]{8,}",
        line,
    )
    cisco_keywords = {
        "snmp-server", "community", "version", "tacacs-server", "radius-server",
        "neighbor", "password", "secret", "authentication-key", "message-digest-key",
        "address", "username", "privilege", "crypto", "isakmp", "key-string",
        "enable", "ospf", "chap", "wpa-psk", "ascii", "ppp", "udp-port",
        "admin", "ABCDEF0123",  # exclude short identifiers we handle separately
    }
    # If ANY non-keyword secret from input still appears in output, it's a leak.
    leaks = [s for s in secrets_in_input
             if s not in cisco_keywords
             and len(s) >= 8
             and s in out_sans_ip
             and not s.startswith("REDACTED")
             and s not in {"PUBLICSTRING", "MYCOMMUNITY", "MYPSKVALUE"}  # redacted by name tests below
             ]
    # Dedicated check for any of our well-known secret markers
    known_leaks = ["$1$abcd$xyzLEAKxyz", "14141B180F0B", "PUBLICSTRING",
                   "MYCOMMUNITY", "ABCDEF0123", "MYPSKVALUE"]
    for marker in known_leaks:
        if marker in line:
            assert marker not in out, (
                f"LEAK: secret {marker!r} still present in output {out!r} "
                f"for input {line!r}"
            )


def test_tacacs_server_nested_key_block(sanitizer):
    """Regression for the real-world bug: nested 'key 7 HEXMATERIAL' under a
    'tacacs server NAME' block was redacting the '7' encryption-type and
    leaving the real key material exposed."""
    config = (
        "tacacs server GTAC1\n"
        " address ipv4 192.0.2.1\n"
        f" key 7 {LEAK_MARKER}\n"
        " port 49162\n"
    )
    out = sanitizer.sanitize(config)
    assert LEAK_MARKER not in out, (
        f"Nested AAA key leaked: {LEAK_MARKER} still present in output:\n{out}"
    )
    # Encryption type digit should remain visible (it's metadata, not secret).
    assert " key 7 <REDACTED" in out, (
        f"Expected ' key 7 <REDACTED_*>' in output but got:\n{out}"
    )


def test_radius_server_nested_key_block(sanitizer):
    config = (
        "radius server RAD1\n"
        " address ipv4 192.0.2.1 auth-port 1812 acct-port 1813\n"
        f" key 7 {LEAK_MARKER}\n"
    )
    out = sanitizer.sanitize(config)
    assert LEAK_MARKER not in out
    assert " key 7 <REDACTED" in out


# ---------------------------------------------------------------------------
# Non-secret lines that must NOT be touched.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected_passthrough", [
    # 'service password-encryption' is a command, not a secret.
    ("service password-encryption", "service password-encryption"),
    # Hostname redaction is disabled by rules.yaml default.
    ("hostname myrouter01", "hostname myrouter01"),
    # Descriptions with incidental secret-like words must pass through untouched.
    (" description Fiber password lab circuit",
     " description Fiber password lab circuit"),
    (" description secret tunnel to site-B",
     " description secret tunnel to site-B"),
    # key-chain 'key <N>' line: N is a key ID, not a secret — no trailing value.
    (" key 1", " key 1"),
    # Top-level 'crypto key generate rsa ...' — no secret on line.
    ("crypto key generate rsa modulus 2048",
     "crypto key generate rsa modulus 2048"),
    # rsakeypair is a keypair NAME reference, not a secret.
    (" rsakeypair KEYPAIRNAME", " rsakeypair KEYPAIRNAME"),
    # 'key chain NAME' — NAME is a chain identifier, not a secret.
    ("key chain MYCHAIN", "key chain MYCHAIN"),
])
def test_non_secret_lines_pass_through(sanitizer, line, expected_passthrough):
    out = _run(sanitizer, line)
    assert out == expected_passthrough, (
        f"Expected {expected_passthrough!r} but got {out!r}"
    )


# ---------------------------------------------------------------------------
# Encryption-type preservation: when Cisco writes 'password 7 HASH', we should
# redact HASH and leave the '7' visible as metadata.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected_fragment", [
    ("enable secret 5 $1$abcd$xyz", "enable secret 5 <REDACTED_"),
    ("enable password 7 14141B", "enable password 7 <REDACTED_"),
    (" neighbor 192.0.2.1 password 7 ABCDEF01",
     " neighbor IP_001 password 7 <REDACTED_"),
    (" ip ospf authentication-key 7 ABCDEF", " ip ospf authentication-key 7 <REDACTED_"),
    (" ip ospf message-digest-key 1 md5 7 ABCDEF",
     " ip ospf message-digest-key 1 md5 7 <REDACTED_"),
])
def test_encryption_type_preserved(sanitizer, line, expected_fragment):
    out = _run(sanitizer, line)
    assert expected_fragment in out, (
        f"Expected output to contain {expected_fragment!r} but got {out!r}"
    )


# ---------------------------------------------------------------------------
# IP address tokenization
# ---------------------------------------------------------------------------

def test_ip_addresses_tokenized(sanitizer):
    out = _run(sanitizer, " ip address 10.1.1.1 255.255.255.0")
    assert "10.1.1.1" not in out
    assert "255.255.255.0" not in out
    assert re.search(r"IP_\d+", out)


def test_ip_zero_preserved(sanitizer):
    """0.0.0.0 is a literal Cisco idiom (default route, any-source) and must
    not be tokenized per existing sanitizer behavior."""
    out = _run(sanitizer, "ip route 0.0.0.0 0.0.0.0 192.0.2.1")
    assert "0.0.0.0 0.0.0.0" in out
    assert "192.0.2.1" not in out


# ---------------------------------------------------------------------------
# Idempotency: running a sanitized config through the sanitizer again must be
# a no-op on the redacted markers.
# ---------------------------------------------------------------------------

def test_idempotent_on_already_redacted(sanitizer):
    line = " key 7 <REDACTED_TACACS_KEY>"
    out = _run(sanitizer, line)
    assert "<REDACTED_TACACS_KEY>" in out, f"Redaction marker lost: {out!r}"


# ---------------------------------------------------------------------------
# Real rules.yaml load integration.
# ---------------------------------------------------------------------------

def test_load_rules_yaml():
    rules = load_rules("rules.yaml")
    assert "sanitize" in rules
    assert "ip_addresses" in rules["sanitize"]
