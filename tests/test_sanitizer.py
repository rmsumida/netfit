"""Regression tests for CiscoConfigSanitizer.

The sanitizer must (a) redact secret material without leaking it into any output
group and (b) leave non-secret lines alone even when they contain words like
"password", "key", or "secret" in descriptions or command names.
"""
import re
from pathlib import Path

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
        "domains": True,
        "certificates": True,
        "interface_descriptions": True,
        "bgp_descriptions": True,
        "bgp_peer_groups": True,
        "bgp_as_numbers": True,
        "crypto_map_names": True,
        "route_map_names": True,
        "banners": True,
        "prefix_list_names": True,
        "community_list_names": True,
        "vrf_names": True,
        "acl_names": True,
        "tacacs_server_names": True,
        "policy_map_names": True,
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
# IPv6 address tokenization. Mirrors IPv4 semantics: every distinct address
# gets a stable IPV6_NNN token via the shared TokenMapper.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,leak", [
    ("ipv6 address 2001:506:2004:FBE::11B1:7D81/64", "2001:506:2004:FBE::11B1:7D81"),
    (" neighbor 2001:1890:1286:100::3:6A peer-group X", "2001:1890:1286:100::3:6A"),
    ("ipv6 route 2001:db8::/32 2001:db8::1", "2001:db8::1"),
    ("ipv6 host MYHOST 2001:0db8:85a3:0000:0000:8a2e:0370:7334",
     "2001:0db8:85a3:0000:0000:8a2e:0370:7334"),
    ("ipv6 nd dad ::1", "::1"),
    ("ipv6 nd suppress fe80::1", "fe80::1"),
])
def test_ipv6_addresses_tokenized(sanitizer, line, leak):
    out = _run(sanitizer, line)
    assert leak.lower() not in out.lower()
    assert "IPV6_" in out


def test_ipv6_unspecified_preserved(sanitizer):
    # `::` (all-zeros / unspecified) is the IPv6 equivalent of 0.0.0.0 and
    # should pass through unchanged for readability.
    out = _run(sanitizer, "ipv6 route ::/0 2001:db8::1")
    assert "::/0" in out
    # The non-`::` address still gets tokenized.
    assert "2001:db8::1" not in out
    assert "IPV6_" in out


def test_ipv6_same_address_reuses_token(sanitizer):
    out = sanitizer.sanitize(
        "ipv6 address 2001:506:2004:FBE::11B1:7D81/64\n"
        "ipv6 route 2001:506:2004:FBE::11B1:7D81/128 Null0\n"
    )
    # Same canonical address (case-folded) → one token, used twice.
    assert out.count("IPV6_001") == 2


def test_ipv6_case_insensitive_dedup(sanitizer):
    # Cisco emits IPv6 in mixed case; same address in different cases must
    # collapse to one token.
    out = sanitizer.sanitize(
        "ipv6 address 2001:DB8::1/64\n"
        "ipv6 route ::/0 2001:db8::1\n"
    )
    # Two address mentions, one canonical token reused.
    assert out.count("IPV6_001") == 2


def test_ipv6_does_not_match_colon_separated_mac(sanitizer):
    # 6-group colon-separated MAC notation has only 5 colons and no `::`,
    # so it must not be matched by the IPv6 pattern.
    out = _run(sanitizer, " mac-address 00:01:02:03:04:05")
    assert "00:01:02:03:04:05" in out
    assert "IPV6_" not in out


