import json
import yaml

from allocation import allocate_speed_capacity


SEVERITY_SCORES = {
    "critical": 40,
    "high": 25,
    "medium": 15,
    "low": 5,
    "info": 0
}


def load_target_profile(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_analysis_report(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_assessment(assessment, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(assessment, f, indent=2)


def make_finding(category, severity, title, detail, recommendation):
    return {
        "category": category,
        "severity": severity,
        "score": SEVERITY_SCORES.get(severity, 0),
        "title": title,
        "detail": detail,
        "recommendation": recommendation
    }


def _get(dct, path, default=None):
    current = dct
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _add_headroom_finding(findings, category, title, current, maximum, recommendation):
    if maximum in (None, 0):
        return

    try:
        ratio = current / maximum
    except ZeroDivisionError:
        return

    if ratio > 1.0:
        return
    if ratio >= 0.90:
        severity = "medium"
    elif ratio >= 0.75:
        severity = "low"
    else:
        return

    findings.append(make_finding(
        category,
        severity,
        title,
        f"Current usage is {current} out of {maximum} ({ratio:.0%}) of target profile scale.",
        recommendation
    ))


def assess_refresh(analysis, target):
    findings = []

    target_name = target.get("platform_name", "UNKNOWN_TARGET")
    capabilities = target.get("capabilities", {})
    scale = target.get("scale", {})
    constraints = target.get("constraints", {})
    notes = target.get("notes", [])

    # --------------------------------------------------
    # Interface / scale checks
    # --------------------------------------------------
    # Physical port demand: prefer the iteration-1 `active_physical_count`
    # signal, which excludes shutdown ports, subinterfaces, and logical
    # interfaces. Fall back to the raw total ONLY if the analyzer output is
    # a pre-iteration-1 shape missing the `active_*` fields — otherwise the
    # scale check fires on hundreds of shutdown/legacy interfaces and
    # declares every platform NOT_RECOMMENDED.
    active_physical_count = _get(analysis, ["interfaces", "active_physical_count"])
    active_physical_by_type = _get(analysis, ["interfaces", "active_physical_by_type"], {}) or {}
    active_subinterfaces = _get(analysis, ["interfaces", "active_subinterfaces"])
    current_total_intf = _get(analysis, ["interfaces", "total"], 0)
    current_by_type = _get(analysis, ["interfaces", "by_type"], {})
    current_l2_access = _get(analysis, ["interfaces", "layer2_access_count"], 0)
    current_l2_trunk = _get(analysis, ["interfaces", "layer2_trunk_count"], 0)
    current_l3 = _get(analysis, ["interfaces", "layer3_count"], 0)
    current_portchannels = _get(analysis, ["interfaces", "port_channels"], 0)
    current_tunnels = _get(analysis, ["interfaces", "tunnels"], 0)
    current_subinterfaces = _get(analysis, ["interfaces", "subinterfaces"], 0)

    # Use active physical where we have it, total as legacy fallback.
    effective_physical_demand = (
        active_physical_count if active_physical_count is not None else current_total_intf
    )
    effective_subinterface_demand = (
        active_subinterfaces if active_subinterfaces is not None else current_subinterfaces
    )
    # Unsupported-type detection should look at ACTIVE types only — a
    # decommissioned Serial block from a previous role shouldn't tank the
    # recommendation. Fall back to raw by_type only if active_* absent.
    types_for_compatibility = active_physical_by_type or current_by_type

    max_interfaces = scale.get("max_interfaces")
    max_physical_interfaces = scale.get("max_physical_interfaces") or max_interfaces
    max_access_ports = scale.get("max_access_ports")
    max_trunk_ports = scale.get("max_trunk_ports")
    max_l3_interfaces = scale.get("max_l3_interfaces")
    max_port_channels = scale.get("max_port_channels")
    max_tunnels = scale.get("max_tunnels")
    max_subinterfaces = scale.get("max_subinterfaces")
    max_vrfs = scale.get("max_vrfs")
    max_bgp_neighbors = scale.get("max_bgp_neighbors")
    max_static_routes = scale.get("max_static_routes")
    max_vlans = scale.get("max_vlans")

    if max_physical_interfaces is not None and effective_physical_demand > max_physical_interfaces:
        findings.append(make_finding(
            "interfaces",
            "critical",
            "Active physical interface count exceeds target platform scale",
            f"Current config uses {effective_physical_demand} active physical interfaces, "
            f"but target {target_name} supports {max_physical_interfaces}.",
            "Choose a higher-scale platform, modular option, or redesign interface allocation."
        ))

    # Speed-class allocation: do source-device demand by speed class fit into
    # the target platform's native port supply (with upward substitution)?
    # Without this finding, an unmet 10G demand was invisible to the verdict
    # and a platform with port-mix gaps could still earn LIKELY_FIT.
    active_physical_by_speed_class = (
        _get(analysis, ["interfaces", "active_physical_by_speed_class"], {}) or {}
    )
    target_native_supply = scale.get("ports", {}).get("native", {}) or {}
    if active_physical_by_speed_class and target_native_supply:
        alloc = allocate_speed_capacity(
            active_physical_by_speed_class, target_native_supply
        )
        if not alloc["allocation_ok"]:
            unmet = alloc["unmet_demand"]
            unmet_high = sum(v for k, v in unmet.items() if k in ("40G", "100G"))
            unmet_mid = sum(v for k, v in unmet.items() if k in ("10G", "25G"))
            unmet_low = sum(v for k, v in unmet.items() if k == "1G")
            unmet_str = ", ".join(f"{k}={v}" for k, v in unmet.items())
            if unmet_high:
                severity = "high"
            elif unmet_mid:
                severity = "high"
            elif unmet_low:
                severity = "medium"
            else:
                severity = "low"
            findings.append(make_finding(
                "interfaces",
                severity,
                "Port-speed demand exceeds target native supply",
                f"Source device needs {sum(active_physical_by_speed_class.values())} "
                f"physical ports across speed classes; after upward substitution, "
                f"{unmet_str} cannot be satisfied by {target_name} native ports.",
                "Plan a port-mix conversion (breakout, line-card swap, or transceiver "
                "rationalization) or pick a platform whose native supply matches the "
                "demand profile."
            ))

    if max_access_ports is not None and current_l2_access > max_access_ports:
        findings.append(make_finding(
            "interfaces",
            "high",
            "Access port demand exceeds target profile",
            f"Current config shows {current_l2_access} access interfaces, exceeding target limit of {max_access_ports}.",
            "Validate required access density and consider a larger access platform or stack design."
        ))

    if max_trunk_ports is not None and current_l2_trunk > max_trunk_ports:
        findings.append(make_finding(
            "interfaces",
            "medium",
            "Trunk port count exceeds target profile",
            f"Current config shows {current_l2_trunk} trunk interfaces, above target limit of {max_trunk_ports}.",
            "Review uplink design and verify trunk scale on the replacement platform."
        ))

    if max_l3_interfaces is not None and current_l3 > max_l3_interfaces:
        findings.append(make_finding(
            "routing",
            "high",
            "Layer 3 interface scale exceeds target profile",
            f"Current config uses {current_l3} Layer 3 interfaces, above target limit of {max_l3_interfaces}.",
            "Confirm routed interface/SVI scale or select a platform with higher Layer 3 capacity."
        ))

    if max_port_channels is not None and current_portchannels > max_port_channels:
        findings.append(make_finding(
            "switching",
            "medium",
            "Port-channel scale exceeds target profile",
            f"Current device uses {current_portchannels} port-channels, above target limit of {max_port_channels}.",
            "Validate EtherChannel/LACP scale and redesign aggregation if needed."
        ))

    if max_tunnels is not None and current_tunnels > max_tunnels:
        findings.append(make_finding(
            "crypto_vpn",
            "high",
            "Tunnel scale exceeds target platform profile",
            f"Current config uses {current_tunnels} tunnel interfaces, above target limit of {max_tunnels}.",
            "Confirm tunnel/GRE/IPsec scale and crypto throughput on the target device."
        ))

    if max_subinterfaces is not None and effective_subinterface_demand > max_subinterfaces:
        findings.append(make_finding(
            "interfaces",
            "high",
            "Subinterface scale exceeds target platform profile",
            f"Current config uses {effective_subinterface_demand} subinterfaces, above target limit of {max_subinterfaces}.",
            "Validate routed subinterface scale and handoff design on the replacement platform."
        ))

    if effective_subinterface_demand > 0 and not capabilities.get("supports_subinterfaces", True):
        findings.append(make_finding(
            "interfaces",
            "critical",
            "Subinterfaces required but unsupported by target profile",
            "Current config uses subinterfaces, but target profile does not indicate support.",
            "Choose a platform that supports routed subinterfaces or redesign the interface model."
        ))

    # Headroom advisories
    _add_headroom_finding(
        findings,
        "interfaces",
        "Active physical interface count is approaching target platform scale",
        effective_physical_demand,
        max_physical_interfaces,
        "Leave operational headroom for growth, temporary migration states, and future services."
    )
    _add_headroom_finding(
        findings,
        "interfaces",
        "Layer 3 interface usage is approaching target platform scale",
        current_l3,
        max_l3_interfaces,
        "Validate future routed-interface growth and ensure adequate headroom."
    )
    _add_headroom_finding(
        findings,
        "switching",
        "Port-channel usage is approaching target platform scale",
        current_portchannels,
        max_port_channels,
        "Review aggregation design and expected future uplink growth."
    )
    _add_headroom_finding(
        findings,
        "crypto_vpn",
        "Tunnel usage is approaching target platform scale",
        current_tunnels,
        max_tunnels,
        "Validate expected tunnel growth and encrypted throughput needs."
    )
    _add_headroom_finding(
        findings,
        "interfaces",
        "Subinterface usage is approaching target platform scale",
        effective_subinterface_demand,
        max_subinterfaces,
        "Confirm VLAN handoff and routed segmentation growth expectations."
    )

    # --------------------------------------------------
    # Interface type compatibility
    # --------------------------------------------------
    # Check types in ACTIVE use (`active_physical_by_type`) against the
    # target's supported list. Types present only on shutdown/legacy
    # interfaces shouldn't drag the recommendation down — they'll disappear
    # in the refresh. Fall back to raw by_type for pre-iteration-1 reports.
    supported_interface_types = capabilities.get("supported_interface_types", [])
    unsupported_types = [
        intf_type for intf_type, count in types_for_compatibility.items()
        if count > 0 and supported_interface_types and intf_type not in supported_interface_types
    ]

    if unsupported_types:
        findings.append(make_finding(
            "interfaces",
            "critical",
            "Unsupported interface types detected",
            f"Current config uses interface types not listed as supported by {target_name}: {', '.join(sorted(unsupported_types))}.",
            "Validate media/module support or select a platform that supports the required interface types."
        ))

    # --------------------------------------------------
    # VLAN / switching checks
    # --------------------------------------------------
    vlan_count = _get(analysis, ["switching", "vlans_defined_count"], 0)
    trunking_present = _get(analysis, ["switching", "trunking_present"], False)
    etherchannel_present = _get(analysis, ["switching", "etherchannel_present"], False)
    spanning_tree_present = _get(analysis, ["switching", "spanning_tree", "present"], False)

    if max_vlans is not None and vlan_count > max_vlans:
        findings.append(make_finding(
            "switching",
            "high",
            "VLAN scale exceeds target platform profile",
            f"Current config defines {vlan_count} VLANs, above target limit of {max_vlans}.",
            "Validate VLAN scale or consolidate VLAN design before migration."
        ))

    _add_headroom_finding(
        findings,
        "switching",
        "VLAN usage is approaching target platform scale",
        vlan_count,
        max_vlans,
        "Validate future VLAN growth and whether switching functions should remain on this node."
    )
    _add_headroom_finding(
        findings,
        "interfaces",
        "Trunk port usage is approaching target platform scale",
        current_l2_trunk,
        max_trunk_ports,
        "Review uplink design and verify future trunk growth."
    )

    if trunking_present and not capabilities.get("supports_trunking", False):
        findings.append(make_finding(
            "switching",
            "critical",
            "Target platform does not support required trunking",
            "Current config uses 802.1Q trunking, but target profile does not indicate trunking support.",
            "Select a target platform with Layer 2 trunk support or redesign the topology."
        ))

    if etherchannel_present and not capabilities.get("supports_etherchannel", False):
        findings.append(make_finding(
            "switching",
            "high",
            "EtherChannel required but not supported in target profile",
            "Current config uses port-channels/channel-groups, but target profile does not indicate EtherChannel support.",
            "Validate LACP/PAgP capabilities or redesign uplinks."
        ))

    if spanning_tree_present and not capabilities.get("supports_spanning_tree", False):
        findings.append(make_finding(
            "switching",
            "high",
            "Spanning-tree dependency not supported by target profile",
            "Current config includes spanning-tree configuration, but target profile does not indicate STP support.",
            "Validate switching feature parity or choose a platform intended for Layer 2 edge/distribution roles."
        ))

    # --------------------------------------------------
    # VRF / routing checks
    # --------------------------------------------------
    vrf_present = _get(analysis, ["routing", "vrf_present"], False)
    vrf_count = len(_get(analysis, ["routing", "vrfs"], []))
    protocols = _get(analysis, ["routing", "protocols"], {})
    bgp_neighbor_count = _get(analysis, ["routing", "bgp", "neighbor_count"], 0)
    static_route_count = _get(analysis, ["routing", "static_route_count"], 0)

    ipv6_present = (
        _get(analysis, ["routing", "ipv6_present"], False) or
        _get(analysis, ["interfaces", "ipv6_enabled"], False) or
        protocols.get("ipv6_static_routing", False)
    )

    if vrf_present and not capabilities.get("supports_vrf", False):
        findings.append(make_finding(
            "routing",
            "critical",
            "VRF configuration present but unsupported by target profile",
            f"Current config includes {vrf_count} VRF definitions/usages, but target profile does not support VRF.",
            "Choose a VRF-capable platform or redesign segmentation."
        ))
    elif vrf_present and max_vrfs is not None and vrf_count > max_vrfs:
        findings.append(make_finding(
            "routing",
            "high",
            "VRF scale exceeds target platform profile",
            f"Current config indicates {vrf_count} VRF entries, above target limit of {max_vrfs}.",
            "Validate actual VRF count and select a platform with sufficient VRF scale."
        ))

    _add_headroom_finding(
        findings,
        "routing",
        "VRF usage is approaching target platform scale",
        vrf_count,
        max_vrfs,
        "Leave VRF headroom for growth, migration staging, and service segmentation changes."
    )

    if protocols.get("ospf") and not capabilities.get("supports_ospf", False):
        findings.append(make_finding(
            "routing",
            "critical",
            "OSPF required but unsupported by target profile",
            "Current device is configured for OSPF, but target profile does not support OSPF.",
            "Select a platform/software image that supports OSPF or redesign routing."
        ))

    if protocols.get("eigrp") and not capabilities.get("supports_eigrp", False):
        findings.append(make_finding(
            "routing",
            "critical",
            "EIGRP required but unsupported by target profile",
            "Current device is configured for EIGRP, but target profile does not support EIGRP.",
            "Confirm software support/licensing or migrate to another routing protocol."
        ))

    if protocols.get("bgp") and not capabilities.get("supports_bgp", False):
        findings.append(make_finding(
            "routing",
            "critical",
            "BGP required but unsupported by target profile",
            "Current device is configured for BGP, but target profile does not support BGP.",
            "Choose a BGP-capable platform or redesign WAN/edge routing."
        ))
    elif protocols.get("bgp") and max_bgp_neighbors is not None and bgp_neighbor_count > max_bgp_neighbors:
        findings.append(make_finding(
            "routing",
            "high",
            "BGP neighbor scale exceeds target platform profile",
            f"Current config shows {bgp_neighbor_count} BGP neighbors, above target limit of {max_bgp_neighbors}.",
            "Validate production neighbor counts and select a platform with sufficient BGP scale."
        ))

    _add_headroom_finding(
        findings,
        "routing",
        "BGP neighbor usage is approaching target platform scale",
        bgp_neighbor_count,
        max_bgp_neighbors,
        "Ensure headroom for neighbor growth, route-policy changes, and migration staging."
    )

    if protocols.get("static_routing") and max_static_routes is not None:
        if static_route_count > max_static_routes:
            over_ratio = static_route_count / max_static_routes if max_static_routes else 0
            severity = "high" if over_ratio >= 1.25 else "medium"
            findings.append(make_finding(
                "routing",
                severity,
                "Static route scale exceeds target profile",
                f"Current config has {static_route_count} static routes, above target limit of {max_static_routes}.",
                "Validate route scale and consider route summarization or platform resizing."
            ))

    _add_headroom_finding(
        findings,
        "routing",
        "Static route usage is approaching target platform scale",
        static_route_count,
        max_static_routes,
        "Review route growth, failover design, and opportunities for summarization."
    )

    if ipv6_present and not capabilities.get("supports_ipv6", False):
        findings.append(make_finding(
            "routing",
            "high",
            "IPv6 dependency present but unsupported by target profile",
            "Current configuration includes IPv6-related features, but target profile does not indicate IPv6 support.",
            "Select an IPv6-capable target or redesign IPv6 service requirements."
        ))

    # --------------------------------------------------
    # FHRP checks
    # --------------------------------------------------
    fhrp = _get(analysis, ["high_availability"], {})

    if fhrp.get("hsrp_present") and not capabilities.get("supports_hsrp", False):
        findings.append(make_finding(
            "high_availability",
            "high",
            "HSRP required but unsupported by target profile",
            "Current config uses HSRP, but target profile does not indicate HSRP support.",
            "Confirm first-hop redundancy support or migrate to a supported FHRP design."
        ))

    if fhrp.get("vrrp_present") and not capabilities.get("supports_vrrp", False):
        findings.append(make_finding(
            "high_availability",
            "high",
            "VRRP required but unsupported by target profile",
            "Current config uses VRRP, but target profile does not indicate VRRP support.",
            "Select a platform supporting VRRP or redesign HA."
        ))

    if fhrp.get("glbp_present") and not capabilities.get("supports_glbp", False):
        findings.append(make_finding(
            "high_availability",
            "high",
            "GLBP required but unsupported by target profile",
            "Current config uses GLBP, but target profile does not indicate GLBP support.",
            "Validate whether GLBP must be preserved or migrate to an alternative HA model."
        ))

    # --------------------------------------------------
    # Security / management checks
    # --------------------------------------------------
    security = _get(analysis, ["security"], {})
    mgmt = _get(analysis, ["management_plane"], {})

    if security.get("aaa_present") and not capabilities.get("supports_aaa", False):
        findings.append(make_finding(
            "security",
            "critical",
            "AAA dependency present but unsupported by target profile",
            "Current config uses AAA, but target profile does not indicate AAA support.",
            "Select a platform/software image that supports centralized authentication."
        ))

    if security.get("tacacs_present") and not capabilities.get("supports_tacacs", False):
        findings.append(make_finding(
            "security",
            "high",
            "TACACS+ dependency present but unsupported by target profile",
            "Current config uses TACACS+, but target profile does not indicate TACACS+ support.",
            "Validate TACACS+ compatibility for administrative access."
        ))

    if security.get("radius_present") and not capabilities.get("supports_radius", False):
        findings.append(make_finding(
            "security",
            "high",
            "RADIUS dependency present but unsupported by target profile",
            "Current config uses RADIUS, but target profile does not indicate RADIUS support.",
            "Validate RADIUS compatibility for access or management workflows."
        ))

    if security.get("ssh_present") and not capabilities.get("supports_ssh", False):
        findings.append(make_finding(
            "management_plane",
            "critical",
            "SSH required but unsupported by target profile",
            "Current management plane uses SSH, but target profile does not indicate SSH support.",
            "Do not proceed with a platform that cannot support secure remote management."
        ))

    if mgmt.get("management_access_class_present") and not capabilities.get("supports_management_acl", False):
        findings.append(make_finding(
            "management_plane",
            "medium",
            "Management ACL controls may not be supported",
            "Current config applies access-class controls to management lines, but target profile does not indicate support.",
            "Review remote access hardening requirements on the replacement platform."
        ))

    if security.get("telnet_enabled_on_lines"):
        findings.append(make_finding(
            "management_plane",
            "medium",
            "Legacy Telnet access detected in current configuration",
            "The current configuration appears to allow Telnet access.",
            "Use the hardware refresh as an opportunity to enforce SSH-only management."
        ))

    # --------------------------------------------------
    # Services checks
    # --------------------------------------------------
    services = _get(analysis, ["services"], {})

    if services.get("snmp_present") and not capabilities.get("supports_snmp", False):
        findings.append(make_finding(
            "services",
            "high",
            "SNMP monitoring dependency not supported by target profile",
            "Current config uses SNMP, but target profile does not indicate SNMP support.",
            "Confirm monitoring/telemetry method support before migration."
        ))

    if services.get("logging_present") and not capabilities.get("supports_syslog", False):
        findings.append(make_finding(
            "services",
            "medium",
            "Syslog/logging dependency not supported by target profile",
            "Current config sends logging configuration, but target profile does not indicate syslog support.",
            "Validate operational logging requirements and alternatives."
        ))

    if services.get("ntp_present") and not capabilities.get("supports_ntp", False):
        findings.append(make_finding(
            "services",
            "medium",
            "NTP dependency not supported by target profile",
            "Current config uses NTP, but target profile does not indicate NTP support.",
            "Validate time synchronization support before migration."
        ))

    if services.get("nat_present") and not capabilities.get("supports_nat", False):
        findings.append(make_finding(
            "services",
            "critical",
            "NAT required but unsupported by target profile",
            "Current config uses NAT, but target profile does not indicate NAT support.",
            "Select a NAT-capable platform or redesign address translation services."
        ))

    if services.get("dhcp_server_present") and not capabilities.get("supports_dhcp_server", False):
        findings.append(make_finding(
            "services",
            "medium",
            "DHCP server functionality present but unsupported by target profile",
            "Current config provides DHCP server-related configuration, but target profile does not indicate support.",
            "Validate whether DHCP services must remain on-box or be moved elsewhere."
        ))

    if services.get("ip_sla_present") and not capabilities.get("supports_ip_sla", False):
        findings.append(make_finding(
            "services",
            "medium",
            "IP SLA dependency present but unsupported by target profile",
            "Current config uses IP SLA, but target profile does not indicate support.",
            "Confirm failover and monitoring design on the replacement platform."
        ))

    if services.get("tracking_present") and not capabilities.get("supports_object_tracking", False):
        findings.append(make_finding(
            "services",
            "medium",
            "Object tracking present but unsupported by target profile",
            "Current config uses tracking objects, but target profile does not indicate support.",
            "Review failover/static route tracking dependencies before migration."
        ))

    if services.get("flow_monitoring_present") and not capabilities.get("supports_flow_monitoring", False):
        findings.append(make_finding(
            "services",
            "medium",
            "Flow monitoring present but unsupported by target profile",
            "Current config uses flow monitoring/export features, but target profile does not indicate support.",
            "Validate NetFlow/IPFIX/telemetry requirements."
        ))

    # --------------------------------------------------
    # QoS / policy checks
    # --------------------------------------------------
    policy = _get(analysis, ["policy"], {})

    if policy.get("qos_present") and not capabilities.get("supports_qos", False):
        findings.append(make_finding(
            "policy",
            "critical",
            "QoS policy required but unsupported by target profile",
            "Current config uses class-map/policy-map/service-policy constructs, but target profile does not indicate QoS support.",
            "Select a platform with sufficient QoS capabilities and validate policy model compatibility."
        ))

    # --------------------------------------------------
    # Crypto / VPN checks
    # --------------------------------------------------
    crypto = _get(analysis, ["crypto_vpn"], {})

    if crypto.get("crypto_present") and not capabilities.get("supports_crypto", False):
        findings.append(make_finding(
            "crypto_vpn",
            "critical",
            "Crypto/VPN dependency present but unsupported by target profile",
            "Current config includes crypto configuration, but target profile does not indicate crypto support.",
            "Choose a platform/software image that supports required VPN and encryption features."
        ))

    if crypto.get("isakmp_present") and not capabilities.get("supports_isakmp", False):
        findings.append(make_finding(
            "crypto_vpn",
            "high",
            "ISAKMP/IKEv1 usage present but unsupported by target profile",
            "Current config includes ISAKMP-related configuration, but target profile does not indicate support.",
            "Confirm whether IKEv1 must be preserved or migrated to IKEv2."
        ))

    if crypto.get("ikev2_present") and not capabilities.get("supports_ikev2", False):
        findings.append(make_finding(
            "crypto_vpn",
            "high",
            "IKEv2 required but unsupported by target profile",
            "Current config includes IKEv2 configuration, but target profile does not indicate support.",
            "Select a platform that supports the required VPN standards."
        ))

    if crypto.get("ipsec_present") and not capabilities.get("supports_ipsec", False):
        findings.append(make_finding(
            "crypto_vpn",
            "high",
            "IPsec required but unsupported by target profile",
            "Current config includes IPsec configuration, but target profile does not indicate support.",
            "Validate tunnel and encryption feature support before migration."
        ))

    if crypto.get("tunnel_interfaces_present") and not capabilities.get("supports_tunnel_interfaces", False):
        findings.append(make_finding(
            "crypto_vpn",
            "high",
            "Tunnel interfaces required but unsupported by target profile",
            "Current config includes tunnel interfaces, but target profile does not indicate tunnel support.",
            "Validate GRE/DMVPN/tunnel use cases and choose an appropriate platform."
        ))

    # --------------------------------------------------
    # Constraint-based / role-fit advisories
    # --------------------------------------------------
    intended_role = str(constraints.get("intended_role", "")).lower()

    if intended_role == "access" and current_l3 > 0:
        findings.append(make_finding(
            "design",
            "medium",
            "Target role is access but current device has Layer 3 dependencies",
            f"Target profile role is '{constraints.get('intended_role')}', but current config includes {current_l3} Layer 3 interfaces.",
            "Confirm whether routing should remain on this node or be moved upstream."
        ))

    if intended_role == "wan_edge":
        if current_l2_access > 24 or spanning_tree_present:
            findings.append(make_finding(
                "design",
                "medium",
                "Current configuration has significant switching behavior for a WAN-edge target",
                f"Current config includes {current_l2_access} access ports and/or spanning-tree features while target role is '{constraints.get('intended_role')}'.",
                "Validate whether campus/access switching functions should remain on this device or be offloaded."
            ))
        elif vlan_count > 0 and current_l2_access > 0:
            findings.append(make_finding(
                "design",
                "low",
                "WAN-edge target may not align with current switching use",
                f"Current config includes switching constructs (VLANs/access ports), while target role is '{constraints.get('intended_role')}'.",
                "Validate whether switching functions should be retained or offloaded."
            ))

    # --------------------------------------------------
    # Derived WAN-edge workload advisories
    # --------------------------------------------------
    wan_service_signals = sum([
        1 if protocols.get("bgp") else 0,
        1 if vrf_present else 0,
        1 if services.get("nat_present") else 0,
        1 if crypto.get("crypto_present") else 0,
        1 if crypto.get("tunnel_interfaces_present") else 0,
        1 if policy.get("qos_present") else 0,
    ])

    if intended_role == "wan_edge" and wan_service_signals >= 4:
        findings.append(make_finding(
            "design",
            "low",
            "Service-rich WAN-edge workload detected",
            "Current configuration combines multiple WAN-edge functions such as routing, segmentation, NAT, crypto, tunnels, or QoS.",
            "Validate throughput, encrypted throughput, routing scale, and license entitlements for the target platform."
        ))

    # Platform notes (descriptive metadata about the target) are surfaced as a
    # separate `platform_notes` field on the assessment, not as info-severity
    # findings — they aren't gap analysis and used to dilute the findings list.
    platform_notes = [str(note) for note in notes]

    # --------------------------------------------------
    # Final scoring
    # --------------------------------------------------
    total_score = sum(f["score"] for f in findings)

    if any(f["severity"] == "critical" for f in findings):
        overall = "NOT_RECOMMENDED"
    elif total_score >= 80:
        overall = "HIGH_RISK"
    elif total_score >= 35:
        overall = "CONDITIONAL_FIT"
    else:
        overall = "LIKELY_FIT"

    severity_counts = {
        "critical": sum(1 for f in findings if f["severity"] == "critical"),
        "high": sum(1 for f in findings if f["severity"] == "high"),
        "medium": sum(1 for f in findings if f["severity"] == "medium"),
        "low": sum(1 for f in findings if f["severity"] == "low"),
        "info": sum(1 for f in findings if f["severity"] == "info"),
    }

    result = {
        "target_platform": target_name,
        "assessment_summary": {
            "overall_recommendation": overall,
            "total_risk_score": total_score,
            "finding_counts": severity_counts,
            "current_hostname": _get(analysis, ["summary", "hostname"], "UNKNOWN")
        },
        "findings": findings,
        "platform_notes": platform_notes,
    }

    return result