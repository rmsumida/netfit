# NetBrain Harvest Reference

This document specifies the `show` commands that `netfit` needs from each device to enable runtime-aware platform-fit scoring (route scale, NAT scale, crypto scale, optics, license tier, CPU/memory headroom). It includes parsing requirements and sample output for each command so you can validate that NetBrain's export shape (native text or CSV) is ingestible.

Commands and sample outputs below are Cisco IOS / IOS-XE, which is the currently-supported input family. When extending coverage to NX-OS, IOS-XR, or non-Cisco platforms, add a parallel section per OS dialect rather than overloading the Cisco examples.

**Use this document when:**
- Configuring a NetBrain **CLI Command Template** (primary) or Data View Template (optional) for harvesting
- Validating a NetBrain export (native text or CSV) against the parser's expectations
- Adding a new device family or software train that may have OS-specific command/output differences

---

## NetBrain template strategy

Validated on NetBrain R12.3 (session 2026-04-16).

**Primary object:** **CLI Command Template** — best fit for batch harvesting of raw CLI output across a device group. This is the object netfit's harvest targets.

**Optional:** **Data View Template** — only useful if you want on-screen presentation or dashboarding on top of the harvest. Not required for ingestion.

**Device selection:** runtime — device group, filtered list, or map/manual selection.

**Session behavior to configure:**
- Disable paging
- Continue on failure (a single unsupported command should not abort the run)
- Preserve raw output verbatim

### Maintain platform-variant templates — don't force one universal set

IOS-XE command syntax varies meaningfully across trains. Rather than one template that tolerates per-command failures, maintain a small number of variant templates scoped by platform / software train, and let NetBrain run the variant that matches the device group.

**Known platform/train compatibility (as of 2026-04-16):**

Validated on **Cisco ASR 1013 / IOS-XE 16.03.07** — the following modern-IOS-XE syntaxes return `% Invalid input`:

| Command (modern IOS-XE) | ASR1000 16.x behavior | Working equivalent on this train |
|-------------------------|-----------------------|----------------------------------|
| `show interfaces transceiver detail` | `% Invalid input` | None found — defer optics collection |
| `show interfaces transceiver` | `% Invalid input` | None found — defer optics collection |
| `show crypto ipsec sa count` | `% Invalid input` | `show crypto ipsec sa` (parser derives count from detailed output) |
| `show license summary` | `% Invalid input` | `show license all` |

