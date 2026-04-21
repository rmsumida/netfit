# IPsec Pattern A Fixture — Spec

> **Target:** `tests/fixtures/lab/ipsec_per_partner_vrf/`
> **Status:** Draft spec for lab build. Transcribe per §5, capture per §7, commit the capture (not this spec).
> **Session:** 003

---

## 1. Purpose

Lab-generated fixture representing a single-site WAN edge router terminating one partner IPsec VPN into a dedicated per-partner VRF, with a redundant core pair (HSRP active/standby on the firewall-facing side) and a dedicated route reflector. All iBGP end-to-end; OSPF area 0 underlay. This fixture exercises:

- IKEv2 + IPsec profile + `tunnel protection` (modern crypto)
- Front-door VRF (`FVRF-INET`) for Internet-facing interface
- Partner-scoped VRF (`PARTNER_A`) for decrypted traffic
- VRF-to-global BGP leak at the WAN edge ↔ core boundary (segmentation ends at the firewall)
- OSPF area 0 backbone-only, passive-interface default
- Dedicated route reflector, two RRs referenced in config (one instantiated — see §4)
- HSRP v2 on the firewall-facing subinterfaces of the core pair
- Representative QoS (class-map + policy-map)
- Representative IP SLA + track + conditional default route
- BFD on iBGP peers

The firewall is **omitted** from the lab topology — vWire is L2-transparent, so configs are identical to a real deployment. See §3 for the L2 simplification.

---

## 2. Topology

```
                                                          203.0.113.0/30
                                                         ┌──────────────┐
                    ┌────────────┐                       │              │
                    │ partner-gw │  Gi1 .1 ──────── .2 Gi1 │   wan-edge   │
                    │  AS 65010  │      (Internet sim)    │   AS 65001   │
                    └────────────┘                       │              │
                                                         └──────┬───────┘
                                                                │ Gi2 (trunk)
                                                                │ VLAN 99  (underlay, global)
                                                                │ VLAN 100 (PARTNER_A iBGP)
                                                                │
                                                        ┌───────┴────────┐
                                                        │  sw1 (L2 only) │
                                                        │ unmanaged      │
                                                        └───┬────┬────┬──┘
                                                            │    │    │
                                                Gi2 (trunk) │    │    │ Gi1 (trunk, VLAN 99 only)
                                                            │    │    │
                                                    ┌───────┴┐ ┌─┴───┐ │
                                                    │ core-a │ │core-b│ │
                                                    │ AS 65001│ │AS65001│
                                                    └───┬────┘ └──┬──┘ │
                                                        │         │    │
                                                    Gi3 │═════════│ Gi3│
                                                        │ VLAN 10 │    │
                                                        │ HSRP    │    │
                                                        │ heartbeat    │
                                                                       │
                                                                  ┌────┴────┐
                                                                  │  rr-a   │
                                                                  │ AS 65001│
                                                                  └─────────┘
```

