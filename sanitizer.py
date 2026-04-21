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

    # license udi pid <PID> sn <SERIAL>
    # PID is preserved deliberately — it identifies the platform family and is
    # useful for refresh sizing. The serial is the device-unique hardware ID.
    (re.compile(
        r'^(\s*license\s+udi\s+pid\s+\S+\s+sn\s+)(\S+)(.*)$',
        re.IGNORECASE), 'LICENSE_SERIAL'),
]


class CiscoConfigSanitizer:
    IPV4_PATTERN = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
    # IPv6: full 8-group form, or any compressed form using `::`. Lookarounds
    # gate on hex/colon to keep MAC-like or longer hex strings from accidentally
    # matching as a sub-region.
    IPV6_PATTERN = re.compile(
        r'(?<![0-9A-Fa-f:])'
        r'(?:'
        r'(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}'
        r'|(?:[0-9A-Fa-f]{1,4}:){1,7}:'
        r'|(?:[0-9A-Fa-f]{1,4}:){1,6}(?::[0-9A-Fa-f]{1,4})+'
        r'|::(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?'
        r')'
        r'(?![0-9A-Fa-f:])'
    )
    EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
    # Matches `ip domain name|list` and the older hyphenated `ip domain-name|list`.
    DOMAIN_DECLARATION_PATTERN = re.compile(
        r'^\s*ip\s+domain[-\s](?:name|list)\s+(\S+)\s*$',
        re.IGNORECASE,
    )
    # Header of a single cert inside a `crypto pki certificate chain` (or
    # trustpool) block. Always indented; trailing token is the cert serial.
    # Variants: `certificate <serial>`, `certificate ca <serial>`,
    # `certificate self-signed <serial>`, `certificate rollover <serial>`.
    CERT_HEADER_PATTERN = re.compile(
        r'^(\s+)certificate(?:\s+(?:ca|self-signed|rollover))?\s+\S+\s*$',
        re.IGNORECASE,
    )
    CERT_QUIT_PATTERN = re.compile(r'^\s*quit\s*$', re.IGNORECASE)
    INTERFACE_HEADER_PATTERN = re.compile(r'^interface\s+\S+', re.IGNORECASE)
    INTERFACE_DESCRIPTION_PATTERN = re.compile(r'^(\s+description\s+)\S.*$', re.IGNORECASE)

    # BGP neighbor description: `<indent>neighbor <peer> description <text>`.
    # Stateless — `neighbor X description` only appears under `router bgp`.
    BGP_DESCRIPTION_PATTERN = re.compile(
        r'^(\s+neighbor\s+\S+\s+description\s+)\S.*$', re.IGNORECASE
    )

    # Peer-group declaration: `<indent>neighbor <NAME> peer-group` (no trailing args).
    BGP_PEER_GROUP_DECLARATION_PATTERN = re.compile(
        r'^\s+neighbor\s+(\S+)\s+peer-group\s*$', re.IGNORECASE
    )
    # Peer-group assignment: `<indent>neighbor <peer> peer-group <NAME>` (the
    # peer is usually an IP/IPv6 here; the trailing token is the group name).
    BGP_PEER_GROUP_ASSIGNMENT_PATTERN = re.compile(
        r'^\s+neighbor\s+\S+\s+peer-group\s+(\S+)\s*$', re.IGNORECASE
    )

    # AS-number contexts. Each captures (prefix, asn, suffix) for in-place
    # substitution. Listed in match order; first hit wins per line.
    BGP_AS_PATTERNS = [
        re.compile(r'^(\s*router\s+bgp\s+)(\d+)(.*)$', re.IGNORECASE),
        re.compile(r'^(\s*neighbor\s+\S+\s+remote-as\s+)(\d+)(.*)$', re.IGNORECASE),
        re.compile(r'^(\s*neighbor\s+\S+\s+local-as\s+)(\d+)(.*)$', re.IGNORECASE),
        re.compile(r'^(\s*redistribute\s+bgp\s+)(\d+)(.*)$', re.IGNORECASE),
        re.compile(r'^(\s*bgp\s+confederation\s+identifier\s+)(\d+)(.*)$', re.IGNORECASE),
        # route-target / rd carry an extended-community in `<ASN>:<ID>` form.
        re.compile(
            r'^(\s*route-target\s+(?:import|export|both)\s+)(\d+)(:.+)$',
            re.IGNORECASE,
        ),
        re.compile(r'^(\s*rd\s+)(\d+)(:.+)$', re.IGNORECASE),
    ]
    # `bgp confederation peers <ASN> [<ASN> …]` — multiple AS numbers per line.
    BGP_CONFED_PEERS_PATTERN = re.compile(
        r'^(\s*bgp\s+confederation\s+peers\s+)(.+)$', re.IGNORECASE
    )

    # Crypto map name detection. Both forms collect the same name into one
    # token category so interface-applied names trace back to declarations.
    CRYPTO_MAP_DECLARATION_PATTERN = re.compile(
        r'^crypto\s+(?:dynamic-)?map\s+(\S+)\s+\d+', re.IGNORECASE
    )
    CRYPTO_MAP_INTERFACE_PATTERN = re.compile(
        r'^\s+(?:ipv6\s+)?crypto\s+map\s+(\S+)\s*$', re.IGNORECASE
    )

    # Route-map declaration: `route-map <NAME> {permit|deny} <SEQ>`.
    ROUTE_MAP_DECLARATION_PATTERN = re.compile(
        r'^route-map\s+(\S+)\s+(?:permit|deny)\s+\d+', re.IGNORECASE
    )

    # Banner opening: `banner <type> <DELIM>`. Body extends to a line whose
    # content equals <DELIM>. The captured delimiter is per-banner — most
    # configs use `^C` (literal caret-C) but `#`, `!`, etc. are valid.
    BANNER_OPEN_PATTERN = re.compile(
        r'^banner\s+(motd|exec|login|incoming|prompt-timeout|slip-ppp)\s+(\S+.*?)\s*$',
        re.IGNORECASE,
    )
    # `bgp inject-map <NAME> exist-map <NAME> [copy-attributes]` — both names
    # are route-maps. Captures both so unreferenced inject-map targets are
    # still tokenized even if no `route-map NAME` declaration exists.
    BGP_INJECT_MAP_PATTERN = re.compile(
        r'^\s+bgp\s+inject-map\s+(\S+)\s+exist-map\s+(\S+)', re.IGNORECASE
    )

    # Prefix-list declaration: `ip prefix-list <NAME> {seq N (permit|deny) ... | (permit|deny) ... | description ...}`.
    PREFIX_LIST_DECLARATION_PATTERN = re.compile(
        r'^ipv?6?\s*prefix-list\s+(\S+)\s+(?:seq\s+\d+\s+)?(?:permit|deny|description)\b',
        re.IGNORECASE,
    )
    # Community-list declaration: `ip community-list {standard|expanded|<NUM>} <NAME> {permit|deny} ...`.
    COMMUNITY_LIST_DECLARATION_PATTERN = re.compile(
        r'^ip\s+community-list\s+(?:standard|expanded|\d+)\s+(\S+)\s+(?:permit|deny)\b',
        re.IGNORECASE,
    )
    # VRF declaration: `ip vrf <NAME>` (older) or `vrf definition <NAME>` (modern).
    VRF_DECLARATION_PATTERN = re.compile(
        r'^(?:ip\s+vrf|vrf\s+definition)\s+(\S+)\s*$', re.IGNORECASE,
    )
    # Named ACL declaration. `ip access-list {standard|extended} <NAME>` and the
    # IPv6 variant `ipv6 access-list <NAME>`. Numbered ACLs are skipped (the
    # number IS the identifier and changing it breaks the config).
    ACL_DECLARATION_PATTERN = re.compile(
        r'^ip\s+access-list\s+(?:standard|extended)\s+(\S+)\s*$', re.IGNORECASE,
    )
    ACL_V6_DECLARATION_PATTERN = re.compile(
        r'^ipv6\s+access-list\s+(\S+)\s*$', re.IGNORECASE,
    )
    # TACACS server declaration: `tacacs server <NAME>`. Older `tacacs-server
    # host <IP>` form has no symbolic name (IP already redacted).
    TACACS_SERVER_DECLARATION_PATTERN = re.compile(
        r'^tacacs\s+server\s+(\S+)\s*$', re.IGNORECASE,
    )
    # Policy-map declaration: `policy-map <NAME>` and the typed variant
    # `policy-map type <TYPE> <NAME>`.
    POLICY_MAP_DECLARATION_PATTERN = re.compile(
        r'^policy-map(?:\s+type\s+\S+)?\s+(\S+)\s*$', re.IGNORECASE,
    )

    # Preamble noise emitted before the real config body when a config is
    # captured via interactive CLI (`show run`). The prompt-echo line leaks
    # the unredacted hostname. Stripped leading-edge only so that bare `!`
    # separators mid-config stay intact.
    PREAMBLE_NOISE_PATTERNS = [
        # `<hostname>#show running-config` / `sh run` prompt-echo.
        re.compile(r'^\S+[#>]\s*(?:show|sh)\s+run', re.IGNORECASE),
        re.compile(r'^Building\s+configuration\.\.\.', re.IGNORECASE),
        re.compile(r'^Current\s+configuration\s*:\s*\d+\s+bytes\s*$', re.IGNORECASE),
    ]
    # Change-tracking header comments. IOS writes these at the top of the
    # config and they include the operator username who last modified the
    # device — directly attributable PII. Dropped entirely per issue #11.
    CHANGE_TRACKING_COMMENT_PATTERN = re.compile(
        r'^!\s+(?:Last\s+configuration\s+change|NVRAM\s+config\s+last\s+updated)\b'
        r'.*\bby\s+\S+\s*$',
        re.IGNORECASE,
    )

    # Recognize already-redacted markers so we don't re-tokenize them on a
    # second pass and so IP substitution skips over them.
    REDACTED_MARKER = re.compile(r'<REDACTED_[A-Z_]+>')
    DOMAIN_TOKEN_MARKER = re.compile(r'\bDOMAIN_\d{3,}\b')

    def __init__(self, rules):
        self.rules = rules.get("sanitize", {})
        self.mapper = TokenMapper()
        # Populated by sanitize() pre-pass: list of (compiled_regex, token)
        # tuples sorted longest-domain-first, so subdomain matches replace
        # before the shorter root would consume their suffix.
        self._domain_substitutions = []
        # Same shape: longest-first peer-group name substitutions.
        self._peer_group_substitutions = []
        # Same shape: longest-first crypto map name substitutions.
        self._crypto_map_substitutions = []
        # Same shape: longest-first route-map name substitutions.
        self._route_map_substitutions = []
        # Same shape: longest-first prefix-list name substitutions.
        self._prefix_list_substitutions = []
        # Same shape: longest-first community-list name substitutions.
        self._community_list_substitutions = []
        # Same shape: longest-first substitutions for vrf / acl / tacacs
        # server / policy-map names. All built via _collect_named_decl.
        self._vrf_substitutions = []
        self._acl_substitutions = []
        self._tacacs_server_substitutions = []
        self._policy_map_substitutions = []

    def sanitize(self, config_text):
        # Pre-pass: strip show-run CLI preamble (issue #10) and drop
        # change-tracking header comments that leak the last-modifier's
        # username (issue #11). Runs before any collection pass so the
        # hostname embedded in the CLI prompt is gone before the hostname
        # tokenizer sees the real `hostname X` line.
        config_text = self._strip_preamble(config_text)
        if self.rules.get("domains", True):
            self._collect_domains(config_text)
        if self.rules.get("bgp_peer_groups", True):
            self._collect_peer_groups(config_text)
        if self.rules.get("crypto_map_names", True):
            self._collect_crypto_maps(config_text)
        if self.rules.get("route_map_names", True):
            self._collect_route_maps(config_text)
        if self.rules.get("prefix_list_names", True):
            self._prefix_list_substitutions = self._collect_named_decl(
                config_text,
                self.PREFIX_LIST_DECLARATION_PATTERN,
                "prefix_list_names",
                "PREFIX",
            )
        if self.rules.get("community_list_names", True):
            self._community_list_substitutions = self._collect_named_decl(
                config_text,
                self.COMMUNITY_LIST_DECLARATION_PATTERN,
                "community_list_names",
                "CMTYLIST",
            )
        if self.rules.get("vrf_names", True):
            self._vrf_substitutions = self._collect_named_decl(
                config_text, self.VRF_DECLARATION_PATTERN, "vrf_names", "VRF",
            )
        if self.rules.get("acl_names", True):
            self._acl_substitutions = (
                self._collect_named_decl(
                    config_text, self.ACL_DECLARATION_PATTERN, "acl_names", "ACL",
                )
                + self._collect_named_decl(
                    config_text, self.ACL_V6_DECLARATION_PATTERN, "acl_names", "ACL",
                )
            )
            # Re-sort the merged list so longest-first ordering still holds
            # across the two source patterns.
            self._acl_substitutions.sort(
                key=lambda pair: len(pair[0].pattern), reverse=True,
            )
        if self.rules.get("tacacs_server_names", True):
            self._tacacs_server_substitutions = self._collect_named_decl(
                config_text,
                self.TACACS_SERVER_DECLARATION_PATTERN,
                "tacacs_server_names",
                "TACSRV",
            )
        if self.rules.get("policy_map_names", True):
            self._policy_map_substitutions = self._collect_named_decl(
                config_text,
                self.POLICY_MAP_DECLARATION_PATTERN,
                "policy_map_names",
                "POLICY",
            )
        return "\n".join(self._sanitize_lines(config_text.splitlines()))

    def _strip_preamble(self, config_text):
        """Drop show-run CLI preamble + change-tracking header comments.

        1. Walk leading lines and drop each one that matches a known
           preamble-noise shape (prompt-echo, `Building configuration...`,
           `Current configuration : N bytes`) or is blank. Stop at the first
           line that doesn't match — that's the start of real config content
           (typically a `!` separator or `version N`). This is leading-edge
           only so mid-config `!` separators and blank lines aren't touched.
        2. Drop `! Last configuration change ... by <user>` and `! NVRAM
           config last updated ... by <user>` comments wherever they appear.
           These carry no config state and leak the last-modifier's operator
           ID.
        """
        lines = config_text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip() or any(
                p.match(line) for p in self.PREAMBLE_NOISE_PATTERNS
            ):
                i += 1
                continue
            break
        return "\n".join(
            line for line in lines[i:]
            if not self.CHANGE_TRACKING_COMMENT_PATTERN.match(line)
        )

    def _collect_named_decl(self, config_text, pattern, category, prefix):
        """Generic single-capture name collector. Returns longest-first
        substitution list of (compiled_word_boundary_regex, token) tuples.
        Used for prefix-list, community-list, and other name categories that
        share the same `<keyword> <NAME> ...` shape."""
        seen = []
        for line in config_text.splitlines():
            m = pattern.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.append(name)
            self.mapper.get_token(category, name, prefix)
        return [
            (re.compile(r'\b' + re.escape(n) + r'\b'),
             self.mapper.get_token(category, n, prefix))
            for n in sorted(seen, key=len, reverse=True)
        ]

    def _collect_peer_groups(self, config_text):
        seen = []
        for line in config_text.splitlines():
            m = self.BGP_PEER_GROUP_DECLARATION_PATTERN.match(line)
            if not m:
                m = self.BGP_PEER_GROUP_ASSIGNMENT_PATTERN.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.append(name)
            self.mapper.get_token("bgp_peer_groups", name, "PG")
        self._peer_group_substitutions = [
            (re.compile(r'\b' + re.escape(n) + r'\b'),
             self.mapper.get_token("bgp_peer_groups", n, "PG"))
            for n in sorted(seen, key=len, reverse=True)
        ]

    def _collect_crypto_maps(self, config_text):
        seen = []
        for line in config_text.splitlines():
            m = self.CRYPTO_MAP_DECLARATION_PATTERN.match(line)
            if not m:
                m = self.CRYPTO_MAP_INTERFACE_PATTERN.match(line)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                continue
            seen.append(name)
            self.mapper.get_token("crypto_map_names", name, "CRYPTOMAP")
        self._crypto_map_substitutions = [
            (re.compile(r'\b' + re.escape(n) + r'\b'),
             self.mapper.get_token("crypto_map_names", n, "CRYPTOMAP"))
            for n in sorted(seen, key=len, reverse=True)
        ]

    def _collect_route_maps(self, config_text):
        seen = []
        def _add(name):
            if name and name not in seen:
                seen.append(name)
                self.mapper.get_token("route_map_names", name, "RTMAP")
        for line in config_text.splitlines():
            m = self.ROUTE_MAP_DECLARATION_PATTERN.match(line)
            if m:
                _add(m.group(1))
                continue
            m = self.BGP_INJECT_MAP_PATTERN.match(line)
            if m:
                _add(m.group(1))
                _add(m.group(2))
        self._route_map_substitutions = [
            (re.compile(r'\b' + re.escape(n) + r'\b'),
             self.mapper.get_token("route_map_names", n, "RTMAP"))
            for n in sorted(seen, key=len, reverse=True)
        ]

    def _sanitize_lines(self, lines):
        """Iterate lines with stateful handling for multi-line cert blobs and
        interface-block scoped description redaction.

        Cert blob: a `certificate <type> <serial>` line opens a hex blob that
        runs until a line stripping to `quit`. The blob is replaced by a
        single `<REDACTED_CERTIFICATE>` placeholder at the hex indent; the
        header and the `quit` terminator are preserved.

        Interface descriptions: while inside an `interface <name>` block,
        `description <text>` lines are replaced by `description
        <REDACTED_DESCRIPTION>`. Block exit is signaled by any column-0 line
        that is not blank and not a `!` separator.
        """
        certs_enabled = self.rules.get("certificates", True)
        descs_enabled = self.rules.get("interface_descriptions", True)
        banners_enabled = self.rules.get("banners", True)
        # mode: "normal" | "cert_first_hex" (waiting for first hex line to
        # capture indent) | "cert_skip" (placeholder emitted, swallow rest)
        # | "banner_skip" (placeholder emitted, swallow until closing delim).
        mode = "normal"
        in_interface = False
        banner_delim = None
        for line in lines:
            if mode == "banner_skip":
                if line.strip() == banner_delim:
                    yield line
                    mode = "normal"
                    banner_delim = None
                continue
            if mode == "cert_first_hex":
                if self.CERT_QUIT_PATTERN.match(line):
                    yield "  <REDACTED_CERTIFICATE>"
                    yield line
                    mode = "normal"
                    continue
                indent_match = re.match(r'^(\s+)', line)
                indent = indent_match.group(1) if indent_match else "  "
                yield f"{indent}<REDACTED_CERTIFICATE>"
                mode = "cert_skip"
                continue
            if mode == "cert_skip":
                if self.CERT_QUIT_PATTERN.match(line):
                    yield line
                    mode = "normal"
                continue
            if certs_enabled and self.CERT_HEADER_PATTERN.match(line):
                yield line
                mode = "cert_first_hex"
                continue
            if banners_enabled:
                bm = self.BANNER_OPEN_PATTERN.match(line)
                if bm:
                    yield line
                    yield "<REDACTED_BANNER>"
                    banner_delim = bm.group(2)
                    mode = "banner_skip"
                    continue
            # Interface-block tracking: a column-0 non-`!` line either starts a
            # new top-level command (exit any prior interface block) or, if it
            # matches `interface <name>`, opens a new one.
            stripped = line.strip()
            if line and not line[0].isspace() and stripped and stripped != "!":
                in_interface = bool(self.INTERFACE_HEADER_PATTERN.match(stripped))
            if in_interface and descs_enabled:
                m = self.INTERFACE_DESCRIPTION_PATTERN.match(line)
                if m:
                    yield f"{m.group(1)}<REDACTED_DESCRIPTION>"
                    continue
            yield self._sanitize_line(line)

    def _collect_domains(self, config_text):
        seen = []
        for line in config_text.splitlines():
            m = self.DOMAIN_DECLARATION_PATTERN.match(line)
            if not m:
                continue
            domain = m.group(1)
            if domain in seen:
                continue
            seen.append(domain)
            # Reserve a stable token now so order matches first-seen order in
            # the file rather than longest-first iteration order.
            self.mapper.get_token("domains", domain, "DOMAIN")
        # Compile substitution patterns longest-first so e.g. `pmtr.swst.att.com`
        # matches before `att.com` would consume its suffix.
        self._domain_substitutions = [
            (re.compile(r'\b' + re.escape(d) + r'\b'),
             self.mapper.get_token("domains", d, "DOMAIN"))
            for d in sorted(seen, key=len, reverse=True)
        ]

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
        if self.rules.get("domains", True) and self._domain_substitutions:
            line = self._sanitize_domains(line)
        if self.rules.get("bgp_descriptions", True):
            line = self._sanitize_bgp_description(line)
        if self.rules.get("bgp_as_numbers", True):
            line = self._sanitize_bgp_as_numbers(line)
        # Compound names run BEFORE atomic names (peer-group, crypto-map).
        # Route-map / prefix-list / ACL identifiers routinely embed peer-group
        # names and crypto-map names — e.g. `ATT-TO-EBIZ-PMTR` (route-map
        # wrapping peer-group `EBIZ-PMTR`) or `IPvX-EBIZ-LESS-SPECIFIC-TEMP`
        # (route-map wrapping crypto-map `EBIZ`). If the atomic pass fires
        # first it rewrites those substrings in place, the compound
        # identifier no longer matches, and the organizational wrapper
        # (`ATT`, `IPvX`) leaks through. Issue #12 is the canonical instance.
        if self.rules.get("route_map_names", True) and self._route_map_substitutions:
            line = self._sanitize_route_maps(line)
        if self.rules.get("prefix_list_names", True) and self._prefix_list_substitutions:
            line = self._apply_substitutions(line, self._prefix_list_substitutions)
        if self.rules.get("community_list_names", True) and self._community_list_substitutions:
            line = self._apply_substitutions(line, self._community_list_substitutions)
        if self.rules.get("acl_names", True) and self._acl_substitutions:
            line = self._apply_substitutions(line, self._acl_substitutions)
        if self.rules.get("vrf_names", True) and self._vrf_substitutions:
            line = self._apply_substitutions(line, self._vrf_substitutions)
        if self.rules.get("tacacs_server_names", True) and self._tacacs_server_substitutions:
            line = self._apply_substitutions(line, self._tacacs_server_substitutions)
        if self.rules.get("policy_map_names", True) and self._policy_map_substitutions:
            line = self._apply_substitutions(line, self._policy_map_substitutions)
        # Atomic-name passes run AFTER compound names so they only match
        # standalone occurrences, not substrings inside compound identifiers.
        if self.rules.get("bgp_peer_groups", True) and self._peer_group_substitutions:
            line = self._sanitize_peer_groups(line)
        if self.rules.get("crypto_map_names", True) and self._crypto_map_substitutions:
            line = self._sanitize_crypto_maps(line)
        if self.rules.get("ip_addresses", True):
            line = self._sanitize_ipv4(line)
            line = self._sanitize_ipv6(line)
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

    def _sanitize_domains(self, line):
        # Skip substitution inside existing redaction markers so we don't
        # rewrite tokens like <REDACTED_FOO_BAR_COM>.
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_segment(segment):
            for pattern, token in self._domain_substitutions:
                segment = pattern.sub(token, segment)
            return segment

        rebuilt = [sub_segment(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_segment(next_part))
        return "".join(rebuilt)

    def _sanitize_bgp_description(self, line):
        m = self.BGP_DESCRIPTION_PATTERN.match(line)
        if not m:
            return line
        return f"{m.group(1)}<REDACTED_DESCRIPTION>"

    def _sanitize_peer_groups(self, line):
        # Same marker-protection split used for domains and IPs.
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_segment(segment):
            for pattern, token in self._peer_group_substitutions:
                segment = pattern.sub(token, segment)
            return segment

        rebuilt = [sub_segment(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_segment(next_part))
        return "".join(rebuilt)

    def _apply_substitutions(self, line, substitutions):
        """Apply a longest-first list of (regex, token) substitutions to a
        line, splitting around <REDACTED_*> markers so we don't rewrite
        text inside an existing redaction marker."""
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_segment(segment):
            for pattern, token in substitutions:
                segment = pattern.sub(token, segment)
            return segment

        rebuilt = [sub_segment(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_segment(next_part))
        return "".join(rebuilt)

    def _sanitize_route_maps(self, line):
        # Marker-protection split, same shape as the other name-substitution
        # passes (domains, peer-groups, crypto-maps).
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_segment(segment):
            for pattern, token in self._route_map_substitutions:
                segment = pattern.sub(token, segment)
            return segment

        rebuilt = [sub_segment(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_segment(next_part))
        return "".join(rebuilt)

    def _sanitize_crypto_maps(self, line):
        # Marker-protection split, same shape as domains/peer-groups.
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_segment(segment):
            for pattern, token in self._crypto_map_substitutions:
                segment = pattern.sub(token, segment)
            return segment

        rebuilt = [sub_segment(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_segment(next_part))
        return "".join(rebuilt)

    def _sanitize_bgp_as_numbers(self, line):
        # `bgp confederation peers <ASN> [<ASN> ...]` — variable trailing list.
        m = self.BGP_CONFED_PEERS_PATTERN.match(line)
        if m:
            prefix, tail = m.group(1), m.group(2)
            tokens = []
            for t in tail.split():
                if t.isdigit():
                    tokens.append(self.mapper.get_token("as_numbers", t, "ASN"))
                else:
                    tokens.append(t)
            return prefix + " ".join(tokens)
        for pattern in self.BGP_AS_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            prefix, asn, suffix = m.group(1), m.group(2), m.group(3)
            token = self.mapper.get_token("as_numbers", asn, "ASN")
            return f"{prefix}{token}{suffix}"
        return line

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

    def _sanitize_ipv6(self, line):
        # Same marker-protection split as IPv4 so we don't tokenize inside
        # `<REDACTED_*>` or accidentally re-tokenize an IPV6_NNN token.
        parts = self.REDACTED_MARKER.split(line)
        markers = self.REDACTED_MARKER.findall(line)

        def sub_ip(segment):
            def repl(m):
                addr = m.group(0)
                # `::` (unspecified) is the IPv6 equivalent of 0.0.0.0 — keep
                # it readable for parity with the IPv4 path.
                if addr == "::":
                    return addr
                return self.mapper.get_token("ipv6", addr.lower(), "IPV6")
            return self.IPV6_PATTERN.sub(repl, segment)

        rebuilt = [sub_ip(parts[0])]
        for marker, next_part in zip(markers, parts[1:]):
            rebuilt.append(marker)
            rebuilt.append(sub_ip(next_part))
        return "".join(rebuilt)

    def get_mappings(self):
        return self.mapper.export_mappings()
