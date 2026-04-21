# Crypto-Map Global WAN Edge Fixture — Spec

> **Target:** `tests/fixtures/lab/crypto_map_global_wan_edge/`
> **Status:** Draft spec for lab build. Transcribe per §5, capture per §7, hand-insert §5.6 blocks, commit the result.
> **Session:** 003
> **Companion spec:** `ipsec_pattern_a_spec.md` (greenfield reference; this spec reflects production reality)

---

## 1. Purpose

Lab-generated fixture representing a legacy-style partner-aggregation WAN edge: IOS-XE 16.x/17.x on ASR1000-family hardware (Cat8000V in lab), global-table routing for all partners, policy-based IPsec via crypto maps + ISAKMP, Port-channel upstream to core with inside/outside VLAN split across a single bundle, NAT in the data path, peer-group-scoped BGP policy, and representative transport variety (IPsec-over-Internet, MPLS L3VPN, direct Ethernet, T3 Serial).

This fixture complements the Pattern A / IKEv2 spec. That one represents modern greenfield design; this one represents the production shape that actually appears in the field today. Both should exist in the corpus because netfit's parser must handle both.

This fixture exercises:

- Legacy crypto: ISAKMP (IKEv1) policies + `crypto map` with per-entry ACLs, applied to an outside subinterface
- Global-only routing — no service VRFs (mgmt VRF only)
- `Port-channel1` with dot1Q subinterfaces using `encapsulation dot1Q <vlan> primary <ifname> secondary <ifname>` LAG member preference
- Inside/outside separation via VLAN (not physical) — VLAN 2 inside + VLAN 4 outside on the same Port-channel
- NAT data path (`ip nat inside` on .100, `ip nat outside` on .200)
- BGP with `bgp asnotation dot`, peer-group-per-partner, `no bgp default ipv4-unicast`, IPv4 + IPv6 neighbor pairs
- OSPF scoped to loopback + inside subif reachability only
- Flexible NetFlow references (`ip flow monitor`)
- Transport breadth: GigE (Ethernet subif + direct), Serial (T3)
- Route-map policy naming patterns matching `IN/OUT` style observed in production
- Representative QoS, TACACS, SNMP, NTP, AAA, banner, logging, services

---

## 2. Topology

```
                                           ┌──────────────┐
                                           │ partner-ipsec│
                                           │  AS 65010    │
                                           └──────┬───────┘
                                                  │ Gi1 (VLAN 4, outside)
                                                  │
                                                  ▼
                                          ┌──────────────┐
                                          │  sw1 (L2)    │
                                          │  unmanaged   │
                                          └──┬────────┬──┘
                                             │ VLAN 2 │ VLAN 4
                                             │ inside │ outside
                                             │        │
                                             ▼        ▼
                            ┌─────────────────────────────────────┐
                            │             wan-edge                │
                            │              AS 65001               │
                            │                                     │
                            │  Gi2+Gi3 = Port-channel1 (trunk)    │
                            │    .100 (VLAN 2, inside, NAT in)    │
                            │    .200 (VLAN 4, outside, NAT out,  │
                            │           crypto map applied)       │
                            │                                     │
                            │  Gi4  = SP handoff (MPLS L3VPN)     │
                            │    .100 (dot1Q 100) → partner-mpls  │
                            │  Gi5  = direct Ethernet             │
                            │    → partner-eth                    │
                            │  Se0/2/0 = T3 (config-only, §5.6)   │
                            │    → partner-t3 (NOT LIVE IN LAB)   │
                            └──────┬──────────────────┬───────────┘
                                   │ Gi4              │ Gi5
                                   │                  │
                                   ▼                  ▼
                           ┌──────────────┐   ┌──────────────┐
                           │partner-mpls-sp│  │ partner-eth  │
                           │  AS 65030 (SP)│  │  AS 65040    │
                           │  carries      │  │              │
                           │  AS 65020     │  │              │
                           └───────────────┘  └──────────────┘

         (core-rr attached to sw1 on VLAN 2 — picks up the inside trunk)
                                             │
                                             ▼
                                     ┌──────────────┐
                                     │   core-rr    │
                                     │  AS 65001    │
                                     │  (collapsed  │
                                     │   core + RR) │
                                     └──────────────┘
```