**Node count:** 5 × Cat8000V (fits CML-Free's 5-node cap). `sw1` is an unmanaged switch and doesn't count.

**Direct link between core-a.Gi3 and core-b.Gi3** carries VLAN 10 as an L2 trunk for HSRP peer adjacency. In production, this adjacency is provided by the firewall pair's L2 bridging; in lab it's a direct cable. **Captured configs are identical either way.**

---

## 3. Addressing & Identifier Plan

### ASNs

| Role | ASN | Notes |
|------|-----|-------|
| Internal | 65001 | RFC 5398 documentation-use |
| Partner | 65010 | RFC 5398 documentation-use |

### Loopback (router-id + iBGP peer address)

| Node | Loopback0 |
|------|-----------|
| wan-edge | 10.255.0.1/32 |
| core-a | 10.255.0.2/32 |
| core-b | 10.255.0.3/32 |
| rr-a | 10.255.1.1/32 |
| rr-b (not instantiated) | 10.255.1.2/32 |
| partner-gw | 192.0.2.1/32 |

### VLANs

| VLAN | Purpose | Scope | Participants |
|------|---------|-------|--------------|
| 10 | Firewall-facing LAN, HSRP | Global | core-a, core-b |
| 99 | Underlay (OSPF area 0) | Global | wan-edge, core-a, core-b, rr-a |
| 100 | PARTNER_A iBGP peering | Mixed* | wan-edge (VRF PARTNER_A), core-a (global), core-b (global) |

\* VLAN 100 is the segmentation-ends-at-firewall leak point. WAN edge side is in VRF PARTNER_A; core side is in global. BGP peering across this VLAN is how partner routes cross from the VRF into global.

### Subnets

| Subnet | Scope | Assignment |
|--------|-------|------------|
| 203.0.113.0/30 | Internet sim | partner-gw Gi1 = .1, wan-edge Gi1 (FVRF-INET) = .2 |
| 10.99.99.0/29 | VLAN 99 underlay | wan-edge = .1, core-a = .2, core-b = .3, rr-a = .4 |
| 10.100.100.0/29 | VLAN 100 VRF iBGP | wan-edge (VRF) = .1, core-a = .2, core-b = .3 |
| 10.10.10.0/24 | VLAN 10 HSRP | HSRP VIP = .1, core-a = .2, core-b = .3 |
| 10.200.1.0/30 | Tunnel100 inside | wan-edge (VRF) = .1, partner-gw = .2 |
| 192.0.2.0/24 | Partner prefix | Advertised by partner-gw |

### HSRP

| Group | VIP | Priority: core-a | Priority: core-b |
|-------|-----|------------------|------------------|
| 10 | 10.10.10.1 | 110 (preempt, active) | 100 (standby) |

### Pre-shared keys

All PSKs are placeholder strings beginning with `lab-psk-`. The sanitizer should redact these on ingest regardless, but using the `lab-psk-` prefix makes manual inspection trivial.

### Domain

`lab.netfit.local`

---

## 4. Assumptions Called Out

These are decisions made for the draft. Verify against production and flag any that don't match:

1. **OSPF area 0 backbone-only**, no area 1+. WAN edge, cores, and RR all in area 0.
2. **OSPF passive-interface default**, `no passive-interface` on the VLAN 99 subinterfaces only.
3. **Global iBGP from WAN edge to RR is included**, even though the WAN edge's global RIB contains only its own loopback. Common in real enterprise fabrics for consistency; drop if your production doesn't do this.
4. **VRF-to-global BGP leak** happens at the WAN edge ↔ core VLAN 100 peering — WAN edge advertises from AF `ipv4 vrf PARTNER_A`, core receives into AF `ipv4 unicast` (global). Both peers are AS 65001, so iBGP; the VRF asymmetry is handled by each peer's local address-family scope.
5. **Second RR (rr-b) is referenced but not instantiated.** WAN edge and core configs list both `10.255.1.1` and `10.255.1.2` as iBGP peers. The session to `10.255.1.2` will stay down in lab — this is expected and the captured config is still faithful to a 2-RR design.
6. **No NAT on this fixture.** Partner-sourced traffic (192.0.2.0/24) is routed end-to-end. A NAT variant comes later as `ipsec_per_partner_vrf_nat/` if/when Pattern A with NAT shows up in production.
7. **BFD on iBGP only**, not on eBGP over tunnel (tunnel keepalive + IKEv2 DPD handle that).
8. **One partner, one tunnel.** Multi-partner variants come later.
9. **QoS is illustrative.** One voice class, one critical class, default class — enough to exercise the parser without inflating the config.
10. **IP SLA + track + conditional default** points at `8.8.8.8` as a public reachability probe. Production likely probes a specific SP gateway or monitoring destination; adjust per environment.

---

## 5. Device Configs

Each config below is a **spec**, not a literal paste. IOS-XE auto-generates certain lines (`version`, `service timestamps`, AAA defaults, etc.) — those will appear in the captured `show running-config` but are omitted here for readability. Transcribe the explicit config; let IOS-XE fill in the rest; capture.

### 5.1 partner-gw

```
hostname partner-gw
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 192.0.2.1 255.255.255.255
!
interface GigabitEthernet1
 description to wan-edge (Internet sim)
 ip address 203.0.113.1 255.255.255.252
 no shutdown
!
! --- IKEv2 + IPsec (partner side) ---
crypto ikev2 proposal PROP-1
 encryption aes-cbc-256
 integrity sha256
 group 14
!
crypto ikev2 policy POL-1
 proposal PROP-1
!
crypto ikev2 keyring KR-WAN
 peer wan-edge
  address 203.0.113.2
  pre-shared-key local lab-psk-partner-to-wan
  pre-shared-key remote lab-psk-wan-to-partner
!
crypto ikev2 profile PROF-WAN
 match identity remote address 203.0.113.2 255.255.255.255
 authentication remote pre-share
 authentication local pre-share
 keyring local KR-WAN
 lifetime 3600
!
crypto ipsec transform-set TS-1 esp-aes 256 esp-sha256-hmac
 mode tunnel
!
crypto ipsec profile PROT-WAN
 set transform-set TS-1
 set ikev2-profile PROF-WAN
 set pfs group14
!
interface Tunnel100
 description to wan-edge (partner IPsec)
 ip address 10.200.1.2 255.255.255.252
 tunnel source GigabitEthernet1
 tunnel mode ipsec ipv4
 tunnel destination 203.0.113.2
 tunnel protection ipsec profile PROT-WAN
!
! --- BGP (partner side) ---
router bgp 65010
 bgp router-id 192.0.2.1
 bgp log-neighbor-changes
 neighbor 10.200.1.1 remote-as 65001
 !
 address-family ipv4 unicast
  network 192.0.2.0 mask 255.255.255.0
  neighbor 10.200.1.1 activate
 exit-address-family
!
ip route 192.0.2.0 255.255.255.0 Null0
!
end
```

---

### 5.2 wan-edge

```
hostname wan-edge
!
ip domain name lab.netfit.local
!
! --- VRFs ---
vrf definition FVRF-INET
 description Front-door VRF for Internet-facing transport
 address-family ipv4
 exit-address-family
!
vrf definition PARTNER_A
 description Decrypted partner-A traffic
 rd 65001:100
 route-target export 65001:100
 route-target import 65001:100
 address-family ipv4
 exit-address-family
!
! --- Loopback (global underlay) ---
interface Loopback0
 description router-id + iBGP peer address
 ip address 10.255.0.1 255.255.255.255
!
! --- Internet-facing (in FVRF-INET) ---
interface GigabitEthernet1
 description to partner-gw (Internet sim)
 vrf forwarding FVRF-INET
 ip address 203.0.113.2 255.255.255.252
 no shutdown
!
! --- Core-facing trunk (to sw1) ---
interface GigabitEthernet2
 description trunk to core-a and core-b via sw1
 no ip address
 no shutdown
!
interface GigabitEthernet2.99
 description Underlay OSPF area 0
 encapsulation dot1Q 99
 ip address 10.99.99.1 255.255.255.248
 ip ospf network broadcast
 bfd interval 300 min_rx 300 multiplier 3
!
interface GigabitEthernet2.100
 description PARTNER_A iBGP to core-a / core-b
 encapsulation dot1Q 100
 vrf forwarding PARTNER_A
 ip address 10.100.100.1 255.255.255.248
!
! --- IKEv2 + IPsec ---
crypto ikev2 proposal PROP-1
 encryption aes-cbc-256
 integrity sha256
 group 14
!
crypto ikev2 policy POL-1
 proposal PROP-1
!
crypto ikev2 keyring KR-PARTNER-A
 peer partner-a
  address 203.0.113.1
  pre-shared-key local lab-psk-wan-to-partner
  pre-shared-key remote lab-psk-partner-to-wan
!
crypto ikev2 profile PROF-PARTNER-A
 match fvrf FVRF-INET
 match identity remote address 203.0.113.1 255.255.255.255
 authentication remote pre-share
 authentication local pre-share
 keyring local KR-PARTNER-A
 lifetime 3600
!
crypto ipsec transform-set TS-1 esp-aes 256 esp-sha256-hmac
 mode tunnel
!
crypto ipsec profile PROT-PARTNER-A
 set transform-set TS-1
 set ikev2-profile PROF-PARTNER-A
 set pfs group14
!
! --- Tunnel (inside VRF PARTNER_A, sourced from FVRF-INET) ---
interface Tunnel100
 description Partner-A IPsec tunnel
 vrf forwarding PARTNER_A
 ip address 10.200.1.1 255.255.255.252
 ip mtu 1400
 ip tcp adjust-mss 1360
 tunnel source GigabitEthernet1
 tunnel mode ipsec ipv4
 tunnel destination 203.0.113.1
 tunnel vrf FVRF-INET
 tunnel protection ipsec profile PROT-PARTNER-A
 bfd interval 500 min_rx 500 multiplier 3
!
! --- OSPF (underlay, global area 0) ---
router ospf 1
 router-id 10.255.0.1
 passive-interface default
 no passive-interface GigabitEthernet2.99
 network 10.255.0.1 0.0.0.0 area 0
 network 10.99.99.0 0.0.0.7 area 0
 bfd all-interfaces
!
! --- BGP ---
router bgp 65001
 bgp router-id 10.255.0.1
 bgp log-neighbor-changes
 bgp listen limit 100
 !
 ! Global iBGP to RRs (loopback-to-loopback)
 neighbor 10.255.1.1 remote-as 65001
 neighbor 10.255.1.1 description rr-a
 neighbor 10.255.1.1 update-source Loopback0
 neighbor 10.255.1.1 fall-over bfd
 neighbor 10.255.1.2 remote-as 65001
 neighbor 10.255.1.2 description rr-b
 neighbor 10.255.1.2 update-source Loopback0
 neighbor 10.255.1.2 fall-over bfd
 !
 address-family ipv4 unicast
  neighbor 10.255.1.1 activate
  neighbor 10.255.1.1 send-community both
  neighbor 10.255.1.2 activate
  neighbor 10.255.1.2 send-community both
 exit-address-family
 !
 ! eBGP to partner over Tunnel100 + VRF iBGP to cores (VLAN 100 leak)
 address-family ipv4 vrf PARTNER_A
  ! eBGP to partner
  neighbor 10.200.1.2 remote-as 65010
  neighbor 10.200.1.2 description partner-a via tunnel
  neighbor 10.200.1.2 activate
  neighbor 10.200.1.2 send-community both
  neighbor 10.200.1.2 route-map RM-PARTNER-IN in
  neighbor 10.200.1.2 route-map RM-PARTNER-OUT out
  !
  ! iBGP to cores (VLAN 100 — leaks VRF routes into core global)
  neighbor 10.100.100.2 remote-as 65001
  neighbor 10.100.100.2 description core-a (VRF leak)
  neighbor 10.100.100.2 activate
  neighbor 10.100.100.2 send-community both
  neighbor 10.100.100.2 next-hop-self
  neighbor 10.100.100.3 remote-as 65001
  neighbor 10.100.100.3 description core-b (VRF leak)
  neighbor 10.100.100.3 activate
  neighbor 10.100.100.3 send-community both
  neighbor 10.100.100.3 next-hop-self
 exit-address-family
!
! --- Route-maps / communities (representative) ---
ip community-list standard CL-PARTNER-A permit 65001:100
!
route-map RM-PARTNER-IN permit 10
 set community 65001:100 additive
 set local-preference 200
!
route-map RM-PARTNER-OUT permit 10
 match ip address prefix-list PL-INTERNAL-AGGREGATES
!
ip prefix-list PL-INTERNAL-AGGREGATES seq 10 permit 10.0.0.0/8 le 24
!
! --- QoS (representative) ---
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
  random-detect dscp-based
!
interface Tunnel100
 service-policy output PM-WAN-OUT
!
! --- IP SLA + track + conditional default (FVRF-INET) ---
ip sla 1
 icmp-echo 8.8.8.8 source-interface GigabitEthernet1
 vrf FVRF-INET
 threshold 500
 frequency 5
ip sla schedule 1 life forever start-time now
!
track 1 ip sla 1 reachability
!
ip route vrf FVRF-INET 0.0.0.0 0.0.0.0 203.0.113.1 track 1
!
end
```

---

### 5.3 core-a (HSRP active)

```
hostname core-a
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 10.255.0.2 255.255.255.255
!
! --- WAN-edge-facing trunk (to sw1) ---
interface GigabitEthernet2
 description trunk to wan-edge and core-b via sw1
 no ip address
 no shutdown
!
interface GigabitEthernet2.99
 description Underlay OSPF area 0
 encapsulation dot1Q 99
 ip address 10.99.99.2 255.255.255.248
 ip ospf network broadcast
 bfd interval 300 min_rx 300 multiplier 3
!
interface GigabitEthernet2.100
 description PARTNER_A iBGP peer with wan-edge (VRF leak to global)
 encapsulation dot1Q 100
 ip address 10.100.100.2 255.255.255.248
!
! --- Firewall-facing (HSRP peer link direct to core-b) ---
interface GigabitEthernet3
 description HSRP peer link to core-b (firewall-facing VLAN 10)
 no ip address
 no shutdown
!
interface GigabitEthernet3.10
 description Firewall-facing LAN (HSRP active)
 encapsulation dot1Q 10
 ip address 10.10.10.2 255.255.255.0
 standby version 2
 standby 10 ip 10.10.10.1
 standby 10 priority 110
 standby 10 preempt delay minimum 60
 standby 10 timers msec 250 msec 750
 standby 10 name FW-FACING
!
! --- OSPF (global area 0) ---
router ospf 1
 router-id 10.255.0.2
 passive-interface default
 no passive-interface GigabitEthernet2.99
 no passive-interface GigabitEthernet3.10
 network 10.255.0.2 0.0.0.0 area 0
 network 10.99.99.0 0.0.0.7 area 0
 network 10.10.10.0 0.0.0.255 area 0
 bfd all-interfaces
!
! --- BGP ---
router bgp 65001
 bgp router-id 10.255.0.2
 bgp log-neighbor-changes
 !
 ! Global iBGP to RRs
 neighbor 10.255.1.1 remote-as 65001
 neighbor 10.255.1.1 description rr-a
 neighbor 10.255.1.1 update-source Loopback0
 neighbor 10.255.1.1 fall-over bfd
 neighbor 10.255.1.2 remote-as 65001
 neighbor 10.255.1.2 description rr-b
 neighbor 10.255.1.2 update-source Loopback0
 neighbor 10.255.1.2 fall-over bfd
 !
 ! VRF-leak iBGP to wan-edge (VLAN 100)
 neighbor 10.100.100.1 remote-as 65001
 neighbor 10.100.100.1 description wan-edge (VRF PARTNER_A leak)
 !
 address-family ipv4 unicast
  neighbor 10.255.1.1 activate
  neighbor 10.255.1.1 send-community both
  neighbor 10.255.1.1 next-hop-self
  neighbor 10.255.1.2 activate
  neighbor 10.255.1.2 send-community both
  neighbor 10.255.1.2 next-hop-self
  neighbor 10.100.100.1 activate
  neighbor 10.100.100.1 send-community both
  neighbor 10.100.100.1 next-hop-self
 exit-address-family
!
end
```

---

### 5.4 core-b (HSRP standby)

Identical to core-a except for identifiers and HSRP priority. Only the deltas are listed below; apply the rest from §5.3 verbatim with address substitution.

**Deltas from core-a:**

```
hostname core-b
!
interface Loopback0
 ip address 10.255.0.3 255.255.255.255
!
interface GigabitEthernet2.99
 ip address 10.99.99.3 255.255.255.248
!
interface GigabitEthernet2.100
 ip address 10.100.100.3 255.255.255.248
!
interface GigabitEthernet3.10
 ip address 10.10.10.3 255.255.255.0
 standby version 2
 standby 10 ip 10.10.10.1
 standby 10 priority 100
 standby 10 preempt delay minimum 60
 standby 10 timers msec 250 msec 750
 standby 10 name FW-FACING
!
router ospf 1
 router-id 10.255.0.3
 ! (network statements identical to core-a using core-b's own addresses)
!
router bgp 65001
 bgp router-id 10.255.0.3
 ! (all neighbor + AF config identical to core-a except router-id)
```

Everything else (HSRP timers, BFD, neighbor RR definitions, AF configuration) is identical.

---

### 5.5 rr-a

```
hostname rr-a
!
ip domain name lab.netfit.local
!
interface Loopback0
 ip address 10.255.1.1 255.255.255.255
!
interface GigabitEthernet1
 description to sw1 (underlay trunk, VLAN 99 only)
 no ip address
 no shutdown
!
interface GigabitEthernet1.99
 description Underlay OSPF area 0
 encapsulation dot1Q 99
 ip address 10.99.99.4 255.255.255.248
 ip ospf network broadcast
 bfd interval 300 min_rx 300 multiplier 3
!
router ospf 1
 router-id 10.255.1.1
 passive-interface default
 no passive-interface GigabitEthernet1.99
 network 10.255.1.1 0.0.0.0 area 0
 network 10.99.99.0 0.0.0.7 area 0
 bfd all-interfaces
!
router bgp 65001
 bgp router-id 10.255.1.1
 bgp log-neighbor-changes
 bgp cluster-id 10.255.1.1
 !
 neighbor 10.255.0.1 remote-as 65001
 neighbor 10.255.0.1 description wan-edge (client)
 neighbor 10.255.0.1 update-source Loopback0
 neighbor 10.255.0.1 fall-over bfd
 neighbor 10.255.0.2 remote-as 65001
 neighbor 10.255.0.2 description core-a (client)
 neighbor 10.255.0.2 update-source Loopback0
 neighbor 10.255.0.2 fall-over bfd
 neighbor 10.255.0.3 remote-as 65001
 neighbor 10.255.0.3 description core-b (client)
 neighbor 10.255.0.3 update-source Loopback0
 neighbor 10.255.0.3 fall-over bfd
 !
 address-family ipv4 unicast
  neighbor 10.255.0.1 activate
  neighbor 10.255.0.1 route-reflector-client
  neighbor 10.255.0.1 send-community both
  neighbor 10.255.0.2 activate
  neighbor 10.255.0.2 route-reflector-client
  neighbor 10.255.0.2 send-community both
  neighbor 10.255.0.3 activate
  neighbor 10.255.0.3 route-reflector-client
  neighbor 10.255.0.3 send-community both
 exit-address-family
!
end
```

---

## 6. CML Build Notes

### Node palette
- All router nodes: **Cat8000V** (virtual Catalyst 8000V Edge Platform, IOS-XE)
- `sw1`: **Unmanaged Switch** (free, doesn't count against 5-node cap)

### Interface/link plan

| Source | Source port | Destination | Destination port | Purpose |
|--------|-------------|-------------|------------------|---------|
| partner-gw | Gi1 | wan-edge | Gi1 | Internet sim (203.0.113.0/30) |
| wan-edge | Gi2 | sw1 | port 1 | Trunk: VLAN 99, 100 |
| core-a | Gi2 | sw1 | port 2 | Trunk: VLAN 99, 100 |
| core-b | Gi2 | sw1 | port 3 | Trunk: VLAN 99, 100 |
| rr-a | Gi1 | sw1 | port 4 | Trunk: VLAN 99 only |
| core-a | Gi3 | core-b | Gi3 | Direct, VLAN 10 (HSRP) |

### Build order
1. Instantiate all 5 Cat8000V nodes + 1 unmanaged switch.
2. Wire per the table above.
3. Boot all nodes. Allow ~3 min for Cat8000V first-boot.
4. Console into each node. Apply the config from §5. Save.
5. Verify: `show ip ospf neighbor`, `show bgp ipv4 unicast summary`, `show bgp ipv4 vrf PARTNER_A summary`, `show crypto ikev2 sa`, `show standby brief`.
6. Expected state: OSPF full adjacencies among wan-edge, core-a, core-b, rr-a; global iBGP up to rr-a (rr-b down — expected); VRF eBGP up to partner-gw; VRF iBGP up between wan-edge and both cores; IKEv2 SA installed; HSRP active on core-a.

---

## 7. Capture & Commit

Once the lab is up and verified:

```bash
# On each node:
show running-config | redirect flash:<hostname>.txt
# SCP/copy out, or pull via CML's file transfer
```

Commit the captures as:

```
tests/fixtures/lab/ipsec_per_partner_vrf/
  partner-gw.txt
  wan-edge.txt
  core-a.txt
  core-b.txt
  rr-a.txt
  SOURCES.md
  topology.yml        # CML topology export, optional but recommended
```

**`SOURCES.md` content template:**

```
# Source: IPsec per-partner VRF (Pattern A)

Lab-generated from CML-Free. Represents a single-site WAN edge terminating
one partner IPsec tunnel into a dedicated VRF, with redundant cores (HSRP)
and a dedicated route reflector.

## Topology
See `topology.yml`.

## Identifier conventions
- Internal ASN: 65001 (RFC 5398)
- Partner ASN: 65010
- Public IPs: RFC 5737 documentation ranges (192.0.2.0/24, 203.0.113.0/30)
- Internal IPs: RFC 1918 (10.0.0.0/8)
- Pre-shared keys: all `lab-psk-*` placeholders
- Domain: lab.netfit.local

## Verification commands
(list the show commands used to confirm fixture validity)

## Generated
<YYYY-MM-DD>, CML-Free <version>, Cat8000V image <version>
```

**Sanity check before commit:**

```bash
# Run sanitizer over each file; output should be byte-identical to input.
# (Since lab identifiers are already sanitizer-safe, this asserts idempotency.)
python3 main.py tests/fixtures/lab/ipsec_per_partner_vrf/wan-edge.txt \
  --no-sanitize
# Then:
diff wan-edge.txt output/wan-edge/sanitized_config.txt
# Expected: no diff, or only trailing whitespace
```

---

## 8. Open Items for Verification Against Production

These are items explicitly flagged as needing real-config verification. Each should be checked before the fixture is considered production-faithful:

1. **Dedicated vs. core-colocated RRs** — I've assumed dedicated (based on your statement). Verify.
2. **Global iBGP from WAN edge** — I've included it; drop if production doesn't.
3. **VRF iBGP neighbor count from WAN edge** — I've included both core-a and core-b as VRF iBGP peers from the WAN edge. Verify production doesn't funnel all VRF traffic through only the HSRP-active core.
4. **BFD timers** — I've used 300/300/3 on physical subifs and 500/500/3 on the tunnel. Adjust to match your production profile.
5. **HSRP preempt delay** — 60s chosen conservatively; match production.
6. **IKEv2 proposal / IPsec transform-set** — I've used `aes-cbc-256 + sha256 + DH14`. Verify against your actual standard (some orgs mandate GCM, DH19/20, AES-256-CBC vs. GCM, etc.).
7. **`ip mtu 1400` + `ip tcp adjust-mss 1360`** on Tunnel100 — common but varies.
8. **Route-map detail** — `RM-PARTNER-IN`/`RM-PARTNER-OUT` as drafted are illustrative; real production maps will have more match/set clauses.
9. **PBR absence** — correctly absent in Pattern A (that's the whole point). PBR lives in the Pattern C fixture (`ipsec_shared_vrf_pbr/`), to be drafted next.

---

## 9. Next Fixtures in Queue

| Fixture | Status | Delta from this spec |
|---------|--------|---------------------|
| `ipsec_per_partner_vrf/` | **This doc** | — |
| `ipsec_shared_vrf_pbr/` (Pattern C) | Next up | Single PARTNER VRF on WAN edge, PBR steering to multiple firewall-context VLANs, NAT on decrypted traffic path |
| `mpls_l2_handoff/` | Queued | SP-facing subinterface per VRF, eBGP to SP PE, no IPsec |
| `ipsec_per_partner_vrf_nat/` | Queued (conditional) | Only if production runs Pattern A with NAT |
| `ipsec_legacy_crypto_map/` | Queued (conditional) | crypto-map + ISAKMP variant for long-standing partners |

---

*End of spec.*