Parser-side handling of these alternates is specified under [Parser intent groups / command aliases](#parser-intent-groups--command-aliases). The parser matches on intent, not exact command string, so extending this table does **not** require new parsers — only new entries in the alias map.

### Recommended template variants

**Template 1 — Modern IOS-XE (17.x+)**

```
Template name: NETFIT_RUNTIME_MINIMAL_IOSXE_MODERN
```

Commands:
```
show running-config
show inventory
show version
show interfaces transceiver detail
show ip route summary
show ip nat statistics
show crypto ipsec sa count
show license summary
show processes cpu sorted
```

**Template 2 — ASR1000 older IOS-XE (16.x)**

```
Template name: NETFIT_RUNTIME_MINIMAL_ASR1000_16X
```

Commands:
```
show running-config
show inventory
show version
show ip route summary
show ip nat statistics
show crypto ipsec sa
show license all
show processes cpu sorted
```

Optics/transceiver collection is omitted on this variant — both tested syntaxes failed on IOS-XE 16.03.07. Hardware identity is still adequately covered by `show inventory` + `show version`. Revisit if a stable ASR-compatible optics command is identified.

**When adding a new variant:** duplicate the modern template, swap in known-working aliases from the intent-group table, test on one device, then register the template name + command set here.

---

## Export format expectations

The netfit runtime ingester (planned `runtime_loader.py`) must accept both of NetBrain's plausible export shapes. Either form carries the same logical record: `(device_name, command, timestamp, raw_output)`.

### Native NetBrain text export (primary, validated 2026-04-16)

NetBrain R12.3's CLI Command Template produces a text file in which each command execution is preceded by a delimiter header of the form:

```
#--- <device_name> <command text> Execute at <YYYY-MM-DD HH:MM:SS>
<raw command output, multi-line, preserved verbatim>
```

The loader splits the file on `#---` delimiter lines to recover `(device_name, command, timestamp, output)` tuples. One file may contain multiple devices and multiple commands; order is not significant.

**Header regex:**
```
^#---\s+(?P<device>\S+)\s+(?P<command>.+?)\s+Execute at\s+(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$
```

**Sample (sanitized, from ASR 1013 / IOS-XE 16.03.07):**
```
#--- deviceXXXXXXXXX show inventory Execute at 2026-04-16 17:28:29
deviceXXXXXXXXX>show inventory
NAME: "Chassis", DESCR: "Cisco ASR1013 Chassis"
PID: ASR1013 , VID: V01 , SN: XOX1234ABCD
...

#--- deviceXXXXXXXXX show version Execute at 2026-04-16 17:28:29
deviceXXXXXXXXX>show version
Cisco IOS XE Software, Version 16.03.07
...
```

**Loader responsibilities:**
- Strip the echoed command prompt (e.g., `deviceXXXXXXXXX>show inventory`) from the first line of each block before passing the body to the per-command parser.
- Normalize the captured `command` string to an **intent key** via the alias map (see [Parser intent groups / command aliases](#parser-intent-groups--command-aliases)), then dispatch to the parser keyed by that intent — not by raw command text.
- Handle the case where a command block contains an error line such as `% Invalid input detected at '^' marker.` — skip the block and emit a warning; do not attempt to parse.

### CSV format (alternative)

If NetBrain is configured to export CSV instead (or a downstream tool re-shapes the text export), the loader accepts one row per `(device, command)` pair with at least these columns:

| Column | Required? | Purpose |
|--------|-----------|---------|
| `device_name` (or `hostname`) | Yes | Used to correlate with the device's config file in `input/`. Must match the hostname found in the config (`hostname FOO` directive) — case-insensitive comparison is fine. |
| `command` | Yes | The show command exactly as it was run (e.g., `show ip route summary`). Normalized to an intent key by the alias map before dispatch. |
| `result` (or `output`) | Yes | The raw text output of the command, multi-line, preserved verbatim. |
| `timestamp` | Optional | When the command was run; used for staleness checks. |
| `vendor` / `os_version` | Optional | Helps disambiguate output dialect (IOS vs. IOS-XE vs. NX-OS) when the parser needs to branch. |

**Column-name flexibility:** the loader accepts common synonyms (`device` / `hostname` / `device_name`; `command` / `cmd`; `result` / `output` / `raw_output`). If NetBrain's export uses different names, that's configurable.

**Encoding:** UTF-8 with no BOM preferred. ANSI/cp1252 acceptable but the loader will need a charset hint.

**Multi-line result handling:** the `result` field will contain embedded newlines. The CSV export must quote these correctly per RFC 4180 (field wrapped in `"..."`, internal `"` escaped as `""`). If NetBrain replaces newlines with literal `\n` strings or strips them, that needs to be undone before ingestion.

---

## Combined harvest ingestion

When the NetBrain template harvests `show running-config` alongside the runtime show commands, the exported native-text file carries both the config body and every runtime record in one `#---`-delimited stream. Operators in that workflow point `main.py` at the single file and netfit handles the split internally — no manual pre-slicing and no second `--runtime-csv` argument.

**Auto-detection criterion.** `main.py` peeks at the input file's first non-empty line. If it matches the `#--- <device> <command> Execute at <timestamp>` header regex already owned by the loader, the file is treated as a combined harvest. Any other shape (a bare `hostname` directive, a CSV header row, arbitrary text) falls through to the existing bare-config / two-file code path. The detection logic lives in `runtime_loader.is_combined_harvest()`.

**Hostname inference precedence.** `split_combined_harvest()` resolves the target hostname in this order:
1. The `device` field of the first `show running-config` record.
2. If that field disagrees with the `hostname FOO` directive inside the extracted running-config body, the device field wins (it matches the rest of the harvest's records) and a warning is logged. This is the expected precedence — NetBrain scoped the export to that device and labeled every other record with the same device name.

**Sanitizer behavior over runtime data.** In combined-harvest mode, `process_single_device()` constructs a single `CiscoConfigSanitizer` instance and runs `sanitizer.sanitize(body)` over every runtime record *before* dispatching the body to `runtime_parsers.INTENT_PARSERS[intent]`. The same instance also sanitizes the running-config body. Because token-ID counters are instance-level on `TokenMapper`, IPs / serials / UDIs / license tokens seen across all bodies share a single monotonically-numbered keyspace, and the merged result lands in `sanitization_mappings.json` as a unified table. Three runtime-specific (prefix, secret, suffix) patterns — serial numbers (`SN:`), license UDIs (`UDI: PID:X SN:Y`), and smart-license Registration Tokens — are added alongside the existing config secret patterns; each has its own toggle in `rules.yaml`. No generic `\bpassword\b` / `\bkey\b` catch-all exists, per the existing sanitizer invariant.

**startup-config handling.** If the harvest contains `show startup-config` records, they're dropped with an INFO log line: running-config takes precedence. If *only* `show startup-config` is present (no running-config), the splitter returns `(None, None, None)` and `main.py` raises a SystemExit pointing the operator at either harvesting a running-config or using the two-file workflow.

**Multi-device error case.** If the combined harvest contains records for more than one distinct device (case-insensitive hostname compare), `split_combined_harvest()` raises `ValueError` with the device list. netfit currently handles one device per invocation; multi-device harvests are deferred to a future enhancement. The operator's remediation is either to narrow the NetBrain device scope or to pre-split the export.

**Mutual exclusion with `--runtime-csv`.** Combined-harvest auto-detect and `--runtime-csv` / `--runtime-dir` are mutually exclusive. Passing both raises SystemExit before any work is done.

---

## Tier 1 — Minimal v1 set (8 commands)

These eight commands are the smallest set that produces a meaningful runtime-aware sizing decision. Start here when configuring the NetBrain template.

### 1. `show inventory`

**Why we need it:** chassis + module PIDs, serial numbers. Establishes the current SKU baseline and the slot/module footprint that the replacement must accommodate.

**Maps to JSON:** `runtime.inventory.chassis`, `runtime.inventory.modules[]`, `runtime.inventory.serial_numbers[]`

**Sample output:**
```
NAME: "Chassis", DESCR: "Cisco ASR1001-X Chassis"
PID: ASR1001-X         , VID: V07, SN: FOX1234ABCD

NAME: "module 0", DESCR: "Cisco ASR1001-X SPA Interface Processor"
PID: ASR1001-X         , VID: , SN:

NAME: "subslot 0/0 transceiver 0", DESCR: "GE T"
PID: SFP-GE-T          , VID: V01, SN: ABC1234XYZ
```

**Parsing strategy:** stanza-based. Each entry is a `NAME` line followed by a `PID/VID/SN` line. Split on blank lines or on the `NAME:` keyword.

**Extraction targets:**
- Chassis PID (the entry where `NAME` is `"Chassis"` or matches `/chassis/i`)
- Each module PID + its `NAME` slot identifier
- Each transceiver PID (entries where `NAME` matches `/transceiver/i`) — critical for optics inventory

**Parsing complexity:** Medium. Stanza-based but stable. ~30 lines of parser code.

---

### 2. `show version`

**Why we need it:** software train, image filename, uptime, hardware revision, current license-level hint (in older trains). Confirms the device is on a supported software level and tells us what software we'd be migrating *from*.

**Maps to JSON:** `runtime.platform.software_version`, `runtime.platform.image_name`, `runtime.platform.uptime_seconds`, `runtime.platform.rommon_version`

**Sample output:**
```
Cisco IOS XE Software, Version 17.09.04a
Cisco IOS Software [Cupertino], ASR1000 Software (X86_64_LINUX_IOSD-UNIVERSALK9-M), Version 17.9.4a, RELEASE SOFTWARE (fc4)
...
ROM: IOS-XE ROMMON
hostname uptime is 1 year, 24 weeks, 3 days, 12 hours, 8 minutes
System image file is "bootflash:asr1000-universalk9.17.09.04a.SPA.bin"
...
License Level: advipservices
License Type: Smart License
Next reload license Level: advipservices
```

**Parsing strategy:** line-by-line regex. Pull keyed lines.

**Extraction targets:**
- `Version` regex: `Cisco IOS XE Software, Version (\S+)` (also handle classic IOS variant)
- `uptime` regex: `(\S+) uptime is (.+)` → parse to seconds
- `image file` regex: `System image file is "(.+)"`
- `License Level` regex: `License Level:\s+(\S+)` (legacy / non-Smart cases)

**Parsing complexity:** Easy. Single-pass scan of ~50 lines.

---

### 3. `show interfaces transceiver detail`

**Why we need it:** tells us exactly what optics are installed in every port. Critical for procurement — the replacement platform must accept the same optics SKUs, or you need to budget transceiver replacements.

**Maps to JSON:** `runtime.optics[]` (list of `{interface, pid, vendor, serial, wavelength_nm, type}` records)

**Sample output:**
```
                                          Optical    Optical
                Temperature  Voltage  Tx Power   Rx Power
Port      (Celsius)   (Volts)  (dBm)     (dBm)
--------- ----------- -------- --------- ---------
Te0/0/0     32.5        3.30   -2.4       -3.1
Te0/0/1     31.8        3.30   -2.5       -3.4

Transceiver Type: SFP-10G-LR
Vendor: CISCO-FINISAR
Vendor SN: ABC123456
...
```

(Format varies significantly between IOS-XE and NX-OS. On Catalyst 9K, `show idprom interface <name>` is sometimes preferred.)

**Parsing strategy:** stanza-based, one stanza per port. May require a separate per-port command (`show interfaces <name> transceiver detail`) if the all-port form is unavailable on the platform.

**Extraction targets:**
- Per-port: PID (`Transceiver Type`), Vendor, Serial Number, optic class (1G/10G/25G/40G/100G — derivable from PID prefix)

**Parsing complexity:** Medium-Hard. Output dialect varies. May need OS-specific branches.

**NetBrain notes:** if NetBrain can run a "for each interface" loop and concatenate per-port output, that's easier to parse than the merged form. Either is acceptable.

**Intent group:** `optics` — aliases include `show interfaces transceiver detail`, `show interfaces transceiver`, and `show idprom interface <name>`. **Validation status:** no ASR1000/IOS-XE 16.x alias has been found that works; on that train the optics intent is currently deferred and the command is omitted from the `NETFIT_RUNTIME_MINIMAL_ASR1000_16X` template. See the [alias table](#parser-intent-groups--command-aliases) for the current list.

---

### 4. `show ip route summary`

**Why we need it:** **the single biggest runtime signal.** Total IPv4 RIB size and breakdown by source protocol (BGP, OSPF, EIGRP, static, connected). Drives the route-scale ceiling check against the platform's `max_routes_ipv4`.

**Maps to JSON:** `runtime.route_table.ipv4_total`, `runtime.route_table.ipv4_by_protocol.{bgp, ospf, eigrp, static, connected}`, `runtime.route_table.ipv4_memory_bytes`

**Sample output:**
```
IP routing table name is default (0x0)
IP routing table maximum-paths is 32
Route Source    Networks    Subnets     Replicates  Overhead    Memory (bytes)
connected       0           24          0           1920        7104
static          0           17          0           1360        4488
ospf 100        12          487         0           39920       144860
  Intra-area: 312 Inter-area: 145 External-1: 30 External-2: 12
bgp 65001       18          487034      0           38964320    140772064
  External: 487023 Internal: 29 Local: 0
internal        12                                              16464
Total           42          487602      0           39007520    140940980
```

**Parsing strategy:** find the `Route Source` table, iterate each row up to the `Total` row.

**Extraction targets:**
- `Total` row → `Networks + Subnets` (column 2 + column 3) = total prefix count
- Each protocol row → per-protocol prefix count
- `Memory (bytes)` from `Total` row → memory footprint

**Parsing complexity:** Easy. Single regex per row. ~20 lines of parser code.

**Critical:** distinguish per-protocol counts from external/internal sub-counts on the indented continuation lines. Indented lines (BGP `External / Internal / Local`, OSPF `Intra/Inter/External`) are sub-totals of the protocol row above and should not be summed.

---

### 5. `show ip nat statistics`

**Why we need it:** active NAT translation count, peak, NAT type breakdown. Tells us which platform NAT capacity tier is needed (some SKUs cap at 10K translations, others at 1M+). Config alone says "ip nat inside" — runtime says "and we have 50K active translations sustained."

**Maps to JSON:** `runtime.nat.active_translations`, `runtime.nat.peak_translations`, `runtime.nat.translation_rate_per_sec`, `runtime.nat.miss_count`, `runtime.nat.hit_count`

**Sample output:**
```
Total active translations: 12453 (4 static, 12449 dynamic; 8127 extended)
Peak translations: 18900, occurred 14:23:45 ago
Outside interfaces:
  GigabitEthernet0/0/0
Inside interfaces:
  GigabitEthernet0/0/1
Hits: 4823498234  Misses: 124993
CEF Translated packets: 4823498234, CEF Punted packets: 124993
Expired translations: 4291847
Dynamic mappings:
  -- Inside Source
  [Id: 1] access-list NAT pool POOL refcount 12449
   pool POOL: netmask 255.255.255.0
        start 198.51.100.1 end 198.51.100.254
        type generic, total addresses 254, allocated 247 (97%), misses 14
  ...
```

**Parsing strategy:** keyed-line regex. Stable across IOS / IOS-XE.

**Extraction targets:**
- `Total active translations:\s+(\d+)`
- `Peak translations:\s+(\d+)`
- `Hits:\s+(\d+)\s+Misses:\s+(\d+)`
- (Optional) per-pool utilization from the `Dynamic mappings` block

**Parsing complexity:** Easy. ~10 lines of parser code.

---

### 6. `show crypto ipsec sa count`

**Why we need it:** active IPsec SA count by direction. DMVPN spokes and FlexVPN clients build SAs dynamically — one config builds anywhere from 5 to 5000 SAs. SA-count is often the platform-gating limit (separate from tunnel count).

**Maps to JSON:** `runtime.crypto.active_sas`, `runtime.crypto.active_sessions`, `runtime.crypto.encrypted_sa_count_in`, `runtime.crypto.encrypted_sa_count_out`

**Sample output:**
```
Total IPsec SAs: 247
   Active IPsec SAs: 247
   Cloned IPsec SAs: 0
```

Or on platforms returning the longer form:
```
Crypto map tag: VPNMAP, local addr 198.51.100.1
   IKE SAs:  124 active, 0 rekeying, 0 dead, 0 negotiating
   IPsec SAs: 247 active, 0 rekeying, 0 unused
```

**Parsing strategy:** keyed-line regex.

**Extraction targets:**
- `Total IPsec SAs:\s+(\d+)`
- `Active IPsec SAs:\s+(\d+)`
- (If long form) IKE active count

**Parsing complexity:** Easy. ~5 lines.

**NetBrain notes:** `show crypto ipsec sa count` is not available on ASR1000 / IOS-XE 16.x. On that train the validated alias is `show crypto ipsec sa` (full detail), from which the parser derives the scalar count. `show crypto session summary` (command #14) is a complementary signal, not a replacement.

**Intent group:** `crypto_ipsec_summary` — aliases `show crypto ipsec sa count` • `show crypto ipsec sa`. See the [alias table](#parser-intent-groups--command-aliases).

---

### 7. `show license summary`

**Why we need it:** confirms which license tier is actively in use. Drives the replacement license SKU sizing — biggest cost driver after the chassis itself. Also catches "we configured features that need a higher tier than what's actually licensed" mismatches.

**Maps to JSON:** `runtime.license.tier`, `runtime.license.entitlements[]`, `runtime.license.smart_account`, `runtime.license.compliance_status`

**Sample output (Smart Licensing, IOS-XE 17.x+):**
```
License Usage:
  License                 Entitlement Tag                           Count Status
  -----------------------------------------------------------------------------
  network-advantage_T1    (NWSTACK_T1)                                  1 IN USE
  dna-advantage_T1        (DNA_NWStack)                                 1 IN USE
```

**Sample output (Classic Licensing):**
```
License Type: Permanent
License Level: advipservices
Next reload license Level: advipservices

License Storage: bootflash:tracelogs/license_data.tlog
```

**Parsing strategy:** keyed-line regex; branch on Smart vs. Classic by whether `License Usage:` header is present.

**Extraction targets:**
- Smart: each license name + tag + status
- Classic: `License Level:\s+(\S+)`

**Parsing complexity:** Medium. Two output dialects to handle.

**NetBrain notes:** `show license summary` is not available on ASR1000 / IOS-XE 16.x. The validated alias on that train is `show license all`, which produces a superset of the Classic-licensing output the parser already handles.

**Intent group:** `license_summary` — aliases `show license summary` • `show license all` • `show license feature`. See the [alias table](#parser-intent-groups--command-aliases).

---

### 8. `show processes cpu sorted`

**Why we need it:** current CPU headroom. If the existing platform sustains 85% CPU, the replacement needs more than nameplate-equivalent capacity. Also identifies whether the load is in a CPU-bound process (BGP scanner, IP input, OSPF) vs. ASIC-offloaded paths.

**Maps to JSON:** `runtime.platform.cpu_5sec_pct`, `runtime.platform.cpu_1min_pct`, `runtime.platform.cpu_5min_pct`, `runtime.platform.top_processes[]`

**Sample output:**
```
CPU utilization for five seconds: 23%/8%; one minute: 19%; five minutes: 18%
PID Runtime(ms)     Invoked      uSecs   5Sec   1Min   5Min TTY Process
192   123498234   234982341         52   3.20%  2.85%  2.74%   0 BGP Scanner
 47    98234123   189234123         51   2.10%  1.95%  1.88%   0 IP Input
...
```

**Parsing strategy:** first line for the headline CPU figures; iterate subsequent table rows for top-N processes.

**Extraction targets:**
- Headline: `five seconds: (\d+)%/(\d+)%; one minute: (\d+)%; five minutes: (\d+)%`
- Top processes: PID, name, 5Min%

**Parsing complexity:** Easy. ~15 lines.

**NetBrain notes:** if the harvest can apply `| include CPU utilization` server-side, the output shrinks to one line. That works for the headline number but loses per-process detail. Recommend full output if practical.

---

## Tier 1 — Full set (add these to v1 if harvest budget allows)

The following six commands round out the v1 sizing picture. They have higher parsing complexity but each unlocks a distinct scoring check.

### 9. `show ipv6 route summary`

**Why:** IPv6 RIB size. Required if any platform candidate has separate IPv4/IPv6 FIB partitioning or IPv6-specific scale ceilings.

**Output / parsing:** identical structure to `show ip route summary`. Reuses the same parser.

**Maps to JSON:** `runtime.route_table.ipv6_total`, `runtime.route_table.ipv6_by_protocol.*`

---

### 10. `show ip route vrf *`  *(or per-VRF if `*` form unsupported on the OS train)*

**Why:** per-VRF route counts. Critical for MPLS-PE and SD-WAN platforms with shared FIB ceilings — total RIB might fit but per-VRF distribution might violate platform partitioning rules.

**Sample output:** the standard `show ip route` output, but repeated once per VRF with a `Routing Table: <vrf-name>` header between sections.

**Parsing strategy:** split on `Routing Table:` headers, then count routes per section. The summary form (`show ip route vrf <name> summary`) is easier to parse and is preferred if NetBrain can fan out per-VRF.

**Extraction targets:** dict of `{vrf_name: route_count}`.

**Parsing complexity:** Hard with `show ip route vrf *` (need to count line items per VRF). Easy if NetBrain runs per-VRF `show ip route vrf <name> summary` — same parser as command #4.

**Maps to JSON:** `runtime.route_table.per_vrf[vrf_name].ipv4_total`, etc.

**NetBrain notes:** strongly prefer the per-VRF summary fan-out approach if NetBrain supports `show vrf` enumeration as a precursor step.

---

### 11. `show ip bgp all summary`

**Why:** per-neighbor `PfxRcd`. Distinguishes "all neighbors are small" from "one neighbor sends 950K prefixes" — different sizing implications.

**Sample output:**
```
For address family: IPv4 Unicast
BGP router identifier 198.51.100.1, local AS number 65001
BGP table version is 487034, main routing table version 487034
487023 network entries using 109897248 bytes of memory
...
Neighbor        V  AS    MsgRcvd  MsgSent  TblVer   InQ OutQ Up/Down  State/PfxRcd
192.0.2.1       4  65000  4823948   348923  487034    0    0  1y23w   487023
192.0.2.2       4  65002    23498     8923  487034    0    0  3w4d         12
198.51.100.10   4  65001     8923     4823  487034    0    0  6d12h         5
```

**Parsing strategy:** find the `Neighbor ... PfxRcd` table header, parse each subsequent row until blank line or next `For address family:` header.

**Extraction targets:** dict of `{neighbor_ip: {asn, prefixes_received, uptime, state}}`.

**Parsing complexity:** Hard. Multi-AFI (IPv4 unicast, IPv4 VPN, IPv6 unicast, etc.) — each AFI is its own section. Column widths vary with terminal width settings on the device.

**Maps to JSON:** `runtime.bgp.per_neighbor[neighbor_ip].prefixes_received`, etc.

**NetBrain notes:** if NetBrain can run with `terminal length 0` and `terminal width 511` set, output is much cleaner.

---

### 12. `show memory statistics`  *(or `show memory summary` on classic IOS)*

**Why:** memory headroom signal. Pairs with CPU as the second sizing input.

**Sample output:**
```
                Head    Total(b)     Used(b)     Free(b)   Lowest(b)  Largest(b)
Processor   7E000000  4194304000  1734892034  2459411966  2398234123  2298234023
        I/O 80000000   536870912    98123498   438747414    420192342   389234123
```

**Parsing strategy:** column-positional. Pick the `Processor` row.

**Extraction targets:** `total_bytes`, `used_bytes`, `free_bytes` for the Processor pool.

**Parsing complexity:** Easy. ~10 lines.

**Maps to JSON:** `runtime.platform.memory_total_bytes`, `runtime.platform.memory_used_bytes`, `runtime.platform.memory_used_pct`

---

### 13. `show interfaces summary`

**Why:** per-interface 5-min in/out bps — actual bandwidth utilization. Drives the 1G vs. 10G vs. 25G uplink decision on the new platform.

**Sample output:**
```
*: interface is up
 IHQ: pkts in input hold queue     IQD: pkts dropped from input queue
 OHQ: pkts in output hold queue    OQD: pkts dropped from output queue
 RXBS: rx rate (bits/sec)          RXPS: rx rate (pkts/sec)
 TXBS: tx rate (bits/sec)          TXPS: tx rate (pkts/sec)
 TRTL: throttle count

  Interface             IHQ   IQD   OHQ   OQD   RXBS   RXPS   TXBS   TXPS  TRTL
-----------------------------------------------------------------------------
* GigabitEthernet0/0/0     0     0     0     0  234000000  31234  189000000  28145    0
* GigabitEthernet0/0/1     0     0     0     0    1230000    156    8923400   1023    0
  GigabitEthernet0/0/2     0     0     0     0          0      0          0      0    0
```

**Parsing strategy:** find the `Interface` header row, parse subsequent rows. Skip the legend.

**Extraction targets:** dict of `{interface_name: {rxbs, txbs, rxps, txps, oqd}}`.

**Parsing complexity:** Medium. Column-positional but with leading `* ` indicator and variable interface-name column width.

**Maps to JSON:** `runtime.interfaces[<name>].rx_bps_5min`, `runtime.interfaces[<name>].tx_bps_5min`, etc.

**NetBrain notes:** alternative is per-interface `show interfaces <name>` parsed for the `5 minute input rate` / `5 minute output rate` lines, which is easier per-command but multiplies the harvest cost by N interfaces.

---

### 14. `show crypto session summary`

**Why:** active IKE/IPsec session count plus per-peer breakdown. Complements command #6 (`show crypto ipsec sa count`) — sessions are a coarser unit than SAs but sometimes the platform-limiting factor.

**Sample output:**
```
Crypto session current status

Code: C - IKE Configuration mode, D - Dead Peer Detection
K - Keepalives, N - NAT-traversal, T - cTCP encapsulation
X - IKE Extended Authentication, F - IKE Fragmentation
R - IKE Auto Reconnect, U - IKE Dynamic Route Update

Total active session: 124
Total active IKE session: 124
Total active IPsec session: 124
```

**Parsing strategy:** keyed-line regex.

**Extraction targets:** session totals.

**Parsing complexity:** Easy. ~5 lines.

**Maps to JSON:** `runtime.crypto.active_sessions`, `runtime.crypto.active_ike_sessions`

---

## Tier 2 — Situational (add when workload calls for it)

Skip these for v1 unless the device class clearly demands them.

| Command | When to harvest | Maps to |
|---------|-----------------|---------|
| `show ip arp summary` | Routed-edge with high subnet density | `runtime.arp.entries_count` |
| `show mac address-table count` | Switching role / L2 distribution | `runtime.l2.mac_entries_count` |
| `show ip mroute count` | Multicast in use (financial / video / IPTV) | `runtime.multicast.sg_state_count` |
| `show power inline` | PoE switch refresh | `runtime.poe.budget_watts`, `runtime.poe.consumed_watts` |
| `show standby brief` | HSRP-paired devices | `runtime.ha.hsrp_active_groups` |
| `show vrrp brief` | VRRP-paired devices | `runtime.ha.vrrp_active_groups` |
| `show platform hardware qfp active feature ipsec data` | IOS-XE crypto throughput sizing | `runtime.crypto.encrypted_bps` |
| `show platform hardware qfp active datapath utilization summary` | IOS-XE QFP load (better than CPU on QFP boxes) | `runtime.platform.qfp_load_pct` |
| `show platform hardware fed switch active fwd-asic resource utilization` | Catalyst 9K TCAM utilization | `runtime.platform.tcam_utilization` |
| `show flow exporter statistics` | NetFlow / telemetry sizing | `runtime.flow.exported_records_per_min` |
| `show policy-map interface <intf>` | Confirm QoS is doing real work (selective per heavy interface) | `runtime.qos.drops_per_class[]` |
| `show sdwan control connections` | cEdge / SD-WAN devices | `runtime.sdwan.control_connections[]` |

---

## Target ingestion JSON shape

The runtime loader will merge harvested data into the existing `analysis_report.json` under a new `runtime` section, keyed by hostname-correlated lookup. Final shape:

```json
{
  "summary": { "...existing analyzer fields..." },
  "interfaces": { "...existing analyzer fields..." },
  "...other existing sections...": { },

  "runtime": {
    "harvest_timestamp": "2026-04-15T14:23:45Z",
    "harvest_source": "netbrain",
    "platform": {
      "software_version": "17.09.04a",
      "image_name": "asr1000-universalk9.17.09.04a.SPA.bin",
      "uptime_seconds": 47520480,
      "cpu_5sec_pct": 23,
      "cpu_1min_pct": 19,
      "cpu_5min_pct": 18,
      "memory_total_bytes": 4194304000,
      "memory_used_bytes": 1734892034,
      "memory_used_pct": 41.4,
      "qfp_load_pct": null,
      "tcam_utilization": null
    },
    "inventory": {
      "chassis_pid": "ASR1001-X",
      "chassis_serial": "FOX1234ABCD",
      "modules": [
        { "slot": "module 0", "pid": "ASR1001-X", "serial": "" }
      ]
    },
    "optics": [
      { "interface": "TenGigE0/0/0", "pid": "SFP-10G-LR", "vendor": "CISCO-FINISAR", "serial": "ABC123", "speed_class": "10G" }
    ],
    "license": {
      "model": "smart",
      "tier": "network-advantage_T1",
      "entitlements": ["network-advantage_T1", "dna-advantage_T1"],
      "compliance_status": "IN USE"
    },
    "route_table": {
      "ipv4_total": 487602,
      "ipv4_by_protocol": { "bgp": 487034, "ospf": 487, "static": 17, "connected": 24 },
      "ipv4_memory_bytes": 140940980,
      "ipv6_total": 0,
      "ipv6_by_protocol": {},
      "per_vrf": {
        "CUSTOMER-A": { "ipv4_total": 12340 },
        "CUSTOMER-B": { "ipv4_total": 234 }
      }
    },
    "bgp": {
      "router_id": "198.51.100.1",
      "local_as": 65001,
      "table_version": 487034,
      "per_neighbor": {
        "192.0.2.1": { "asn": 65000, "prefixes_received": 487023, "uptime": "1y23w", "state": "Established" },
        "192.0.2.2": { "asn": 65002, "prefixes_received": 12, "uptime": "3w4d", "state": "Established" }
      }
    },
    "nat": {
      "active_translations": 12453,
      "peak_translations": 18900,
      "hits": 4823498234,
      "misses": 124993
    },
    "crypto": {
      "active_sas": 247,
      "active_sessions": 124,
      "active_ike_sessions": 124,
      "encrypted_bps": null
    },
    "interfaces": {
      "GigabitEthernet0/0/0": { "rx_bps_5min": 234000000, "tx_bps_5min": 189000000 },
      "GigabitEthernet0/0/1": { "rx_bps_5min": 1230000, "tx_bps_5min": 8923400 }
    },
    "arp": { "entries_count": null },
    "l2": { "mac_entries_count": null },
    "multicast": { "sg_state_count": null },
    "poe": { "budget_watts": null, "consumed_watts": null },
    "ha": { "hsrp_active_groups": null, "vrrp_active_groups": null },
    "flow": { "exported_records_per_min": null }
  }
}
```

Fields that aren't harvested will be `null` — the assessor's `_get(...)` defaults make them harmless.

---

## Parser intent groups / command aliases

Platform and software-train variation means the same intent — e.g., "tell me how many IPsec SAs are active" — is satisfied by different exact command strings on different devices. The loader and parsers match on **intent key**, not raw command string. The loader normalizes the incoming `command` field to an intent key via this map before dispatching to `runtime_parsers.py`.

| Intent key | Accepted command strings (aliases) | Parser output shape |
|------------|-------------------------------------|---------------------|
| `inventory` | `show inventory` | `runtime.inventory.*` |
| `version` | `show version` | `runtime.platform.software_version`, `image_name`, `uptime_seconds`, `license_level` (legacy) |
| `optics` | `show interfaces transceiver detail` • `show interfaces transceiver` • `show idprom interface <name>` | `runtime.optics[]` |
| `route_table_ipv4_summary` | `show ip route summary` | `runtime.route_table.ipv4_*` |
| `route_table_ipv6_summary` | `show ipv6 route summary` | `runtime.route_table.ipv6_*` |
| `route_table_per_vrf` | `show ip route vrf *` • `show ip route vrf <name> summary` (fan-out) | `runtime.route_table.per_vrf[<name>].ipv4_total` |
| `bgp_summary_all` | `show ip bgp all summary` | `runtime.bgp.per_neighbor[<ip>].*` |
| `nat_statistics` | `show ip nat statistics` | `runtime.nat.*` |
| `crypto_ipsec_summary` | `show crypto ipsec sa count` • `show crypto ipsec sa` | `runtime.crypto.active_sas` (derived from full output if `count` form unavailable) |
| `crypto_session_summary` | `show crypto session summary` | `runtime.crypto.active_sessions`, `active_ike_sessions` |
| `license_summary` | `show license summary` • `show license all` • `show license feature` | `runtime.license.*` |
| `cpu_processes` | `show processes cpu sorted` | `runtime.platform.cpu_*`, `runtime.platform.top_processes[]` |
| `memory` | `show memory statistics` • `show memory summary` | `runtime.platform.memory_*` |
| `interfaces_rates` | `show interfaces summary` | `runtime.interfaces.<name>.rx_bps_5min`, `tx_bps_5min` |

Each parser function in `runtime_parsers.py`:
- Is keyed by **intent**, not raw command string (e.g., `parse_crypto_ipsec_summary(raw_text, source_command)`).
- Accepts the raw output of **any of its aliases** and produces the same output dict.
- When a short/summary form (e.g., `sa count`) is unavailable and only the long/detail form (e.g., `sa`) is present, the parser is responsible for **deriving** the summary scalar from the detailed output.

**Alias validation status (2026-04-16):**

| Intent | Train | Validated alias | Note |
|--------|-------|----------------|------|
| `crypto_ipsec_summary` | IOS-XE 17.x+ | `show crypto ipsec sa count` | Primary |
| `crypto_ipsec_summary` | IOS-XE 16.x (ASR1000) | `show crypto ipsec sa` | Derived count |
| `license_summary` | IOS-XE 17.x+ (Smart) | `show license summary` | Primary |
| `license_summary` | IOS-XE 16.x (ASR1000) | `show license all` | Classic-style output |
| `optics` | IOS-XE 17.x+ | `show interfaces transceiver detail` | Primary |
| `optics` | IOS-XE 16.x (ASR1000) | *(unresolved)* | Deferred in `NETFIT_RUNTIME_MINIMAL_ASR1000_16X` |

When a new train/platform is validated, add a row here and update the variant template under [NetBrain template strategy](#netbrain-template-strategy). No new parser code is required if the intent already has a parser — only an alias-map entry.

---

## Implementation hooks (for reference)

When the runtime loader is built:

1. **New file:** `runtime_loader.py` — exposes `load_runtime_for_device(export_path, hostname) -> dict | None`. Accepts either a NetBrain native text export (split on `#---` delimiters) or a CSV. Returns `None` if no records for the hostname (signal to the caller that this device has no runtime augmentation). Also exposes the alias map that normalizes a raw `command` string to an **intent key** before parser dispatch.

2. **New parser submodule:** `runtime_parsers.py` — one function per **intent key** (not per raw command string): `parse_inventory`, `parse_route_table_ipv4_summary`, `parse_crypto_ipsec_summary`, etc. Each takes a raw text blob plus the source command string (so it can branch when aliases produce different output shapes) and returns a structured dict. These are pure functions — easy to unit-test against captured sample output.

3. **Analyzer enrichment:** at the end of `analyze_config()`, look up runtime data by hostname and merge it under `analysis["runtime"]` if present.

4. **Assessor extension:** add new finding sites for runtime-aware checks. Each must use `_get(analysis, ["runtime", "..."], None)` and gracefully skip the check if the field is absent (config-only mode must keep working).

5. **Platform YAML extensions:** add the runtime-relevant ceilings — `max_routes_ipv4`, `max_routes_ipv6`, `max_nat_translations`, `max_ipsec_sas`, `max_ipsec_sessions`, `crypto_throughput_gbps`, `forwarding_throughput_gbps`. Optional `optics_compatibility: [PID, ...]` and `license_tiers_supported: [tier, ...]`.

6. **CLI extension:** add `--runtime-csv FILE` (single-device) or `--runtime-dir DIR` (batch) to `main.py`. Loader is invoked before `analyze_config()`.

7. **Tests:** add `tests/test_runtime_parsers.py` — one test per show command parser, with the sample output blocks from this document as fixtures. Add an E2E test that confirms a runtime-augmented analysis produces different scoring than a config-only analysis (proves the runtime fields actually flow through).

---

## What to verify when NetBrain harvest is configured

Before wiring this into the tool, validate against a sample export from a real device:

**Format-agnostic checks:**

1. **Hostname matches the config's `hostname` directive** exactly (case-insensitive). If NetBrain uses an inventory-management name that differs from the configured hostname, plan a mapping table.
2. **Each (device, command) pair appears once** — duplicate records (e.g., from re-runs) need to be deduplicated, ideally by keeping the most recent `timestamp`.
3. **No truncation** — `show ip route summary` and `show ip bgp all summary` outputs can exceed several KB. Text exports rarely truncate; some CSV exporters cap cells at 32K characters.
4. **The command strings in the export are covered by the alias map** in [Parser intent groups / command aliases](#parser-intent-groups--command-aliases). If a device ran `show license all` and no entry maps that string to the `license_summary` intent, the loader will drop the record silently.
5. **Command-not-supported blocks are detected, not parsed** — `% Invalid input detected at '^' marker.` lines must be recognized and skipped with a warning, not fed to a parser.
6. **Sample one command's output and walk it through this document's parsing strategy by hand** — confirms the raw format matches what the parser expects.

**Native text export (primary) — additional checks:**

7. **`#--- <device> <command> Execute at <timestamp>` delimiter present before every command block** and matches the documented regex.
8. **Echoed command prompts** (e.g., `deviceXXXXXXXXX>show inventory`) on the first line of each output block are stripped by the loader before parsing.

**CSV export (alternative) — additional checks:**

9. **Column names match** (or are remappable to) `device_name`, `command`, `result`.
10. **Multi-line `result` values are properly quoted** — open the CSV in a text editor and confirm the show output is wrapped in `"..."` with internal `"` escaped as `""`. If NetBrain replaces newlines with literal `\n`, undo that before ingestion.

If any of those don't hold, capture an actual sample export and we'll adjust the loader's expectations to match.