def test_ipv6_disabled_via_ip_addresses_rule():
    # IPv6 redaction is gated on the same `ip_addresses` rule as IPv4.
    rules = {"sanitize": {**RULES["sanitize"], "ip_addresses": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("ipv6 address 2001:db8::1/64\n")
    assert "2001:db8::1" in out
    assert "IPV6_" not in out


def test_ipv6_mappings_exported(sanitizer):
    sanitizer.sanitize(
        "ipv6 address 2001:506:2004:FBE::11B1:7D81/64\n"
        "ipv6 route ::/0 fe80::1\n"
    )
    mappings = sanitizer.get_mappings()
    assert "ipv6" in mappings
    # Two distinct addresses → IPV6_001 and IPV6_002.
    assert sorted(mappings["ipv6"].values()) == ["IPV6_001", "IPV6_002"]


# ---------------------------------------------------------------------------
# Domain anonymization: domains declared via `ip domain {name,-name,list,-list}`
# are tokenized and substituted everywhere they appear in the config.
# ---------------------------------------------------------------------------

def test_ip_domain_list_tokenized(sanitizer):
    out = sanitizer.sanitize("ip domain list att.com\n")
    assert "att.com" not in out
    assert "DOMAIN_001" in out


def test_ip_domain_name_tokenized(sanitizer):
    out = sanitizer.sanitize("ip domain name corp.example.net\n")
    assert "corp.example.net" not in out
    assert "DOMAIN_001" in out


def test_old_hyphenated_syntax_tokenized(sanitizer):
    out = sanitizer.sanitize("ip domain-list legacy.example.org\nip domain-name old.example.org\n")
    assert "legacy.example.org" not in out
    assert "old.example.org" not in out
    assert "DOMAIN_001" in out and "DOMAIN_002" in out


def test_same_domain_reuses_token(sanitizer):
    out = sanitizer.sanitize(
        "ip domain list att.com\n"
        "ip domain list att.com\n"
    )
    assert out.count("DOMAIN_001") == 2
    assert "DOMAIN_002" not in out


def test_domain_substituted_outside_ip_domain_lines(sanitizer):
    # The point of pre-scanning ip-domain config is to anonymize the domain
    # everywhere it appears — including TACACS/NTP server FQDNs etc.
    out = sanitizer.sanitize(
        "ip domain list att.com\n"
        "ntp server ntp1.att.com\n"
    )
    assert "att.com" not in out
    assert "ntp1.DOMAIN_001" in out


def test_longer_fqdn_replaces_before_root(sanitizer):
    # If both `att.com` and `pmtr.swst.att.com` are declared, longer-first
    # ordering must give each its own token without nested corruption.
    out = sanitizer.sanitize(
        "ip domain list att.com\n"
        "ip domain list pmtr.swst.att.com\n"
        "ntp server pmtr.swst.att.com\n"
        "ntp server att.com\n"
    )
    # Each unique FQDN gets its own DOMAIN token (att.com=001, longer=002).
    assert "att.com" not in out
    assert "DOMAIN_001" in out
    assert "DOMAIN_002" in out
    # The longer FQDN must collapse to one token, not "pmtr.swst.DOMAIN_001".
    assert "pmtr.swst.DOMAIN_001" not in out


def test_substring_not_matching_domain_left_alone(sanitizer):
    # `mfatt.com` contains `att.com` as a substring but NOT at a word
    # boundary — must not match.
    out = sanitizer.sanitize(
        "ip domain list att.com\n"
        "ntp server mfatt.com\n"
    )
    assert "mfatt.com" in out


def test_domains_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "domains": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("ip domain list att.com\nntp server ntp.att.com\n")
    assert "att.com" in out
    assert "DOMAIN_" not in out


def test_no_ip_domain_lines_means_no_domain_substitution(sanitizer):
    # If a config has no `ip domain` declarations, FQDN substrings elsewhere
    # are left alone — anonymization is gated on declared domains.
    out = sanitizer.sanitize("ntp server ntp.att.com\n")
    assert "ntp.att.com" in out
    assert "DOMAIN_" not in out


def test_domain_substitution_skips_inside_redaction_markers(sanitizer):
    # Domain substitution must not rewrite text inside a previously redacted
    # marker like <REDACTED_att.com_OPS> if such a string ever appears.
    out = sanitizer.sanitize(
        "ip domain list att.com\n"
        "ntp server <REDACTED_att.com_OPS>\n"
    )
    assert "<REDACTED_att.com_OPS>" in out


def test_domain_mappings_exported(sanitizer):
    sanitizer.sanitize("ip domain list att.com\nip domain list other.example\n")
    mappings = sanitizer.get_mappings()
    assert "domains" in mappings
    assert mappings["domains"] == {"att.com": "DOMAIN_001", "other.example": "DOMAIN_002"}


# ---------------------------------------------------------------------------
# Certificate blob redaction: hex content inside `crypto pki certificate
# chain` blocks is collapsed to a single <REDACTED_CERTIFICATE> placeholder.
# Header and `quit` terminator are preserved so chain structure stays visible.
# ---------------------------------------------------------------------------

CERT_BLOCK = """\
crypto pki certificate chain SLA-TrustPoint
 certificate ca 01
  30820321 30820209 A0030201 02020101 300D0609 2A864886 F70D0101 0B050030
  32310E30 0C060355 040A1305 43697363 6F312030 1E060355 04031317 43697363
  D697DF7F 28
      quit
!
"""


def test_certificate_hex_blob_redacted(sanitizer):
    out = sanitizer.sanitize(CERT_BLOCK)
    assert "30820321" not in out
    assert "D697DF7F 28" not in out
    assert "<REDACTED_CERTIFICATE>" in out


def test_certificate_chain_header_preserved(sanitizer):
    out = sanitizer.sanitize(CERT_BLOCK)
    assert "crypto pki certificate chain SLA-TrustPoint" in out
    assert " certificate ca 01" in out


def test_certificate_quit_terminator_preserved(sanitizer):
    out = sanitizer.sanitize(CERT_BLOCK)
    assert "quit" in out
    # Lines after the cert block should resume normal sanitization.
    assert out.rstrip().endswith("!")


def test_certificate_self_signed_variant(sanitizer):
    block = (
        "crypto pki certificate chain MY-TP\n"
        " certificate self-signed 02\n"
        "  AABBCCDD EEFF0011\n"
        "      quit\n"
    )
    out = sanitizer.sanitize(block)
    assert "AABBCCDD" not in out
    assert " certificate self-signed 02" in out
    assert "<REDACTED_CERTIFICATE>" in out


def test_certificate_bare_serial_variant(sanitizer):
    # `certificate <serial>` (no `ca`/`self-signed` qualifier) is the device
    # cert form. Must still be detected.
    block = (
        " certificate 1A2B3C4D\n"
        "  AABBCCDD EEFF0011\n"
        "  quit\n"
    )
    out = sanitizer.sanitize(block)
    assert "AABBCCDD" not in out
    assert "<REDACTED_CERTIFICATE>" in out


def test_certificate_indent_preserved_in_placeholder(sanitizer):
    # Placeholder indent should match the hex indent so the redacted block
    # still looks like a child of the chain.
    out = sanitizer.sanitize(CERT_BLOCK)
    assert "  <REDACTED_CERTIFICATE>" in out


def test_multiple_certs_in_one_chain(sanitizer):
    block = (
        "crypto pki certificate chain MY-CHAIN\n"
        " certificate ca 01\n"
        "  AAAA1111\n"
        "      quit\n"
        " certificate self-signed 02\n"
        "  BBBB2222\n"
        "      quit\n"
    )
    out = sanitizer.sanitize(block)
    assert "AAAA1111" not in out
    assert "BBBB2222" not in out
    # Both cert headers preserved → chain structure visible.
    assert " certificate ca 01" in out
    assert " certificate self-signed 02" in out
    # Both blobs redacted → two placeholders.
    assert out.count("<REDACTED_CERTIFICATE>") == 2


def test_lines_outside_cert_block_unaffected(sanitizer):
    block = (
        "hostname r1\n"
        "crypto pki certificate chain MY-TP\n"
        " certificate ca 01\n"
        "  AABBCCDD\n"
        "      quit\n"
        "ntp server 192.0.2.1\n"
    )
    out = sanitizer.sanitize(block)
    # Hostname rule is disabled in test RULES; it must pass through.
    assert "hostname r1" in out
    # NTP line: IP must still be tokenized by the IP pass.
    assert "192.0.2.1" not in out
    assert "ntp server IP_" in out


def test_certificates_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "certificates": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(CERT_BLOCK)
    # Hex must remain intact when the rule is off.
    assert "30820321" in out
    assert "<REDACTED_CERTIFICATE>" not in out


def test_description_with_word_certificate_not_treated_as_cert(sanitizer):
    # A `description ... certificate ...` line must not be treated as a
    # cert header — it doesn't match `^\s+certificate <serial>` exactly.
    out = sanitizer.sanitize(" description renew certificate before EOL\n")
    assert "<REDACTED_CERTIFICATE>" not in out
    assert "renew certificate before EOL" in out


# ---------------------------------------------------------------------------
# License UDI: hardware serial in `license udi pid <PID> sn <SERIAL>` is
# redacted; the PID is preserved (useful for refresh sizing).
# ---------------------------------------------------------------------------

def test_license_udi_serial_redacted(sanitizer):
    out = _run(sanitizer, "license udi pid ASR1013 sn NWG1447009X")
    assert "NWG1447009X" not in out
    assert "<REDACTED_LICENSE_SERIAL>" in out


def test_license_udi_pid_preserved(sanitizer):
    out = _run(sanitizer, "license udi pid ASR1013 sn NWG1447009X")
    assert "ASR1013" in out


def test_license_udi_with_trailing_tokens(sanitizer):
    # IOS occasionally appends a `cf` (chassis-frame) field. The sn redaction
    # must not swallow trailing fields.
    out = _run(sanitizer, "license udi pid ISR4451-X sn FDO12345ABC cf 9999")
    assert "FDO12345ABC" not in out
    assert "<REDACTED_LICENSE_SERIAL>" in out
    assert "cf 9999" in out


def test_license_non_udi_lines_untouched(sanitizer):
    # `license boot level adventerprise` and similar must not be matched.
    out = _run(sanitizer, "license boot level adventerprise")
    assert "<REDACTED_LICENSE_SERIAL>" not in out
    assert "license boot level adventerprise" in out


# ---------------------------------------------------------------------------
# Interface description redaction: descriptions inside `interface ...` blocks
# are replaced by `description <REDACTED_DESCRIPTION>`. Descriptions in other
# block types (flow record, class-map, BGP neighbor, etc.) are left alone.
# ---------------------------------------------------------------------------

def test_interface_description_redacted(sanitizer):
    block = (
        "interface GigabitEthernet0/0/0\n"
        " description AVPN PE FTWOTXED;IZEC.592320..ATI;14/KFGS/872233/SW\n"
        " ip address 192.0.2.1 255.255.255.0\n"
    )
    out = sanitizer.sanitize(block)
    assert "AVPN" not in out
    assert "IZEC" not in out
    assert " description <REDACTED_DESCRIPTION>" in out
    # Adjacent ip address line still gets normal IP tokenization.
    assert "192.0.2.1" not in out


def test_description_keyword_preserved(sanitizer):
    out = sanitizer.sanitize(
        "interface Loopback0\n"
        " description Loop0; 10.0.0.1/32\n"
    )
    # Structure is visible — interface has a description — but content is gone.
    assert "description" in out
    assert "Loop0" not in out
    assert "10.0.0.1" not in out


def test_multiple_interfaces_each_tracked(sanitizer):
    block = (
        "interface GigabitEthernet0/0/0\n"
        " description first leaky text\n"
        "interface GigabitEthernet0/0/1\n"
        " description second leaky text\n"
    )
    out = sanitizer.sanitize(block)
    assert "first leaky" not in out
    assert "second leaky" not in out
    assert out.count("<REDACTED_DESCRIPTION>") == 2


def test_description_outside_interface_preserved(sanitizer):
    # Descriptions under non-interface blocks (flow record, class-map, etc.)
    # are not in scope for this rule.
    block = (
        "flow record netflow-mini\n"
        " description netflow src dest ip and port\n"
        "class-map match-any VOICE\n"
        " description voice traffic class\n"
    )
    out = sanitizer.sanitize(block)
    assert "netflow src dest ip and port" in out
    assert "voice traffic class" in out
    assert "<REDACTED_DESCRIPTION>" not in out


def test_bang_separator_does_not_exit_interface_mode(sanitizer):
    # The `!` separator is conventional but is NOT a top-level command —
    # interface-mode state must survive it (until the next real column-0 cmd).
    block = (
        "interface GigabitEthernet0/0/0\n"
        " ip address 10.0.0.1 255.255.255.0\n"
        "!\n"
        " description should still redact (continuation of interface)\n"
    )
    out = sanitizer.sanitize(block)
    assert "should still redact" not in out
    assert "<REDACTED_DESCRIPTION>" in out


def test_top_level_command_exits_interface_mode(sanitizer):
    # The point of this test is the state-machine boundary: a column-0 line
    # exits `interface` mode so the interface-description rule no longer
    # fires. Use `class-map` (no description rule) to isolate that behavior.
    block = (
        "interface GigabitEthernet0/0/0\n"
        " description redact me\n"
        "class-map match-any VOICE\n"
        " description preserve me\n"
    )
    out = sanitizer.sanitize(block)
    assert "redact me" not in out
    assert "preserve me" in out
    assert out.count("<REDACTED_DESCRIPTION>") == 1


def test_interface_descriptions_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "interface_descriptions": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(
        "interface GigabitEthernet0/0/0\n"
        " description sensitive customer info\n"
    )
    assert "sensitive customer info" in out
    assert "<REDACTED_DESCRIPTION>" not in out


def test_empty_description_left_alone(sanitizer):
    # A `description` keyword with no content is meaningless to redact and
    # would create a malformed `description <REDACTED_DESCRIPTION>` if naively
    # matched. The pattern requires at least one non-space char of content.
    out = sanitizer.sanitize(
        "interface GigabitEthernet0/0/0\n"
        " description\n"
    )
    assert " description\n" in out + "\n"
    assert "<REDACTED_DESCRIPTION>" not in out


# ---------------------------------------------------------------------------
# BGP redactions: peer-group names, neighbor descriptions, and AS numbers.
# ---------------------------------------------------------------------------

BGP_BLOCK = """\
router bgp 797
 bgp router-id 10.0.0.1
 neighbor EBIZ-PMTR peer-group
 neighbor EBIZ-PMTR remote-as 13979
 neighbor EBIZ-PMTR description *** EBIZ-PMTR vrf for BP-BP traffic ***
 neighbor EBIZ-PMTR-LOCAL-EBIZ-RTR peer-group
 neighbor EBIZ-PMTR-LOCAL-EBIZ-RTR remote-as 797
 neighbor EBIZ-PMTR-LOCAL-EBIZ-RTR description *** Peer to DLLSTXCFDRNEZVPN3 ***
 neighbor 192.0.2.1 peer-group EBIZ-PMTR
"""


def test_bgp_local_as_redacted(sanitizer):
    out = sanitizer.sanitize(BGP_BLOCK)
    # router bgp <AS> → router bgp ASN_NNN
    assert "router bgp 797" not in out
    assert "router bgp ASN_001" in out


def test_bgp_remote_as_redacted(sanitizer):
    out = sanitizer.sanitize(BGP_BLOCK)
    assert "remote-as 13979" not in out
    assert "remote-as 797" not in out
    # 797 and 13979 → ASN_001 and ASN_002
    assert "ASN_001" in out and "ASN_002" in out


def test_bgp_local_and_remote_share_token(sanitizer):
    # The local AS 797 also appears in `neighbor X remote-as 797` (iBGP) —
    # both must collapse to the same ASN token.
    out = sanitizer.sanitize(BGP_BLOCK)
    # 797 appears 2x (router bgp + iBGP remote-as), 13979 appears 1x.
    assert out.count("ASN_001") == 2
    assert out.count("ASN_002") == 1


def test_bgp_peer_group_name_redacted(sanitizer):
    out = sanitizer.sanitize(BGP_BLOCK)
    assert "EBIZ-PMTR" not in out
    # Each peer-group name → distinct PG token.
    assert "PG_001" in out and "PG_002" in out


def test_bgp_peer_group_longest_first(sanitizer):
    # `EBIZ-PMTR-LOCAL-EBIZ-RTR` must collapse to ONE token, not be partially
    # rewritten as `PG_001-LOCAL-EBIZ-RTR` (which would happen with shortest-
    # first ordering since `EBIZ-PMTR` is a prefix of the longer name).
    out = sanitizer.sanitize(BGP_BLOCK)
    assert "PG_002-LOCAL" not in out
    assert "PG_001-LOCAL" not in out


def test_bgp_peer_group_assignment_form_collected(sanitizer):
    # `neighbor 192.0.2.1 peer-group EBIZ-PMTR` — the trailing peer-group
    # name should be substituted by the same token used elsewhere.
    out = sanitizer.sanitize(BGP_BLOCK)
    assignment_line = next(l for l in out.splitlines() if "peer-group" in l and "IP_" in l)
    # The peer-group name in the assignment must collapse to a PG token.
    assert "EBIZ-PMTR" not in assignment_line
    assert "PG_" in assignment_line


def test_bgp_neighbor_description_redacted(sanitizer):
    out = sanitizer.sanitize(BGP_BLOCK)
    assert "vrf for BP-BP traffic" not in out
    assert "DLLSTXCFDRNEZVPN3" not in out
    # Two BGP neighbor descriptions in BGP_BLOCK → two redacted markers.
    desc_lines = [l for l in out.splitlines() if "description <REDACTED_DESCRIPTION>" in l]
    assert len(desc_lines) == 2


def test_bgp_redistribute_as_redacted(sanitizer):
    out = sanitizer.sanitize(" redistribute bgp 65001 metric 100\n")
    assert "redistribute bgp 65001" not in out
    assert "redistribute bgp ASN_001" in out
    # Trailing `metric 100` is not an AS number — must be preserved.
    assert "metric 100" in out


def test_bgp_route_target_as_redacted(sanitizer):
    out = sanitizer.sanitize(
        " route-target import 65001:100\n"
        " route-target export 65001:200\n"
    )
    # ASN portion (left of colon) tokenized; admin-assigned ID after `:` kept.
    assert "65001:100" not in out
    assert "65001:200" not in out
    assert "ASN_001:100" in out
    assert "ASN_001:200" in out


def test_bgp_rd_as_redacted(sanitizer):
    out = sanitizer.sanitize(" rd 65001:42\n")
    assert "65001:42" not in out
    assert "rd ASN_001:42" in out


def test_bgp_confederation_peers_multi_as(sanitizer):
    out = sanitizer.sanitize(" bgp confederation peers 100 200 300\n")
    assert "100" not in out.split("peers")[1]
    assert "ASN_001" in out and "ASN_002" in out and "ASN_003" in out


def test_bgp_descriptions_disabled():
    rules = {"sanitize": {**RULES["sanitize"], "bgp_descriptions": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(" neighbor X description sensitive peer info\n")
    assert "sensitive peer info" in out


def test_bgp_peer_groups_disabled():
    rules = {"sanitize": {**RULES["sanitize"], "bgp_peer_groups": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(BGP_BLOCK)
    assert "EBIZ-PMTR" in out
    assert "PG_" not in out


def test_bgp_as_numbers_disabled():
    rules = {"sanitize": {**RULES["sanitize"], "bgp_as_numbers": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(BGP_BLOCK)
    assert "router bgp 797" in out
    assert "remote-as 13979" in out
    assert "ASN_" not in out


def test_non_bgp_integers_left_alone(sanitizer):
    # MTU, VLAN ID, numbered-ACL number must not be touched by AS-number
    # redaction. (`ip access-list extended <NAME>` is the *named* form even
    # when the name is digits — that's covered by the ACL-name rule, not
    # this test.)
    out = sanitizer.sanitize(
        "interface Vlan797\n"
        " ip mtu 1500\n"
        "access-list 100 permit ip any any\n"
    )
    assert "Vlan797" in out
    assert "ip mtu 1500" in out
    assert "access-list 100" in out
    assert "ASN_" not in out


def test_bgp_as_and_peer_group_mappings_exported(sanitizer):
    sanitizer.sanitize(BGP_BLOCK)
    mappings = sanitizer.get_mappings()
    assert mappings["as_numbers"] == {"797": "ASN_001", "13979": "ASN_002"}
    assert mappings["bgp_peer_groups"] == {
        "EBIZ-PMTR": "PG_001",
        "EBIZ-PMTR-LOCAL-EBIZ-RTR": "PG_002",
    }


# ---------------------------------------------------------------------------
# Crypto map name redaction. Names are tokenized with stable CRYPTOMAP_NNN
# tokens (one per unique name) so each occurrence can still be traced
# through the config.
# ---------------------------------------------------------------------------

def test_crypto_map_declaration_redacted(sanitizer):
    out = sanitizer.sanitize("crypto map EBIZ 10 ipsec-isakmp\n")
    assert "EBIZ" not in out
    assert "crypto map CRYPTOMAP_001 10 ipsec-isakmp" in out


def test_crypto_map_interface_application_redacted(sanitizer):
    block = (
        "crypto map EBIZ 10 ipsec-isakmp\n"
        "interface GigabitEthernet0/0/0\n"
        " crypto map EBIZ\n"
    )
    out = sanitizer.sanitize(block)
    assert "EBIZ" not in out
    # Both the declaration and the interface application must use the same
    # token so the trace from interface → map definition is preserved.
    assert out.count("CRYPTOMAP_001") == 2


def test_crypto_map_dynamic_form(sanitizer):
    out = sanitizer.sanitize("crypto dynamic-map MYDYN 20\n")
    assert "MYDYN" not in out
    assert "crypto dynamic-map CRYPTOMAP_001 20" in out


def test_crypto_map_ipv6_interface_form(sanitizer):
    block = (
        "crypto map V6MAP 10 ipsec-isakmp\n"
        "interface GigabitEthernet0/0/0\n"
        " ipv6 crypto map V6MAP\n"
    )
    out = sanitizer.sanitize(block)
    assert "V6MAP" not in out
    assert out.count("CRYPTOMAP_001") == 2


def test_crypto_map_unique_names_get_unique_tokens(sanitizer):
    block = (
        "crypto map EBIZ 10 ipsec-isakmp\n"
        "crypto map JASPER 20 ipsec-isakmp\n"
        "crypto map IQOR 30 ipsec-isakmp\n"
    )
    out = sanitizer.sanitize(block)
    for name in ("EBIZ", "JASPER", "IQOR"):
        assert name not in out
    assert "CRYPTOMAP_001" in out
    assert "CRYPTOMAP_002" in out
    assert "CRYPTOMAP_003" in out


def test_crypto_map_longest_first_substitution(sanitizer):
    # If two map names share a prefix, the longer one must collapse to a
    # single token, not a partially-rewritten string.
    block = (
        "crypto map EBIZ 10 ipsec-isakmp\n"
        "crypto map EBIZ-BACKUP 20 ipsec-isakmp\n"
    )
    out = sanitizer.sanitize(block)
    assert "EBIZ" not in out
    # Specifically: must NOT see e.g. `CRYPTOMAP_001-BACKUP`.
    assert "CRYPTOMAP_001-BACKUP" not in out
    assert "CRYPTOMAP_002-BACKUP" not in out


def test_crypto_map_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "crypto_map_names": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("crypto map EBIZ 10 ipsec-isakmp\n")
    assert "EBIZ" in out
    assert "CRYPTOMAP_" not in out


def test_crypto_map_mappings_exported(sanitizer):
    sanitizer.sanitize(
        "crypto map EBIZ 10 ipsec-isakmp\n"
        "crypto map JASPER 20 ipsec-isakmp\n"
    )
    mappings = sanitizer.get_mappings()
    assert mappings["crypto_map_names"] == {
        "EBIZ": "CRYPTOMAP_001",
        "JASPER": "CRYPTOMAP_002",
    }


# ---------------------------------------------------------------------------
# Route-map name redaction. Names are tokenized with stable RTMAP_NNN tokens
# (one per unique name) so each occurrence — declaration, BGP/redistribute
# reference, BGP inject-map / exist-map directive — can still be traced.
# ---------------------------------------------------------------------------

def test_route_map_declaration_redacted(sanitizer):
    out = sanitizer.sanitize("route-map ATT-TO-DIRECTV permit 10\n")
    assert "ATT-TO-DIRECTV" not in out
    assert "route-map RTMAP_001 permit 10" in out


def test_route_map_referenced_under_neighbor_redacted(sanitizer):
    block = (
        "route-map ATT-TO-DIRECTV permit 10\n"
        "router bgp 797\n"
        " neighbor 192.0.2.1 route-map ATT-TO-DIRECTV out\n"
    )
    out = sanitizer.sanitize(block)
    assert "ATT-TO-DIRECTV" not in out
    # Same token used in declaration and reference — trace preserved.
    assert out.count("RTMAP_001") == 2


def test_route_map_multiple_sequence_numbers_share_token(sanitizer):
    block = (
        "route-map ORACLE_SOURCE_ROUTES permit 10\n"
        "route-map ORACLE_SOURCE_ROUTES permit 20\n"
        "route-map ORACLE_SOURCE_ROUTES permit 30\n"
    )
    out = sanitizer.sanitize(block)
    assert "ORACLE_SOURCE_ROUTES" not in out
    # Three sequence stanzas of the same map → one token, three uses.
    assert out.count("RTMAP_001") == 3


def test_bgp_inject_map_both_names_redacted(sanitizer):
    block = (
        "route-map ORACLE_SOURCE_ROUTES permit 10\n"
        "router bgp 797\n"
        " address-family ipv4\n"
        "  bgp inject-map INJECT-ORACLE-MORE-SPECIFIC exist-map ORACLE_SOURCE_ROUTES copy-attributes\n"
    )
    out = sanitizer.sanitize(block)
    # Both names — even the inject-map name without a `route-map` declaration —
    # must be tokenized and the trailing `copy-attributes` keyword preserved.
    assert "INJECT-ORACLE-MORE-SPECIFIC" not in out
    assert "ORACLE_SOURCE_ROUTES" not in out
    assert "copy-attributes" in out
    # Two distinct names → two distinct RTMAP tokens.
    assert "RTMAP_001" in out and "RTMAP_002" in out


def test_route_map_redistribute_reference(sanitizer):
    block = (
        "route-map REDIST-FILTER permit 10\n"
        "router bgp 797\n"
        " address-family ipv4\n"
        "  redistribute connected route-map REDIST-FILTER\n"
    )
    out = sanitizer.sanitize(block)
    assert "REDIST-FILTER" not in out
    assert "redistribute connected route-map RTMAP_001" in out


def test_route_map_runs_before_crypto_map(sanitizer):
    # When a route-map name embeds a crypto-map name (`IPvX-EBIZ-...`), the
    # route-map name must collapse to ONE token instead of becoming a
    # crypto-map-rewritten string like `IPvX-CRYPTOMAP_001-LESS-SPECIFIC-TEMP`.
    block = (
        "crypto map EBIZ 10 ipsec-isakmp\n"
        "route-map IPvX-EBIZ-LESS-SPECIFIC-TEMP permit 10\n"
    )
    out = sanitizer.sanitize(block)
    assert "IPvX-EBIZ" not in out
    assert "EBIZ" not in out
    # Route-map collapses cleanly, no crypto-map-substring pollution.
    assert "IPvX-CRYPTOMAP" not in out
    # Route-map line uses RTMAP_001; crypto-map declaration uses CRYPTOMAP_001.
    assert "route-map RTMAP_001 permit 10" in out
    assert "crypto map CRYPTOMAP_001 10 ipsec-isakmp" in out


def test_route_map_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "route_map_names": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("route-map ATT-TO-DIRECTV permit 10\n")
    assert "ATT-TO-DIRECTV" in out
    assert "RTMAP_" not in out


def test_route_map_mappings_exported(sanitizer):
    sanitizer.sanitize(
        "route-map ATT-TO-DIRECTV permit 10\n"
        "route-map DIRECTV-TO-ATT permit 10\n"
        "router bgp 797\n"
        " address-family ipv4\n"
        "  bgp inject-map INJECT-X exist-map DIRECTV-TO-ATT copy-attributes\n"
    )
    mappings = sanitizer.get_mappings()
    # Three unique names: 2 declared route-maps + 1 inject-map name (the
    # exist-map name DIRECTV-TO-ATT is already in the declared set).
    assert mappings["route_map_names"] == {
        "ATT-TO-DIRECTV": "RTMAP_001",
        "DIRECTV-TO-ATT": "RTMAP_002",
        "INJECT-X": "RTMAP_003",
    }


# ---------------------------------------------------------------------------
# Banner redaction. Multi-line `banner <type> <DELIM> ... <DELIM>` blocks are
# collapsed to a single <REDACTED_BANNER> placeholder. Opening line and
# closing delimiter line are preserved so structure stays visible.
# ---------------------------------------------------------------------------

def test_banner_motd_redacted(sanitizer):
    block = (
        "banner motd ^C\n"
        "Welcome to the production router.\n"
        "Authorized AT&T users only.\n"
        "^C\n"
    )
    out = sanitizer.sanitize(block)
    assert "Welcome" not in out
    assert "AT&T" not in out
    assert "<REDACTED_BANNER>" in out
    assert out.startswith("banner motd ^C\n")
    # Closing delimiter preserved.
    assert "\n^C\n" in out + "\n"


def test_banner_login_redacted(sanitizer):
    block = (
        "banner login ^C\n"
        "Restricted system. Unauthorized access prohibited.\n"
        "^C\n"
    )
    out = sanitizer.sanitize(block)
    assert "Restricted system" not in out
    assert "<REDACTED_BANNER>" in out


def test_banner_with_hash_delimiter(sanitizer):
    # Cisco accepts any character as the delimiter — must be captured per-banner.
    block = (
        "banner exec #\n"
        "Some leaky message here.\n"
        "#\n"
    )
    out = sanitizer.sanitize(block)
    assert "Some leaky message" not in out
    assert "<REDACTED_BANNER>" in out


def test_banner_only_one_redacted_line_per_block(sanitizer):
    # A 50-line banner body must collapse to exactly one placeholder.
    body_lines = "\n".join(f"line {i}" for i in range(50))
    block = f"banner motd ^C\n{body_lines}\n^C\n"
    out = sanitizer.sanitize(block)
    assert out.count("<REDACTED_BANNER>") == 1
    assert "line 25" not in out


def test_multiple_banners_each_tracked(sanitizer):
    block = (
        "banner motd ^C\nfirst banner\n^C\n"
        "banner login ^C\nsecond banner\n^C\n"
    )
    out = sanitizer.sanitize(block)
    assert out.count("<REDACTED_BANNER>") == 2
    assert "first banner" not in out
    assert "second banner" not in out


def test_lines_after_banner_resume_normal_sanitization(sanitizer):
    block = (
        "banner motd ^C\nleaky text\n^C\n"
        "ntp server 192.0.2.1\n"
    )
    out = sanitizer.sanitize(block)
    # IP redaction must still fire on the post-banner line.
    assert "192.0.2.1" not in out
    assert "ntp server IP_" in out


def test_banner_disabled_via_rules():
    rules = {"sanitize": {**RULES["sanitize"], "banners": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("banner motd ^C\nleaky text\n^C\n")
    assert "leaky text" in out
    assert "<REDACTED_BANNER>" not in out


# ---------------------------------------------------------------------------
# Prefix-list + community-list name redaction. Same shape as route-maps:
# collect declarations, longest-first substitution everywhere they appear.
# ---------------------------------------------------------------------------

def test_prefix_list_declaration_redacted(sanitizer):
    out = sanitizer.sanitize(
        "ip prefix-list 9-1-1_PSP_UNTRUSTED-TO_ATT seq 10 permit 10.0.0.0/24\n"
    )
    assert "9-1-1_PSP_UNTRUSTED-TO_ATT" not in out
    assert "PREFIX_001" in out


def test_prefix_list_referenced_under_route_map_redacted(sanitizer):
    block = (
        "ip prefix-list MY-PFX seq 10 permit 10.0.0.0/24\n"
        "route-map MAP-A permit 10\n"
        " match ip address prefix-list MY-PFX\n"
    )
    out = sanitizer.sanitize(block)
    assert "MY-PFX" not in out
    # Same token used in declaration and reference.
    assert out.count("PREFIX_001") == 2


def test_prefix_list_description_form_collected(sanitizer):
    # `ip prefix-list NAME description ...` is also a declaration form.
    out = sanitizer.sanitize(
        "ip prefix-list MY-PFX description some prefix list\n"
        "ip prefix-list MY-PFX seq 10 permit 0.0.0.0/0\n"
    )
    assert "MY-PFX" not in out
    assert out.count("PREFIX_001") == 2


def test_community_list_declaration_redacted(sanitizer):
    out = sanitizer.sanitize(
        "ip community-list standard ATT-AGGREGATE permit 797:7\n"
    )
    assert "ATT-AGGREGATE" not in out
    assert "CMTYLIST_001" in out


def test_community_list_expanded_form(sanitizer):
    out = sanitizer.sanitize(
        "ip community-list expanded MY-COMMUNITY permit _65001_\n"
    )
    assert "MY-COMMUNITY" not in out
    assert "CMTYLIST_001" in out


def test_community_list_referenced_in_route_map(sanitizer):
    block = (
        "ip community-list standard MY-COMMUNITY permit 65001:100\n"
        "route-map MAP-A permit 10\n"
        " match community MY-COMMUNITY\n"
    )
    out = sanitizer.sanitize(block)
    assert "MY-COMMUNITY" not in out
    assert out.count("CMTYLIST_001") == 2


def test_prefix_list_names_disabled():
    rules = {"sanitize": {**RULES["sanitize"], "prefix_list_names": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("ip prefix-list MY-PFX seq 10 permit 0.0.0.0/0\n")
    assert "MY-PFX" in out


def test_community_list_names_disabled():
    rules = {"sanitize": {**RULES["sanitize"], "community_list_names": False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize("ip community-list standard MY-COMM permit 65001:100\n")
    assert "MY-COMM" in out


def test_prefix_and_community_mappings_exported(sanitizer):
    sanitizer.sanitize(
        "ip prefix-list PFX-A seq 10 permit 10.0.0.0/24\n"
        "ip community-list standard CMTY-A permit 65001:100\n"
    )
    mappings = sanitizer.get_mappings()
    assert mappings["prefix_list_names"] == {"PFX-A": "PREFIX_001"}
    assert mappings["community_list_names"] == {"CMTY-A": "CMTYLIST_001"}


# ---------------------------------------------------------------------------
# VRF / ACL / TACACS server / policy-map name redaction. All four follow the
# generic collect-and-substitute template (longest-first, marker-protected).
# ---------------------------------------------------------------------------

def test_vrf_modern_syntax_redacted(sanitizer):
    block = (
        "vrf definition CUSTOMER-A\n"
        " rd 65001:100\n"
        "interface GigabitEthernet0/0/0\n"
        " vrf forwarding CUSTOMER-A\n"
    )
    out = sanitizer.sanitize(block)
    assert "CUSTOMER-A" not in out
    # Declaration + interface forwarding reference both use VRF_001.
    assert out.count("VRF_001") == 2


def test_vrf_legacy_syntax_redacted(sanitizer):
    out = sanitizer.sanitize(
        "ip vrf MGMT\n"
        " rd 65001:1\n"
    )
    assert "ip vrf MGMT" not in out
    assert "ip vrf VRF_001" in out


def test_acl_extended_named_redacted(sanitizer):
    block = (
        "ip access-list extended ALL-ATT-INTERNALS-ACL\n"
        " permit ip 10.0.0.0 0.255.255.255 any\n"
        "interface GigabitEthernet0/0/0\n"
        " ip access-group ALL-ATT-INTERNALS-ACL in\n"
    )
    out = sanitizer.sanitize(block)
    assert "ALL-ATT-INTERNALS-ACL" not in out
    assert out.count("ACL_001") == 2


def test_acl_standard_named_redacted(sanitizer):
    out = sanitizer.sanitize("ip access-list standard NAT-SBC-SOURCE-NETS-1\n")
    assert "NAT-SBC-SOURCE-NETS-1" not in out
    assert "ACL_001" in out


def test_acl_ipv6_named_redacted(sanitizer):
    out = sanitizer.sanitize(
        "ipv6 access-list V6-MGMT-ACL\n"
        " permit ipv6 any any\n"
    )
    assert "V6-MGMT-ACL" not in out
    assert "ACL_001" in out


def test_acl_numbered_left_alone(sanitizer):
    # `access-list 100 permit ...` (numbered ACL) — the number IS the
    # identifier; can't be tokenized without breaking the config.
    out = sanitizer.sanitize("access-list 100 permit ip any any\n")
    assert "access-list 100" in out
    assert "ACL_" not in out


def test_tacacs_server_name_redacted(sanitizer):
    block = (
        "tacacs server GTAC1\n"
        " address ipv4 10.0.0.1\n"
        " key 7 14141B180F0B\n"
        "tacacs server GTAC2\n"
        " address ipv4 10.0.0.2\n"
    )
    out = sanitizer.sanitize(block)
    assert "GTAC1" not in out
    assert "GTAC2" not in out
    assert "TACSRV_001" in out and "TACSRV_002" in out


def test_policy_map_name_redacted(sanitizer):
    block = (
        "policy-map NOTRUST\n"
        " class class-default\n"
        "  set dscp default\n"
        "interface GigabitEthernet0/0/0\n"
        " service-policy input NOTRUST\n"
    )
    out = sanitizer.sanitize(block)
    assert "NOTRUST" not in out
    assert out.count("POLICY_001") == 2


def test_policy_map_typed_form(sanitizer):
    out = sanitizer.sanitize("policy-map type inspect MY-INSPECT\n")
    assert "MY-INSPECT" not in out
    assert "POLICY_001" in out


@pytest.mark.parametrize("rule_key,sample,leak,token_prefix", [
    ("vrf_names", "vrf definition CUST-A\n", "CUST-A", "VRF_"),
    ("acl_names", "ip access-list extended MY-ACL\n", "MY-ACL", "ACL_"),
    ("tacacs_server_names", "tacacs server GTAC1\n", "GTAC1", "TACSRV_"),
    ("policy_map_names", "policy-map NOTRUST\n", "NOTRUST", "POLICY_"),
])
def test_each_bundle_rule_can_be_disabled(rule_key, sample, leak, token_prefix):
    rules = {"sanitize": {**RULES["sanitize"], rule_key: False}}
    s = CiscoConfigSanitizer(rules)
    out = s.sanitize(sample)
    assert leak in out
    assert token_prefix not in out


def test_bundle_mappings_exported(sanitizer):
    sanitizer.sanitize(
        "vrf definition CUST-A\n"
        "ip access-list extended MY-ACL\n"
        "tacacs server GTAC1\n"
        "policy-map NOTRUST\n"
    )
    mappings = sanitizer.get_mappings()
    assert mappings["vrf_names"] == {"CUST-A": "VRF_001"}
    assert mappings["acl_names"] == {"MY-ACL": "ACL_001"}
    assert mappings["tacacs_server_names"] == {"GTAC1": "TACSRV_001"}
    assert mappings["policy_map_names"] == {"NOTRUST": "POLICY_001"}


# ---------------------------------------------------------------------------
# Real rules.yaml load integration.
# ---------------------------------------------------------------------------

def test_load_rules_yaml():
    rules = load_rules("rules.yaml")
    assert "sanitize" in rules
    assert "ip_addresses" in rules["sanitize"]
    assert "domains" in rules["sanitize"]
    assert "certificates" in rules["sanitize"]
    assert "interface_descriptions" in rules["sanitize"]
    assert "bgp_descriptions" in rules["sanitize"]
    assert "bgp_peer_groups" in rules["sanitize"]
    assert "bgp_as_numbers" in rules["sanitize"]
    assert "crypto_map_names" in rules["sanitize"]
    assert "route_map_names" in rules["sanitize"]
    assert "banners" in rules["sanitize"]
    assert "prefix_list_names" in rules["sanitize"]
    assert "community_list_names" in rules["sanitize"]
    assert "vrf_names" in rules["sanitize"]
    assert "acl_names" in rules["sanitize"]
    assert "tacacs_server_names" in rules["sanitize"]
    assert "policy_map_names" in rules["sanitize"]


# ---------------------------------------------------------------------------
# Show-run preamble strip (issue #10). Captures taken via interactive CLI
# include prompt-echo / `Building configuration...` / `Current configuration`
# lines that leak the device hostname through the prompt. Strip leading-edge
# noise before any other pass runs.
# ---------------------------------------------------------------------------

def test_preamble_prompt_echo_stripped(sanitizer):
    config = (
        "SECRET-EDGE-99#show running-config\n"
        "Building configuration...\n"
        "\n"
        "Current configuration : 12345 bytes\n"
        "!\n"
        "version 16.3\n"
        "service password-encryption\n"
    )
    out = sanitizer.sanitize(config)
    # Distinctive hostname from prompt does not survive.
    assert "SECRET-EDGE-99" not in out
    assert "Building configuration" not in out
    assert "Current configuration" not in out
    # Output begins at first `!` or `version`.
    first_line = out.splitlines()[0]
    assert first_line == "!" or first_line.startswith("version ")


def test_preamble_strip_noop_on_clean_config(sanitizer):
    # Real config files that start directly at `version` have no preamble.
    # Pre-pass must be a no-op and leave the first line intact.
    config = "version 16.3\nservice password-encryption\n"
    out = sanitizer.sanitize(config)
    assert out.startswith("version 16.3")


def test_preamble_strip_noop_when_starts_mid_config(sanitizer):
    # A config fragment starting with `interface X` and containing bare `!`
    # separators later must NOT be stripped to the first `!` — that would
    # nuke the interface block. Leading-edge-only strip means first line is
    # already real config, so nothing is dropped.
    config = (
        "interface GigabitEthernet0/0/0\n"
        " ip address 10.0.0.1 255.255.255.0\n"
        "!\n"
        "interface GigabitEthernet0/0/1\n"
    )
    out = sanitizer.sanitize(config)
    assert out.startswith("interface GigabitEthernet0/0/0")


def test_preamble_prompt_variants(sanitizer):
    # `sh run`, `sh running-config`, user-mode `>` prompt all variant forms.
    for prompt_line in (
        "ROUTER>show running-config",
        "ROUTER#sh run",
        "ROUTER(config)#show run",
    ):
        config = f"{prompt_line}\n!\nversion 16.3\n"
        out = sanitizer.sanitize(config)
        assert "ROUTER" not in out, f"hostname leaked for prompt: {prompt_line!r}"


# ---------------------------------------------------------------------------
# Change-tracking header comments (issue #11). IOS emits `! Last configuration
# change ... by <user>` and `! NVRAM config last updated ... by <user>` at the
# top of every running-config and leaks the last-modifier's operator ID. Drop
# these comments entirely — they carry no config state.
# ---------------------------------------------------------------------------

def test_change_tracking_comments_dropped(sanitizer):
    config = (
        "!\n"
        "! Last configuration change at 05:59:34 UTC Fri Apr 10 2026 by mc0823\n"
        "! NVRAM config last updated at 06:11:47 UTC Fri Apr 10 2026 by mc0823\n"
        "!\n"
        "version 16.3\n"
    )
    out = sanitizer.sanitize(config)
    assert "mc0823" not in out
    assert "Last configuration change" not in out
    assert "NVRAM config last updated" not in out
    # Surrounding `!` separators and `version` preserved.
    assert "version 16.3" in out


def test_change_tracking_drops_regardless_of_operator_id_shape(sanitizer):
    # Org-specific username conventions vary — the drop must not depend on
    # the shape of the trailing token.
    config = (
        "! Last configuration change at 12:00:00 UTC Mon Jan 1 2024 by jdoe\n"
        "! Last configuration change at 12:00:00 UTC Mon Jan 1 2024 by sv-deploy-svc\n"
        "! NVRAM config last updated at 12:00:00 UTC Mon Jan 1 2024 by ADMIN_001\n"
        "version 16.3\n"
    )
    out = sanitizer.sanitize(config)
    assert "jdoe" not in out
    assert "sv-deploy-svc" not in out
    assert "ADMIN_001" not in out


def test_similar_comment_without_by_clause_preserved(sanitizer):
    # An operator-authored comment without a trailing `by <user>` clause is
    # not IOS change-tracking metadata — leave it alone.
    config = (
        "! Migration notes: see ticket CHG-12345\n"
        "version 16.3\n"
    )
    out = sanitizer.sanitize(config)
    assert "Migration notes" in out


# ---------------------------------------------------------------------------
# Combined preamble + change-tracking fixture (issues #10 + #11 end-to-end).
# ---------------------------------------------------------------------------

def test_show_run_with_prompt_fixture():
    # End-to-end assertion requires the hostname rule to be on — otherwise
    # the `hostname SECRET-EDGE-99` line in the body would legitimately
    # leave the hostname visible. The prompt-strip (issue #10) addresses
    # the leak at the TOP of the file; the normal hostname pass addresses
    # the body. Both together → hostname gone everywhere.
    rules = {"sanitize": {**RULES["sanitize"], "hostname": True}}
    s = CiscoConfigSanitizer(rules)
    path = Path(__file__).parent / "fixtures" / "sanitizer" / "show_run_with_prompt.txt"
    out = s.sanitize(path.read_text())
    # Distinctive hostname from prompt line and body does not survive anywhere.
    assert "SECRET-EDGE-99" not in out
    # Operator username from change-tracking headers does not survive.
    assert "mc0823" not in out
    # Preamble noise stripped.
    assert "Building configuration" not in out
    assert "Current configuration" not in out
    # Output begins at `!` or `version`.
    first_line = out.splitlines()[0]
    assert first_line == "!" or first_line.startswith("version ")


# ---------------------------------------------------------------------------
# Route-map whole-identifier collapse (issue #12). When a route-map name
# embeds a peer-group name (e.g. `ATT-TO-EBIZ-PMTR`), compound-name
# substitution must run BEFORE atomic-name substitution; otherwise peer-group
# rewrites `EBIZ-PMTR → PG_NNN` first, the full route-map identifier no
# longer matches, and the `ATT` organizational wrapper leaks through.
# ---------------------------------------------------------------------------

def test_route_map_wrapping_peer_group_collapses_whole(sanitizer):
    block = (
        "router bgp 797\n"
        " neighbor EBIZ-PMTR peer-group\n"
        "route-map ATT-TO-EBIZ-PMTR permit 10\n"
        "router bgp 797\n"
        " neighbor 192.0.2.1 route-map ATT-TO-EBIZ-PMTR out\n"
    )
    out = sanitizer.sanitize(block)
    # Organizational wrapper must not leak.
    assert "ATT" not in out
    # Full peer-group name also gone.
    assert "EBIZ-PMTR" not in out
    # Route-map declaration and reference collapse to the SAME token.
    assert out.count("RTMAP_001") == 2


def test_route_map_peer_group_first_form(sanitizer):
    # `<PEERGROUP>-TO-<SUFFIX>` shape — peer-group as prefix, wrapper as suffix.
    block = (
        "router bgp 797\n"
        " neighbor EBIZ-PMTR peer-group\n"
        "route-map EBIZ-PMTR-TO-ATT permit 10\n"
    )
    out = sanitizer.sanitize(block)
    assert "ATT" not in out
    assert "EBIZ-PMTR" not in out
    assert "RTMAP_001" in out


def test_route_map_multiple_wrapper_shapes(sanitizer):
    # Mirrors the 73-occurrence production leak: many route-map names each
    # wrap peer-group names with varying prefixes/suffixes. Post-fix, every
    # wrapper substring (ACMECORP, PARTNERX, SITE1) is gone from output.
    block = (
        "router bgp 797\n"
        " neighbor INTERNAL-PG peer-group\n"
        " neighbor EDGE-PG peer-group\n"
        "route-map ACMECORP-TO-INTERNAL-PG permit 10\n"
        "route-map INTERNAL-PG-TO-ACMECORP permit 10\n"
        "route-map FROM-INTERNAL-PG permit 10\n"
        "route-map IPv6-TO-EDGE-PG permit 10\n"
        "route-map INTERNAL-PG-TEST permit 10\n"
        "route-map EDGE-PG-PARTNERX-SITE1 permit 10\n"
    )
    out = sanitizer.sanitize(block)
    for leak in ("ACMECORP", "PARTNERX", "SITE1"):
        assert leak not in out, (
            f"Leaked wrapper substring {leak!r} in sanitized output:\n{out}"
        )
    # Peer-group names inside compound route-maps also gone.
    assert "INTERNAL-PG" not in out
    assert "EDGE-PG" not in out
    # Six declared route-maps → six distinct RTMAP_NNN tokens.
    rtmap_tokens = set(re.findall(r"RTMAP_\d{3}", out))
    assert len(rtmap_tokens) == 6