**Node count:** 5 × Cat8000V. `sw1` is an unmanaged switch (free, doesn't count). Fits CML-Free.

**Two architectural simplifications in lab:**

1. **core + RR collapsed into one node** (`core-rr`). In production these are separate; the wan-edge's iBGP config doesn't change either way. If you upgrade to CML-Personal later, split them.
2. **Firewall pair omitted.** Production vWire firewall bridges VLAN 2 and VLAN 4 between wan-edge and its respective upstreams. In lab, `sw1` does the bridging directly. Captured wan-edge config is identical either way.

**Port-channel1 member wiring:** The fixture config declares Port-channel1 as a LAG of Gi2 + Gi3. In CML-Free, **wire only Gi2 to sw1**; leave Gi3 disconnected. LAG member preference appears in the config text (`primary Gi2 secondary Gi3`) regardless of live-wiring. This is deliberate — the parser needs to see the LAG syntax; live LAG behavior through an unmanaged switch is unreliable.

---

## 3. Addressing & Identifier Plan

### ASNs (RFC 5398 documentation range used throughout)

| Role | ASN | Notes |
|------|-----|-------|
| Internal | 65001 | This org |
| Partner via IPsec | 65010 | Direct partner |
| Partner behind SP (MPLS L3VPN) | 65020 | Partner's own ASN |
| SP (MPLS transit) | 65030 | The MPLS provider |
| Partner direct Ethernet | 65040 | Direct partner |
| Partner T3 (config-only) | 65050 | Direct partner |

**`bgp asnotation dot`** enabled globally — matches production. In config these render as-is (e.g., `65001`); in some displays they'd render as dotted (`0.65001`) but the plain form is still accepted.

### Loopbacks

| Node | Loopback | Notes |
|------|----------|-------|
| wan-edge | Loopback0: 10.255.0.1/32 | router-id + iBGP peer |
| wan-edge | Loopback100: 10.255.100.1/32 | NAT source pool anchor (see §5.2) |
| wan-edge | Loopback900: 10.255.200.1/32 | IP SLA source |
| core-rr | Loopback0: 10.255.1.1/32 | router-id + RR iBGP peer |
| partner-ipsec | Loopback0: 172.16.10.1/32 | partner's "inside" network router |
| partner-mpls-sp | Loopback0: 10.200.0.1/32 | SP's BGP router-id |
| partner-eth | Loopback0: 172.16.30.1/32 | partner's router-id |

Three loopbacks on wan-edge to match production (which had 3).

### VLANs

| VLAN | Purpose | Where |
|------|---------|-------|
| 2 | Inside trunk (wan-edge ↔ core-rr via sw1) | Port-channel1.100 on wan-edge, Gi1.2 on core-rr |
| 4 | Outside trunk (wan-edge ↔ Internet via sw1) | Port-channel1.200 on wan-edge, Gi1.4 on partner-ipsec |
| 100 | Per-partner dot1Q on SP handoff | wan-edge Gi4.100, SP Gi1.100 |

### Subnets

| Subnet | Purpose | Assignment |
|--------|---------|------------|
| 10.1.2.0/30 | VLAN 2 inside | wan-edge = .1, core-rr = .2 |
| 203.0.113.0/30 | VLAN 4 outside (sim Internet) | wan-edge = .1, partner-ipsec public = .2 |
| 10.200.1.0/30 | Gi4.100 to SP PE | wan-edge = .1, SP = .2 |
| 192.168.30.0/30 | Gi5 to partner-eth | wan-edge = .1, partner-eth = .2 |
| 192.168.40.0/30 | Se0/2/0 to partner-t3 (config-only) | wan-edge = .1, partner-t3 = .2 |
| 10.10.0.0/16 | Our-inside (crypto ACL src, NAT pool source) | — |
| 172.16.10.0/24 | Partner-ipsec inside (crypto ACL dst) | Partner advertises |
| 172.16.20.0/24 | Partner-mpls inside | Partner advertises via SP |
| 172.16.30.0/24 | Partner-eth inside | Partner advertises |
| 172.16.40.0/24 | Partner-t3 inside (config-only) | Partner advertises |
| 203.0.113.100/32 | NAT pool address (egress masquerade) | Single-address pool |

### IPv6 (minimal, for sanitizer coverage)

- Port-channel1.100 inside subif: `2001:db8:1:2::1/64` (wan-edge), `::2/64` (core-rr)
- Loopback0 IPv6: wan-edge `2001:db8:ffff::1/128`, core-rr `2001:db8:ffff::101/128`
- One IPv6 iBGP neighbor pair (wan-edge ↔ core-rr loopback IPv6)
- Partner IPv6 peerings deferred (keeps fixture readable)

### Peer-group naming (mirrors production `PG_NNN` style)

| Peer-group | Partner | Transport | Crypto |
|-----------|---------|-----------|--------|
| PG_010 | partner-ipsec | Internet | ISAKMP + crypto map entry 10 |
| PG_020 | partner-mpls (via SP) | MPLS L3VPN | none |
| PG_030 | partner-eth | Direct Ethernet | none |
| PG_040 | partner-t3 | T3 Serial | none |

### Route-map naming

| Route-map | Direction | Applied to |
|-----------|-----------|------------|
| PG_010-IN / PG_010-OUT | in / out | PG_010 |
| PG_020-IN / PG_020-OUT | in / out | PG_020 |
| PG_030-IN / PG_030-OUT | in / out | PG_030 |
| PG_040-IN / PG_040-OUT | in / out | PG_040 |

Production used transport-specific suffixes like `ATT-TO-PG_NNN`. In fixture we use the simpler `PG_NNN-IN/OUT` form to avoid hardcoding any SP name.

### Pre-shared keys

All ISAKMP PSKs use the `lab-psk-*` prefix (e.g., `lab-psk-partner-ipsec-010`).

### Domain

`lab.netfit.local`

---

## 4. Assumptions Called Out

These are drafting decisions. Verify against the fresh sanitizer output and production config tomorrow, flag any that don't match:

1. **No service VRFs.** Management VRF (on Gi0, shutdown) is included to match production; all partner routing is in global.
2. **Port-channel LAG members.** `primary Gi2 secondary Gi3` — the dot1Q-level LAG member preference seen in production. Verify the exact syntax form matches.
3. **OSPF scope is loopback-reachability-only.** Single area 0, passive-default, non-passive only on Port-channel1.100. No OSPF on any outside-facing interface or on the SP/Ethernet/T3 handoffs — those exchange routes via eBGP only.
4. **BGP is global-AF only.** No `address-family ipv4 vrf ...` blocks. `no bgp default ipv4-unicast` at process level (matches production).
5. **Peer-group per partner** — even when a peer-group has one member, config uses the peer-group pattern. Matches production exactly.
6. **iBGP up to core-rr only** (no separate RR in lab). WAN edge's BGP config can still reference two RR loopbacks if you want — but in 5-node lab we only instantiate one. Draft below uses one RR; tell me if you want the two-RR-referenced form.
7. **NAT is a NAT overload (PAT) on a pool of one address** (203.0.113.100). Real production might have pools of many addresses or twice-NAT or VRF-aware NAT — this is the simplest form that still exercises the parser blocks.
8. **ISAKMP crypto map is `ipsec-isakmp` (not manual or dynamic)**. One static peer per map entry. Match pattern: one partner per entry. Production has many entries (one crypto map, many entries numbered 10, 20, 30, ...); fixture has one entry (entry 10) to keep it readable.
9. **Partner-mpls modeled as SP-transit eBGP** — wan-edge peers with the SP's PE IP, and the SP advertises partner prefixes with AS-path `65030 65020 i`. This is one of several valid MPLS L3VPN handoff styles.
10. **IPv6 included minimally** — just on the inside subif + one iBGP pair. Not on partner peerings. Enough to exercise v6 sanitizer/parser blocks without doubling the fixture size.
11. **Services kept representative, not exhaustive.** One NTP server, one TACACS, one SNMP community, one logging host. Production had many of each — the fixture isn't meant to inflate those lists.
12. **Flexible NetFlow.** Only the `ip flow monitor MINISNIFF input/output` references on Port-channel1.100 are included. The full FNF record/exporter/monitor config is declared minimally in the spec but may need extension if your production FNF exports are more complex.

---

## 5. Device Configs

### 5.1 partner-ipsec (scaffolding)

Minimal — just enough to be an IPsec peer and BGP neighbor.

```
hostname partner-ipsec
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 172.16.10.1 255.255.255.255
!
interface GigabitEthernet1
 description trunk to sw1 (VLAN 4)
 no ip address
 no shutdown
!
interface GigabitEthernet1.4
 encapsulation dot1Q 4
 ip address 203.0.113.2 255.255.255.252
!
! --- ISAKMP (IKEv1) + crypto map ---
crypto isakmp policy 10
 encr aes 256
 hash sha256
 authentication pre-share
 group 14
 lifetime 28800
!
crypto isakmp key lab-psk-partner-ipsec-010 address 203.0.113.1
!
crypto ipsec transform-set TS-AES256-SHA256 esp-aes 256 esp-sha256-hmac
 mode tunnel
!
ip access-list extended ACL-CRYPTO-010
 permit ip 172.16.10.0 0.0.0.255 10.10.0.0 0.0.255.255
!
crypto map CMAP-WAN 10 ipsec-isakmp
 set peer 203.0.113.1
 set transform-set TS-AES256-SHA256
 set pfs group14
 match address ACL-CRYPTO-010
!
interface GigabitEthernet1.4
 crypto map CMAP-WAN
!
! --- BGP ---
router bgp 65010
 bgp router-id 172.16.10.1
 bgp log-neighbor-changes
 neighbor 203.0.113.1 remote-as 65001
 !
 address-family ipv4 unicast
  network 172.16.10.0 mask 255.255.255.0
  neighbor 203.0.113.1 activate
 exit-address-family
!
ip route 172.16.10.0 255.255.255.0 Null0
!
end
```

---

### 5.2 wan-edge (the subject)

This is the fixture's primary content. The crypto-map, NAT, BGP peer-group, and route-map blocks are where netfit's parser will get the most coverage.

```
hostname wan-edge
!
boot system flash bootflash:asr1000rpx86-universalk9.16.03.07.SPA.bin
boot system flash harddisk:asr1000rpx86-universalk9.17.03.05.SPA.bin
boot system flash bootflash:
!
ip domain name lab.netfit.local
!
card type t3 0 2
!
logging buffered 150000
logging host 10.10.5.50
!
enable secret 5 $1$lab$XXXXXXXXXXXXXXXXXXXXXX
!
aaa new-model
aaa group server tacacs+ TACGROUP
 server-private 10.10.5.10 key lab-tacacs-key-placeholder
 ip vrf forwarding MGMT
 ip tacacs source-interface GigabitEthernet0
aaa authentication login default group TACGROUP local
aaa authorization exec default group TACGROUP local
aaa accounting exec default start-stop group TACGROUP
!
service timestamps debug datetime msec show-timezone
service timestamps log datetime msec show-timezone
service password-encryption
!
vrf definition MGMT
 description Management VRF (Gi0 only)
 !
 address-family ipv4
 exit-address-family
 !
 address-family ipv6
 exit-address-family
!
! --- Loopbacks ---
interface Loopback0
 description router-id + iBGP peer address
 ip address 10.255.0.1 255.255.255.255
 ipv6 address 2001:DB8:FFFF::1/128
 ipv6 enable
!
interface Loopback100
 description NAT pool anchor
 ip address 10.255.100.1 255.255.255.255
!
interface Loopback900
 description IP SLA source
 ip address 10.255.200.1 255.255.255.255
!
! --- Management VRF interface ---
interface GigabitEthernet0
 vrf forwarding MGMT
 no ip address
 shutdown
 negotiation auto
!
! --- Flexible NetFlow (minimal, for parser coverage) ---
flow record FR-V4
 match ipv4 source address
 match ipv4 destination address
 match transport source-port
 match transport destination-port
 collect counter bytes
 collect counter packets
!
flow exporter FE-COLLECTOR
 destination 10.10.5.100
 source Loopback0
 transport udp 9996
 template data timeout 600
!
flow monitor MINISNIFF
 exporter FE-COLLECTOR
 cache timeout active 60
 record FR-V4
!
! --- Port-channel to core/firewall ---
interface Port-channel1
 description trunk to firewall pair (inside VLAN 2 + outside VLAN 4)
 no ip address
 load-interval 30
!
interface Port-channel1.100
 description INSIDE (trusted, post-firewall)
 encapsulation dot1Q 2 primary GigabitEthernet2 secondary GigabitEthernet3
 ip flow monitor MINISNIFF input
 ip flow monitor MINISNIFF output
 ip address 10.1.2.1 255.255.255.252
 ip nat inside
 ip virtual-reassembly max-reassemblies 16
 ipv6 address 2001:DB8:1:2::1/64
 ipv6 enable
 logging event subif-link-status
!
interface Port-channel1.200
 description OUTSIDE (untrusted, pre-firewall; IPsec terminations)
 encapsulation dot1Q 4 primary GigabitEthernet2 secondary GigabitEthernet3
 ip address 203.0.113.1 255.255.255.252
 ip nat outside
 ip virtual-reassembly max-reassemblies 16
 logging event subif-link-status
 crypto map CMAP-WAN
!
! --- Physical members of Port-channel1 ---
interface GigabitEthernet2
 description Po1 member (primary)
 no ip address
 channel-group 1 mode active
 no shutdown
!
interface GigabitEthernet3
 description Po1 member (secondary)
 no ip address
 channel-group 1 mode active
 no shutdown
!
! --- SP handoff (partner-mpls via L3VPN) ---
interface GigabitEthernet4
 description to SP PE (MPLS L3VPN handoff trunk)
 no ip address
 no shutdown
!
interface GigabitEthernet4.100
 description PG_020 (partner-mpls via SP AS 65030)
 encapsulation dot1Q 100
 ip address 10.200.1.1 255.255.255.252
!
! --- Direct Ethernet partner ---
interface GigabitEthernet5
 description PG_030 (partner-eth direct)
 ip address 192.168.30.1 255.255.255.252
 no shutdown
!
! --- T3 Serial partner (CONFIG-ONLY — see §5.6, hand-insert into capture) ---
! (blocks for Serial0/2/0 inserted post-capture per §7)
!
! --- ISAKMP / crypto (IKEv1) ---
crypto isakmp policy 10
 encr aes 256
 hash sha256
 authentication pre-share
 group 14
 lifetime 28800
crypto isakmp policy 20
 encr aes 256
 hash sha1
 authentication pre-share
 group 14
 lifetime 28800
crypto isakmp policy 30
 encr 3des
 hash sha1
 authentication pre-share
 group 2
 lifetime 86400
crypto isakmp policy 40
 encr aes
 hash sha1
 authentication pre-share
 group 2
 lifetime 86400
!
crypto isakmp key lab-psk-partner-ipsec-010 address 203.0.113.2
!
crypto ipsec security-association lifetime kilobytes disable
crypto ipsec security-association lifetime seconds 3600
!
crypto ipsec transform-set TS-AES256-SHA256 esp-aes 256 esp-sha256-hmac
 mode tunnel
crypto ipsec transform-set TS-AES256-SHA1 esp-aes 256 esp-sha-hmac
 mode tunnel
crypto ipsec transform-set TS-3DES-SHA1 esp-3des esp-sha-hmac
 mode tunnel
!
! --- Crypto ACLs (one per crypto map entry) ---
ip access-list extended ACL-CRYPTO-010
 remark PG_010 partner-ipsec: our-inside <-> partner-ipsec inside
 permit ip 10.10.0.0 0.0.255.255 172.16.10.0 0.0.0.255
!
! --- Crypto map ---
crypto map CMAP-WAN 10 ipsec-isakmp
 description PG_010 partner-ipsec
 set peer 203.0.113.2
 set transform-set TS-AES256-SHA256
 set pfs group14
 set security-association lifetime seconds 3600
 match address ACL-CRYPTO-010
!
! --- NAT ---
ip access-list standard ACL-NAT-SOURCE
 remark our-inside subnets allowed to NAT outbound
 permit 10.10.0.0 0.0.255.255
!
ip nat pool NATPOOL-OUTSIDE 203.0.113.100 203.0.113.100 netmask 255.255.255.0
ip nat inside source list ACL-NAT-SOURCE pool NATPOOL-OUTSIDE overload
!
! --- Prefix lists ---
ip prefix-list PL-OUR-AGGREGATES seq 10 permit 10.10.0.0/16 le 24
ip prefix-list PL-PARTNER-IPSEC-IN seq 10 permit 172.16.10.0/24
ip prefix-list PL-PARTNER-MPLS-IN seq 10 permit 172.16.20.0/24
ip prefix-list PL-PARTNER-ETH-IN seq 10 permit 172.16.30.0/24
ip prefix-list PL-PARTNER-T3-IN seq 10 permit 172.16.40.0/24
!
ip community-list standard CL-PARTNER-IPSEC permit 65001:10
ip community-list standard CL-PARTNER-MPLS permit 65001:20
ip community-list standard CL-PARTNER-ETH permit 65001:30
ip community-list standard CL-PARTNER-T3 permit 65001:40
!
! --- Route-maps ---
route-map PG_010-IN permit 10
 match ip address prefix-list PL-PARTNER-IPSEC-IN
 set community 65001:10 additive
 set local-preference 200
!
route-map PG_010-OUT permit 10
 match ip address prefix-list PL-OUR-AGGREGATES
 set community 65001:10 additive
!
route-map PG_020-IN permit 10
 match ip address prefix-list PL-PARTNER-MPLS-IN
 set community 65001:20 additive
 set local-preference 200
!
route-map PG_020-OUT permit 10
 match ip address prefix-list PL-OUR-AGGREGATES
 set community 65001:20 additive
!
route-map PG_030-IN permit 10
 match ip address prefix-list PL-PARTNER-ETH-IN
 set community 65001:30 additive
 set local-preference 200
!
route-map PG_030-OUT permit 10
 match ip address prefix-list PL-OUR-AGGREGATES
 set community 65001:30 additive
!
route-map PG_040-IN permit 10
 match ip address prefix-list PL-PARTNER-T3-IN
 set community 65001:40 additive
 set local-preference 200
!
route-map PG_040-OUT permit 10
 match ip address prefix-list PL-OUR-AGGREGATES
 set community 65001:40 additive
!
! --- OSPF (underlay, global) ---
router ospf 1
 router-id 10.255.0.1
 passive-interface default
 no passive-interface Port-channel1.100
 network 10.255.0.1 0.0.0.0 area 0
 network 10.1.2.0 0.0.0.3 area 0
!
! --- BGP ---
router bgp 65001
 bgp router-id 10.255.0.1
 bgp asnotation dot
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
 ! --- Peer-group declarations ---
 neighbor PG_CORE peer-group
 neighbor PG_CORE remote-as 65001
 neighbor PG_CORE description iBGP to core-rr
 neighbor PG_CORE update-source Loopback0
 !
 neighbor PG_010 peer-group
 neighbor PG_010 remote-as 65010
 neighbor PG_010 description partner-ipsec (IPsec over Internet)
 !
 neighbor PG_020 peer-group
 neighbor PG_020 remote-as 65030
 neighbor PG_020 description partner-mpls via SP (AS 65030 carrying AS 65020)
 !
 neighbor PG_030 peer-group
 neighbor PG_030 remote-as 65040
 neighbor PG_030 description partner-eth (direct Ethernet)
 !
 neighbor PG_040 peer-group
 neighbor PG_040 remote-as 65050
 neighbor PG_040 description partner-t3 (T3 Serial, config-only)
 !
 ! --- Peer-group members ---
 neighbor 10.255.1.1 peer-group PG_CORE
 neighbor 203.0.113.2 peer-group PG_010
 neighbor 10.200.1.2 peer-group PG_020
 neighbor 192.168.30.2 peer-group PG_030
 neighbor 192.168.40.2 peer-group PG_040
 !
 ! --- IPv4 unicast AF ---
 address-family ipv4
  network 10.10.0.0 mask 255.255.0.0
  redistribute connected route-map RM-CONNECTED-TO-BGP
  !
  neighbor PG_CORE activate
  neighbor PG_CORE send-community both
  neighbor PG_CORE next-hop-self
  !
  neighbor PG_010 activate
  neighbor PG_010 send-community both
  neighbor PG_010 route-map PG_010-IN in
  neighbor PG_010 route-map PG_010-OUT out
  neighbor PG_010 soft-reconfiguration inbound
  !
  neighbor PG_020 activate
  neighbor PG_020 send-community both
  neighbor PG_020 route-map PG_020-IN in
  neighbor PG_020 route-map PG_020-OUT out
  !
  neighbor PG_030 activate
  neighbor PG_030 send-community both
  neighbor PG_030 route-map PG_030-IN in
  neighbor PG_030 route-map PG_030-OUT out
  !
  neighbor PG_040 activate
  neighbor PG_040 send-community both
  neighbor PG_040 route-map PG_040-IN in
  neighbor PG_040 route-map PG_040-OUT out
  !
  neighbor 10.255.1.1 peer-group PG_CORE
  neighbor 203.0.113.2 peer-group PG_010
  neighbor 10.200.1.2 peer-group PG_020
  neighbor 192.168.30.2 peer-group PG_030
  neighbor 192.168.40.2 peer-group PG_040
 exit-address-family
 !
 ! --- IPv6 unicast AF (minimal) ---
 address-family ipv6
  neighbor 2001:DB8:FFFF::101 remote-as 65001
  neighbor 2001:DB8:FFFF::101 description iBGP v6 to core-rr
  neighbor 2001:DB8:FFFF::101 update-source Loopback0
  neighbor 2001:DB8:FFFF::101 activate
  neighbor 2001:DB8:FFFF::101 send-community both
  neighbor 2001:DB8:FFFF::101 next-hop-self
 exit-address-family
!
route-map RM-CONNECTED-TO-BGP permit 10
 match ip address prefix-list PL-OUR-AGGREGATES
!
! --- IP SLA + track ---
ip sla 1
 icmp-echo 10.200.1.2 source-interface Loopback900
 threshold 500
 frequency 10
ip sla schedule 1 life forever start-time now
!
track 1 ip sla 1 reachability
!
! --- Default routes (illustrative) ---
ip route 0.0.0.0 0.0.0.0 203.0.113.2 track 1
ip route 172.16.10.0 255.255.255.0 Null0 250
ip route 172.16.20.0 255.255.255.0 Null0 250
ip route 172.16.30.0 255.255.255.0 Null0 250
ip route 172.16.40.0 255.255.255.0 Null0 250
!
! --- QoS (minimal) ---
class-map match-any CM-VOICE
 match dscp ef
class-map match-any CM-CRITICAL
 match dscp af31 af32 af33
!
policy-map PM-WAN-OUT
 class CM-VOICE
  priority percent 20
 class CM-CRITICAL
  bandwidth remaining percent 40
 class class-default
  bandwidth remaining percent 60
!
interface Port-channel1.200
 service-policy output PM-WAN-OUT
!
! --- SNMP / NTP / TACACS ---
snmp-server community lab-snmp-ro RO
snmp-server location Lab-CML-Rack-1
snmp-server contact netops@lab.netfit.local
!
ntp server 10.10.5.200 prefer
!
tacacs-server timeout 10
!
! --- Services / lines / banner ---
line con 0
 exec-timeout 10 0
 logging synchronous
line vty 0 15
 exec-timeout 10 0
 transport input ssh
 logging synchronous
!
banner login ^C
  *** Lab-only device. Authorized use only. ***
  *** All activity is logged.              ***
^C
!
end
```

---

### 5.3 core-rr (collapsed core + RR — scaffolding)

```
hostname core-rr
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 10.255.1.1 255.255.255.255
 ipv6 address 2001:DB8:FFFF::101/128
 ipv6 enable
!
interface GigabitEthernet1
 description trunk to sw1 (VLAN 2 inside only)
 no ip address
 no shutdown
!
interface GigabitEthernet1.2
 encapsulation dot1Q 2
 ip address 10.1.2.2 255.255.255.252
 ipv6 address 2001:DB8:1:2::2/64
 ipv6 enable
!
router ospf 1
 router-id 10.255.1.1
 passive-interface default
 no passive-interface GigabitEthernet1.2
 network 10.255.1.1 0.0.0.0 area 0
 network 10.1.2.0 0.0.0.3 area 0
!
router bgp 65001
 bgp router-id 10.255.1.1
 bgp asnotation dot
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
 neighbor 10.255.0.1 remote-as 65001
 neighbor 10.255.0.1 description wan-edge (RR client)
 neighbor 10.255.0.1 update-source Loopback0
 !
 neighbor 2001:DB8:FFFF::1 remote-as 65001
 neighbor 2001:DB8:FFFF::1 description wan-edge v6
 neighbor 2001:DB8:FFFF::1 update-source Loopback0
 !
 address-family ipv4
  neighbor 10.255.0.1 activate
  neighbor 10.255.0.1 route-reflector-client
  neighbor 10.255.0.1 send-community both
  neighbor 10.255.0.1 next-hop-self
 exit-address-family
 !
 address-family ipv6
  neighbor 2001:DB8:FFFF::1 activate
  neighbor 2001:DB8:FFFF::1 route-reflector-client
  neighbor 2001:DB8:FFFF::1 send-community both
  neighbor 2001:DB8:FFFF::1 next-hop-self
 exit-address-family
!
end
```

---

### 5.4 partner-mpls-sp (scaffolding)

Plays two roles in the fixture: it's the SP's PE router, but for simplicity it also injects the partner-behind-SP's prefixes directly. This collapses partner + SP into one node while still producing the right AS-path shape.

```
hostname partner-mpls-sp
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 10.200.0.1 255.255.255.255
!
interface GigabitEthernet1
 description to wan-edge Gi4.100 (MPLS L3VPN handoff sim)
 no ip address
 no shutdown
!
interface GigabitEthernet1.100
 encapsulation dot1Q 100
 ip address 10.200.1.2 255.255.255.252
!
router bgp 65030
 bgp router-id 10.200.0.1
 bgp log-neighbor-changes
 no bgp default ipv4-unicast
 !
 neighbor 10.200.1.1 remote-as 65001
 neighbor 10.200.1.1 description wan-edge via SP handoff
 !
 address-family ipv4
  ! Advertise partner's prefix with AS-path prepend (simulating SP-behind-partner)
  network 172.16.20.0 mask 255.255.255.0
  neighbor 10.200.1.1 activate
  neighbor 10.200.1.1 route-map RM-PREPEND-PARTNER out
 exit-address-family
!
route-map RM-PREPEND-PARTNER permit 10
 ! Simulates the partner AS behind SP AS
 set as-path prepend 65020
!
ip route 172.16.20.0 255.255.255.0 Null0
!
end
```

---

### 5.5 partner-eth (scaffolding)

```
hostname partner-eth
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 172.16.30.1 255.255.255.255
!
interface GigabitEthernet1
 description to wan-edge Gi5 (direct Ethernet)
 ip address 192.168.30.2 255.255.255.252
 no shutdown
!
router bgp 65040
 bgp router-id 172.16.30.1
 bgp log-neighbor-changes
 neighbor 192.168.30.1 remote-as 65001
 !
 address-family ipv4 unicast
  network 172.16.30.0 mask 255.255.255.0
  neighbor 192.168.30.1 activate
 exit-address-family
!
ip route 172.16.30.0 255.255.255.0 Null0
!
end
```

---

### 5.6 partner-t3 config-only insertion blocks

**These blocks are NOT produced by the CML lab.** They are hand-inserted into the captured `wan-edge.txt` per §7. Cat8000V has no T3 hardware; live capture will omit them.

**Block A — insert into `wan-edge.txt` under the other interface blocks (alphabetical order places it after Serial or before Loopback depending on IOS-XE sort — produce by matching where `show run` would place a Serial interface block if the hardware existed):**

```
interface Serial0/2/0
 description PG_040 (partner-t3 via T3 Serial)
 ip address 192.168.40.1 255.255.255.252
 dsu bandwidth 44210
 framing c-bit
 cablelength 10
 clock source internal
 no shutdown
!
```

**Block B — the `card type t3 0 2` line is already in §5.2 (line-card type declaration appears near the top of `show run`). Confirm it's present after capture; if not, hand-insert:**

```
card type t3 0 2
```

**Block C — the BGP neighbor for partner-t3 (`192.168.40.2`) is declared in §5.2 and will appear in live capture. No hand-insertion needed for BGP.**

**Block D — the `route-map PG_040-IN/OUT`, `ip prefix-list PL-PARTNER-T3-IN`, `ip community-list CL-PARTNER-T3`, and `ip route 172.16.40.0/24 Null0` are all in §5.2 and appear in live capture. No hand-insertion needed.**

**Hand-insertion summary: Block A only.** Validate by `grep -c "Serial0/2/0" wan-edge.txt` → should report 1 after insertion.

---

## 6. CML Build Notes

### Node palette
- All router nodes: **Cat8000V**
- `sw1`: **Unmanaged Switch** (free)

### Link plan

| Source | Source port | Destination | Destination port | Purpose |
|--------|-------------|-------------|------------------|---------|
| wan-edge | Gi2 | sw1 | port 1 | Trunk: VLAN 2 inside + VLAN 4 outside (Po1 primary member) |
| wan-edge | Gi3 | (unwired) | — | Po1 secondary member — declared in config, not wired |
| core-rr | Gi1 | sw1 | port 2 | Trunk: VLAN 2 only |
| partner-ipsec | Gi1 | sw1 | port 3 | Trunk: VLAN 4 only |
| wan-edge | Gi4 | partner-mpls-sp | Gi1 | Direct P2P (dot1Q 100 on wan-edge side) |
| wan-edge | Gi5 | partner-eth | Gi1 | Direct P2P (untagged) |
| wan-edge | Se0/2/0 | — | — | T3 partner — not wired, config-only |

### Build order
1. Instantiate 5 Cat8000V nodes + 1 unmanaged switch.
2. Wire per the link table. Do NOT wire Gi3 on wan-edge; leave disconnected.
3. Boot all nodes. ~3 min for Cat8000V first-boot.
4. Transcribe configs per §5.1–§5.5. Do not attempt §5.6 yet — handle post-capture.
5. Verify:
   - `show ip ospf neighbor` on wan-edge and core-rr — should show each other as FULL
   - `show bgp ipv4 unicast summary` on wan-edge — should show neighbors 10.255.1.1 (up), 203.0.113.2 (up), 10.200.1.2 (up), 192.168.30.2 (up), 192.168.40.2 (idle — expected, T3 not wired)
   - `show crypto isakmp sa` — IKE SA present with partner-ipsec after traffic triggers it
   - `show ip nat translations` — translations visible if test traffic is generated
   - `show etherchannel summary` — Po1 shows Gi2 up, Gi3 down (expected)
   - `show bgp ipv6 unicast summary` — v6 iBGP to core-rr up

### Expected idle neighbor
- `192.168.40.2` (partner-t3) will show `Idle` — expected, interface Serial0/2/0 doesn't exist on Cat8000V

---

## 7. Capture, Insert, Commit

### Step 1 — Capture

```
! On each live node:
show running-config | redirect flash:<hostname>.txt
```

Pull files from CML's file-transfer panel or via SCP.

### Step 2 — Hand-insert §5.6 Block A into `wan-edge.txt`

Open `wan-edge.txt` and insert the Serial0/2/0 block in the interface section. Appropriate placement: between the last `interface GigabitEthernetN` block and the Loopback blocks (or wherever your `show run` sorts Serial interfaces — typically after physical Ethernet, before tunnel/loopback).

Validate placement:
```bash
grep -n "^interface" wan-edge.txt
# Serial0/2/0 should appear in the expected alphabetical/natural position
```

### Step 3 — Sanity check

```bash
# Run sanitizer on each captured file; identifiers should already be lab-safe
# so output should be byte-identical (or only trivial whitespace diffs)
python3 main.py tests/fixtures/lab/crypto_map_global_wan_edge/wan-edge.txt
diff wan-edge.txt output/wan-edge/sanitized_config.txt
# Expected: empty diff or whitespace-only
```

### Step 4 — Commit layout

```
tests/fixtures/lab/crypto_map_global_wan_edge/
  wan-edge.txt              # primary subject (live capture + §5.6 Block A)
  core-rr.txt               # scaffolding
  partner-ipsec.txt         # scaffolding
  partner-mpls-sp.txt       # scaffolding
  partner-eth.txt           # scaffolding
  SOURCES.md
  topology.yml              # CML topology export
  INSERTION_NOTES.md        # records §5.6 hand-inserted blocks, with rationale
```

**`SOURCES.md` template:**

```markdown
# Source: Crypto-map global WAN edge

Lab-generated fixture reflecting production WAN edge shape observed on
ASR1000-family / IOS-XE 16.x: global-table routing, ISAKMP + crypto map
legacy IPsec, Port-channel with inside/outside VLAN split, NAT, and
peer-group-scoped BGP policy.

## Platform caveat
Lab: Cat8000V (IOS-XE 17.x). Production reference: ASR1000 (IOS-XE 16.3.7).
Minor syntactic differences possible between the two IOS-XE trains.
Any diff surfaced by the parser should be logged as a GitHub issue.

## Topology
See `topology.yml`.

## Identifier conventions
[standard RFC 1918 / 5398 / 5737 / db8 block]

## Post-capture hand-insertions
See `INSERTION_NOTES.md`. One block: Serial0/2/0 T3 interface on wan-edge.

## Verification
[list show commands confirming session up]

## Generated
<YYYY-MM-DD>, CML-Free <version>, Cat8000V image <version>
```

---

## 8. Open Items for Verification

Tomorrow's production-config pass should verify these (in rough priority order):

1. **VRF on cores** — does the "single PARTNER VRF with PBR" pattern you described live on the cores rather than on the WAN edge? If yes, that confirms wan-edge is global-only and Pattern C moves to a *core-side* fixture.
2. **Port-channel LAG primary/secondary syntax** — exact form of `encapsulation dot1Q N primary IF secondary IF` across your config base. Uploaded sample had this form consistently.
3. **Peer-group naming conventions** — production sample used `PG_NNN` post-sanitization; verify pre-sanitization names are peer-groups in the same shape (not per-IP explicit config).
4. **SP handoff style** — is MPLS L3VPN handoff eBGP-to-SP-PE (AS 65030-equivalent) as I've drafted, or eBGP-multihop-to-partner-behind-SP?
5. **ISAKMP policy count & strength distribution** — I've drafted 4 policies (mix of AES256/AES/3DES, SHA256/SHA1, DH 14/2) matching the 4-policy count observed. Verify mix matches production's actual distribution.
6. **Crypto ACL shape** — production likely has hundreds of ACE lines per partner. Fixture has 1 ACE per ACL. If parser coverage needs to include large ACLs, extend.
7. **FNF detail** — draft includes minimal `flow record / exporter / monitor` config. If production FNF is more elaborate (multiple monitors, multiple exporters, more `match`/`collect` statements), extend §5.2.
8. **IPv6 scope** — draft includes v6 only on inside subif + iBGP. If production has v6 on partner peerings too, extend.
9. **NAT pool size & overload vs. one-to-one** — draft uses a single-address overload pool. If production uses larger pools, no-overload, or static translations, adjust.
10. **T3 interface block exact syntax** (§5.6 Block A) — `framing c-bit`, `cablelength`, `dsu bandwidth`, `clock source` all guessed at reasonable defaults. Verify against production T3 config.

---

## 9. Next Fixtures in Queue

| Fixture | Status | Notes |
|---------|--------|-------|
| `ipsec_per_partner_vrf/` (Pattern A, IKEv2) | Drafted (greenfield ref) | `ipsec_pattern_a_spec.md` |
| `crypto_map_global_wan_edge/` | **This doc** | Production-shape WAN edge |
| `ipsec_shared_vrf_pbr/` (Pattern C, possibly on core) | Queued pending verification | May move to a *core-side* fixture depending on tomorrow's check |
| `mpls_l2_handoff/` | Queued | Pure MPLS handoff without IPsec, layer-2 VPN style |
| Legacy crypto-map variants (IKEv1 + 3DES + weaker groups) | Queued (conditional) | If the ISAKMP policy distribution in production warrants a weaker-crypto fixture |

---

*End of spec.*
