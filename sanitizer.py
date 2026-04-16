import re
from collections import defaultdict

import yaml


def load_rules(path="rules.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class TokenMapper:
    def __init__(self):
        self.maps = defaultdict(dict)
        self.counters = defaultdict(int)

    def get_token(self, category, original, prefix):
        if original not in self.maps[category]:
            self.counters[category] += 1
            self.maps[category][original] = f"{prefix}_{self.counters[category]:03d}"
        return self.maps[category][original]

    def export_mappings(self):
        return {category: dict(mapping) for category, mapping in self.maps.items()}


# Pattern design:
#
# Every entry captures exactly three groups — (prefix, secret_value, suffix) —
# so _sanitize_secrets can replace only the secret_value regardless of what
# encryption-type digit or metadata surrounds it.
#
# The optional `(?:\s+\d+)?` between the keyword and the secret absorbs Cisco's
# encryption-type indicator (0/5/6/7/8/9) into the prefix, so the next \S+ is
# guaranteed to be the actual secret — not the type digit. Historically the
# generic `\bpassword\s+(\S+)` fallbacks had this backwards: they captured the
# digit as the "secret" and left the real hash in the trailing group. That
# produced output like `password <REDACTED_PASSWORD> 14141B180F0B` — a leak.
#
# Patterns are checked in order; first match wins. No blind fallbacks:
# every command that carries a secret gets an explicit pattern. Lines that
# merely contain the words "password" / "secret" / "key" (descriptions,
# `service password-encryption`, `crypto key generate`, `key chain NAME`,
# unindented `key <id>` rows) are left alone.
SECRET_LINE_PATTERNS = [
    # enable secret|password [N] VALUE
    (re.compile(
        r'^(\s*enable\s+(?:secret|password)(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'ENABLE_SECRET'),

    # username X [privilege N] (secret|password) [N] VALUE
    (re.compile(
        r'^(\s*username\s+\S+(?:\s+privilege\s+\d+)?\s+(?:secret|password)(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'USER_SECRET'),

    # snmp-server community NAME [access-list]
    (re.compile(
        r'^(\s*snmp-server\s+community\s+)(\S+)(.*)$',
        re.IGNORECASE), 'SNMP_COMMUNITY'),

    # snmp-server host IP ... version N COMMUNITY ...
    (re.compile(
        r'^(\s*snmp-server\s+host\s+\S+(?:\s+\S+)*?\s+version\s+\S+\s+)(\S+)(.*)$',
        re.IGNORECASE), 'SNMP_COMMUNITY'),

    # Legacy global AAA keys (pre-'radius server NAME' syntax)
    (re.compile(
        r'^(\s*tacacs-server\s+key(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'TACACS_KEY'),
    (re.compile(
        r'^(\s*radius-server\s+key(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'RADIUS_KEY'),

    # BGP neighbor password
    (re.compile(
        r'^(\s*neighbor\s+\S+\s+password(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'BGP_PASSWORD'),

    # OSPF authentication-key / message-digest-key
    (re.compile(
        r'^(\s*ip\s+ospf\s+authentication-key(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'OSPF_AUTH'),
    (re.compile(
        r'^(\s*ip\s+ospf\s+message-digest-key\s+\d+\s+md5(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'OSPF_MD5'),

    # crypto isakmp key VALUE (address|hostname) X
    (re.compile(
        r'^(\s*crypto\s+isakmp\s+key(?:\s+\d+)?\s+)(\S+)(\s+(?:address|hostname)\s+.*)$',
        re.IGNORECASE), 'ISAKMP_KEY'),

    # IKEv2 / general pre-shared-key (keyring context)
    (re.compile(
        r'^(\s*pre-shared-key(?:\s+(?:local|remote))?(?:\s+(?:address|hostname)\s+\S+(?:\s+\S+)?)?(?:\s+(?:hex|\d+))?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'PRESHARED_KEY'),

    # PPP CHAP password
    (re.compile(
        r'^(\s*ppp\s+chap\s+password(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'CHAP_PASSWORD'),

    # WPA PSK (wireless SSID config)
    (re.compile(
        r'^(\s*wpa-psk\s+(?:ascii|hex)(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'WPA_PSK'),

    # key-string under key-chain > key > key-string
    (re.compile(
        r'^(\s*key-string(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'KEY_STRING'),

    # Nested `key N VALUE` under a 'radius server NAME' / 'tacacs server NAME'
    # block. Requires leading indentation AND an explicit encryption-type digit
    # so 'key chain NAME' (top-level, no indent) and 'key 1' (key-ID under a
    # key chain, no trailing value) cannot match.
    (re.compile(
        r'^(\s+key\s+\d+\s+)(\S+)(.*)$',
        re.IGNORECASE), 'AAA_NESTED_KEY'),

    # line-block password: indented 'password [N] VALUE' under line vty/con/aux.
    # Excludes top-level `ip ftp password ...` (which has no leading indent in
    # real configs) and descriptions (which start with `description`).
    (re.compile(
        r'^(\s+password(?:\s+\d+)?\s+)(\S+)(.*)$',
        re.IGNORECASE), 'LINE_PASSWORD'),
]


class CiscoConfigSanitizer:
    IPV4_PATTERN = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
    # Recognize already-redacted markers so we don't re-tokenize them on a
    # second pass and so IP substitution skips over them.
    REDACTED_MARKER = re.compile(r'<REDACTED_[A-Z_]+>')

    def __init__(self, rules):
        self.rules = rules.get("sanitize", {})
        self.mapper = TokenMapper()

    def sanitize(self, config_text):
        return "\n".join(self._sanitize_line(line) for line in config_text.splitlines())

    def _sanitize_line(self, line):
        if self.rules.get("hostname", True):
            line = self._sanitize_hostname(line)
        if self.rules.get("usernames", True):
            line = self._sanitize_username_identity(line)
        if (self.rules.get("enable_secrets", True)
                or self.rules.get("crypto_keys", True)
                or self.rules.get("tacacs_radius_keys", True)
                or self.rules.get("snmp_communities", True)):
            line = self._sanitize_secrets(line)
        if self.rules.get("snmp_communities", True):
            line = self._sanitize_snmp_locations(line)
        if self.rules.get("email_addresses", True):
            line = self._sanitize_emails(line)
        if self.rules.get("ip_addresses", True):
            line = self._sanitize_ipv4(line)
        return line

    def _sanitize_hostname(self, line):
        match = re.match(r'^(hostname\s+)(\S+)(.*)$', line, re.IGNORECASE)
        if not match:
            return line
        prefix, hostname, suffix = match.groups()
        token = self.mapper.get_token("hostnames", hostname, "HOST")
        return f"{prefix}{token}{suffix}"

    def _sanitize_username_identity(self, line):
        match = re.match(r'^(username\s+)(\S+)(.*)$', line, re.IGNORECASE)
        if not match:
            return line
        prefix, username, suffix = match.groups()
        token = self.mapper.get_token("usernames", username, "USER")
        return f"{prefix}{token}{suffix}"

    def _sanitize_secrets(self, line):
        for pattern, label in SECRET_LINE_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            prefix, secret, suffix = match.groups()
            # Defensive: if secret is ALREADY a redaction marker, this line
            # must have been sanitized on a previous pass — leave it alone.
            if self.REDACTED_MARKER.fullmatch(secret):
                return line
            return f"{prefix}<REDACTED_{label}>{suffix}"
        return line

    def _sanitize_snmp_locations(self, line):
        match = re.match(
            r'^(snmp-server\s+(?:contact|location)\s+)(.+)$',
            line,
            re.IGNORECASE,
        )
        if not match:
            return line
        prefix, value = match.groups()
        token = self.mapper.get_token("snmp_meta", value, "SNMP_META")
        return f"{prefix}{token}"

    def _sanitize_emails(self, line):
        return self.EMAIL_PATTERN.sub(
            lambda m: self.mapper.get_token("emails", m.group(0), "EMAIL"),
            line,
        )

    def _sanitize_ipv4(self, line):
        # Split the line around already-redacted markers so IP tokenization
        # cannot accidentally rewrite tokens inside them.
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_ip(segment):
            def repl(m):
                ip = m.group(0)
                if ip == "0.0.0.0":
                    return ip
                return self.mapper.get_token("ipv4", ip, "IP")
            return self.IPV4_PATTERN.sub(repl, segment)

        rebuilt = [sub_ip(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_ip(next_part))
        return "".join(rebuilt)

    def get_mappings(self):
        return self.mapper.export_mappings()
