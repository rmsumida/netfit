import json
import re
from collections import Counter
from ciscoconfparse import CiscoConfParse


def _unique_sorted(items):
    return sorted(set(items))


def _has(parse, pattern):
    return bool(parse.find_objects(pattern))


def _count(parse, pattern):
    return len(parse.find_objects(pattern))


def _find_lines(parse, pattern):
    return [obj.text.strip() for obj in parse.find_objects(pattern)]


def _classify_interface_name(name):
    n = name.lower()

    if n.startswith("gigabitethernet"):
        return "GigabitEthernet"
    if n.startswith("tengigabitethernet"):
        return "TenGigabitEthernet"
    if n.startswith("twentyfivegige"):
        return "TwentyFiveGigE"
    if n.startswith("fortygigabitethernet"):
        return "FortyGigabitEthernet"
    if n.startswith("hundredgige"):
        return "HundredGigE"
    if n.startswith("fastethernet"):
        return "FastEthernet"
    if n.startswith("ethernet"):
        return "Ethernet"
    if n.startswith("port-channel"):
        return "Port-channel"
    if n.startswith("vlan"):
        return "SVI"
    if n.startswith("loopback"):
        return "Loopback"
    if n.startswith("tunnel"):
        return "Tunnel"
    if n.startswith("serial"):
        return "Serial"
    if n.startswith("bdi"):
        return "BDI"
    if n.startswith("dialer"):
        return "Dialer"
    if n.startswith("mgmt") or n.startswith("management"):
        return "Management"
    return "Other"


def _extract_vrf_names(parse):
    vrf_names = []

    for line in _find_lines(parse, r"^ip vrf\s+"):
        m = re.match(r"^ip vrf\s+(\S+)", line, re.IGNORECASE)
        if m:
            vrf_names.append(m.group(1))

    for line in _find_lines(parse, r"^vrf definition\s+"):
        m = re.match(r"^vrf definition\s+(\S+)", line, re.IGNORECASE)
        if m:
            vrf_names.append(m.group(1))

    return _unique_sorted(vrf_names)


def _detect_interface_features(intf_obj):
    children = [c.text.strip() for c in intf_obj.children]

    features = {
        "shutdown": any(re.search(r"^shutdown$", c, re.IGNORECASE) for c in children),
        "switchport": any(re.search(r"^switchport$", c, re.IGNORECASE) for c in children),
        "no_switchport": any(re.search(r"^no switchport$", c, re.IGNORECASE) for c in children),
        "access_mode": any(re.search(r"^switchport mode access$", c, re.IGNORECASE) for c in children),
        "trunk_mode": any(re.search(r"^switchport mode trunk$", c, re.IGNORECASE) for c in children),
        "channel_group": any(re.search(r"^channel-group\s+\d+", c, re.IGNORECASE) for c in children),
        "ip_address": any(re.search(r"^ip address\s+", c, re.IGNORECASE) for c in children),
        "ip_unnumbered": any(re.search(r"^ip unnumbered\s+", c, re.IGNORECASE) for c in children),
        "ipv6_address": any(re.search(r"^ipv6 address\s+", c, re.IGNORECASE) for c in children),
        "ipv6_enable": any(re.search(r"^ipv6 enable$", c, re.IGNORECASE) for c in children),
        "vrf_forwarding": any(re.search(r"^(ip )?vrf forwarding\s+", c, re.IGNORECASE) for c in children),
        "subinterface_encapsulation": any(re.search(r"^encapsulation dot1[qQ]\s+", c, re.IGNORECASE) for c in children),
        "nat_inside": any(re.search(r"^ip nat inside$", c, re.IGNORECASE) for c in children),
        "nat_outside": any(re.search(r"^ip nat outside$", c, re.IGNORECASE) for c in children),
        "service_policy": any(re.search(r"^service-policy\s+", c, re.IGNORECASE) for c in children),
        "ospf": any(re.search(r"^ip ospf\s+", c, re.IGNORECASE) for c in children),
        "hsrp": any(re.search(r"^standby\s+", c, re.IGNORECASE) for c in children),
        "vrrp": any(re.search(r"^vrrp\s+", c, re.IGNORECASE) for c in children),
        "glbp": any(re.search(r"^glbp\s+", c, re.IGNORECASE) for c in children),
        "acl_in": any(re.search(r"^ip access-group\s+\S+\s+in$", c, re.IGNORECASE) for c in children),
        "acl_out": any(re.search(r"^ip access-group\s+\S+\s+out$", c, re.IGNORECASE) for c in children),
        "dhcp_helper": any(re.search(r"^ip helper-address\s+", c, re.IGNORECASE) for c in children),
        "description": any(re.search(r"^description\s+", c, re.IGNORECASE) for c in children),
    }

    return features


def _is_physical_interface(intf_type, intf_name):
    """
    Determine if an interface is a physical interface.
    
    Physical interfaces:
    - NOT a subinterface (name does not match \.\d+$)
    - NOT a logical type (Loopback, Tunnel, SVI, Port-channel)
    - IS one of the defined physical types (GigabitEthernet, TenGigabitEthernet, etc.)
    """
    # Check if subinterface
    if re.search(r"\.\d+$", intf_name):
        return False
    
    # Exclude logical types
    logical_types = {"Loopback", "Tunnel", "SVI", "Port-channel"}
    if intf_type in logical_types:
        return False
    
    # Include physical types
    physical_types = {
        "GigabitEthernet",
        "TenGigabitEthernet",
        "TwentyFiveGigE",
        "FortyGigabitEthernet",
        "HundredGigE",
        "FastEthernet",
        "Ethernet",
        "Serial",
        "Management"
    }
    
    return intf_type in physical_types


def _normalize_speed_class(intf_type):
    """
    Normalize interface type to speed class for Iteration 2 speed matching.
    Returns one of: '100M', '1G', '10G', '25G', '40G', '100G', 'UNKNOWN'
    """
    speed_map = {
        "FastEthernet": "100M",
        "GigabitEthernet": "1G",
        "TenGigabitEthernet": "10G",
        "TwentyFiveGigE": "25G",
        "FortyGigabitEthernet": "40G",
        "HundredGigE": "100G",
        "Ethernet": "UNKNOWN",  # Generic Ethernet type
        "Serial": "UNKNOWN",
        "Management": "UNKNOWN",
        "Other": "UNKNOWN"
    }
    return speed_map.get(intf_type, "UNKNOWN")


def _detect_interface_role_hints(intf_name, intf_type, features, is_subinterface):
    """
    Detect role hints for an interface. Returns a dict with role indicators.
    An interface may have multiple roles.
    """
    role_hints = {
        "management": False,
        "wan": False,
        "lan": False,
        "uplink": False,
        "port_channel_member": False
    }
    
    name_lower = intf_name.lower()
    
    # Management detection
    if intf_type == "Management" or "mgmt" in name_lower or "management" in name_lower:
        role_hints["management"] = True
    
    # Port-channel member detection
    if features.get("channel_group", False):
        role_hints["port_channel_member"] = True
    
    # WAN candidate detection
    wan_signals = False
    
    # Subinterface with encapsulation + routed behavior suggests WAN
    if is_subinterface and features.get("subinterface_encapsulation", False) and not features.get("switchport", False):
        wan_signals = True
    
    # Routed interface with IP suggests WAN
    if features.get("ip_address", False) and not features.get("switchport", False):
        wan_signals = True
    
    # VRF forwarding suggests WAN
    if features.get("vrf_forwarding", False):
        wan_signals = True
    
    # NAT outside suggests WAN
    if features.get("nat_outside", False):
        wan_signals = True
    
    # Tunnel always suggests WAN
    if intf_type == "Tunnel":
        wan_signals = True
    
    if wan_signals or "tunnel" in name_lower or "wan" in name_lower or "internet" in name_lower:
        role_hints["wan"] = True
    
    # LAN candidate detection
    lan_signals = False
    
    if features.get("access_mode", False):
        lan_signals = True
    
    if features.get("trunk_mode", False):
        lan_signals = True
    
    if intf_type == "SVI":
        lan_signals = True
    
    if features.get("nat_inside", False):
        lan_signals = True
    
    if features.get("dhcp_helper", False):
        lan_signals = True
    
    if features.get("hsrp", False) or features.get("vrrp", False) or features.get("glbp", False):
        lan_signals = True
    
    if lan_signals or "vlan" in name_lower or "access" in name_lower or "distribution" in name_lower:
        role_hints["lan"] = True
    
    # Uplink candidate detection (high-speed + trunk/routed/port-channel)
    high_speed_types = {"TenGigabitEthernet", "TwentyFiveGigE", "FortyGigabitEthernet", "HundredGigE"}
    if intf_type in high_speed_types:
        if features.get("trunk_mode", False) or features.get("channel_group", False) or (
            features.get("ip_address", False) and not features.get("switchport", False)
        ):
            role_hints["uplink"] = True
    
    return role_hints


def analyze_config(file_path):
    parse = CiscoConfParse(file_path, syntax="ios")

    report = {
        "summary": {},
        "inventory": {},
        "interfaces": {
            "total": 0,
            "active_total": 0,
            "by_type": {},
            "active_physical_count": 0,
            "active_physical_by_type": {},
            "active_physical_by_speed_class": {},
            "active_physical_by_role": {},
            "details": [],
            "layer2_access_count": 0,
            "layer2_trunk_count": 0,
            "layer3_count": 0,
            "port_channel_members": 0,
            "port_channels": 0,
            "subinterfaces": 0,
            "active_subinterfaces": 0,
            "tunnels": 0,
            "active_tunnels": 0,
            "loopbacks": 0,
            "active_loopbacks": 0,
            "active_svis": 0,
            "active_port_channels": 0,
            "active_management_interfaces": 0,
            "active_wan_physical_count": 0,
            "active_lan_physical_count": 0,
            "active_uplink_physical_count": 0,
            "active_port_channel_member_count": 0,
            "shutdown_count": 0,
            "ipv6_enabled": False
        },
        "switching": {},
        "routing": {},
        "high_availability": {},
        "security": {},
        "services": {},
        "policy": {},
        "management_plane": {},
        "crypto_vpn": {},
        "refresh_risks": [],
        "migration_considerations": []
    }

    hostname_objs = parse.find_objects(r"^hostname\s+")
    hostname = hostname_objs[0].text.split()[1] if hostname_objs else "UNKNOWN"

    report["inventory"]["hostname"] = hostname

    # -------------------------
    # Interface analysis
    # -------------------------
    intf_objs = parse.find_objects(r"^interface\s+")
    interface_type_counter = Counter()
    interfaces_detail = []

    layer2_access_count = 0
    layer2_trunk_count = 0
    layer3_count = 0
    port_channel_members = 0
    subinterfaces = 0
    tunnels = 0
    loopbacks = 0
    shutdown_count = 0
    ipv6_enabled_on_interfaces = False
    
    # Iteration 1 active interface counters
    active_total = 0
    active_physical_count = 0
    active_physical_by_type = Counter()
    active_subinterfaces = 0
    active_tunnels = 0
    active_loopbacks = 0
    active_svis = 0
    active_port_channels = 0
    
    # Iteration 2 active interface counters
    active_physical_by_speed_class = Counter()
    active_physical_by_role = Counter()  # Count by primary role
    active_management_interfaces = 0
    active_wan_physical_count = 0
    active_lan_physical_count = 0
    active_uplink_physical_count = 0
    active_port_channel_member_count = 0

    for intf in intf_objs:
        intf_name = intf.text.split(None, 1)[1]
        intf_type = _classify_interface_name(intf_name)
        interface_type_counter[intf_type] += 1

        is_subinterface = bool(re.search(r"\.\d+$", intf_name))
        if is_subinterface:
            subinterfaces += 1

        if intf_type == "Tunnel":
            tunnels += 1

        if intf_type == "Loopback":
            loopbacks += 1

        features = _detect_interface_features(intf)

        if features["access_mode"]:
            layer2_access_count += 1

        if features["trunk_mode"]:
            layer2_trunk_count += 1

        is_l3 = any([
            features["no_switchport"],
            features["ip_address"],
            features["ip_unnumbered"],
            features["ipv6_address"],
            features["ipv6_enable"],
            features["vrf_forwarding"],
            intf_type == "Tunnel",
            is_subinterface and not features["switchport"],
        ])

        if is_l3:
            layer3_count += 1

        if features["channel_group"]:
            port_channel_members += 1

        if features["shutdown"]:
            shutdown_count += 1
        else:
            # Interface is ACTIVE (not shutdown)
            active_total += 1
            
            # Track active physical interfaces
            is_physical = _is_physical_interface(intf_type, intf_name)
            if is_physical:
                active_physical_count += 1
                active_physical_by_type[intf_type] += 1
                
                # Iteration 2: Track speed class and roles for active physical interfaces
                speed_class = _normalize_speed_class(intf_type)
                active_physical_by_speed_class[speed_class] += 1
                
                # Detect and track active physical roles
                role_hints = _detect_interface_role_hints(intf_name, intf_type, features, is_subinterface)
                if role_hints["management"]:
                    active_management_interfaces += 1
                    active_physical_by_role["management"] += 1
                elif role_hints["uplink"]:
                    active_uplink_physical_count += 1
                    active_physical_by_role["uplink"] += 1
                elif role_hints["wan"]:
                    active_wan_physical_count += 1
                    active_physical_by_role["wan"] += 1
                elif role_hints["lan"]:
                    active_lan_physical_count += 1
                    active_physical_by_role["lan"] += 1
                else:
                    active_physical_by_role["unclassified"] += 1
                
                if role_hints["port_channel_member"]:
                    active_port_channel_member_count += 1
            
            # Track active logical interface types
            if is_subinterface:
                active_subinterfaces += 1
            elif intf_type == "Tunnel":
                active_tunnels += 1
            elif intf_type == "Loopback":
                active_loopbacks += 1
            elif intf_type == "SVI":
                active_svis += 1
            elif intf_type == "Port-channel":
                active_port_channels += 1

        if features["ipv6_address"] or features["ipv6_enable"]:
            ipv6_enabled_on_interfaces = True

        # Compute role hints and speed class for detail object
        is_physical = _is_physical_interface(intf_type, intf_name)
        is_active = not features["shutdown"]
        role_hints = _detect_interface_role_hints(intf_name, intf_type, features, is_subinterface)
        speed_class = _normalize_speed_class(intf_type) if is_physical else None
        
        interfaces_detail.append({
            "name": intf_name,
            "type": intf_type,
            "is_subinterface": is_subinterface,
            "is_layer3": is_l3,
            "is_active": is_active,
            "is_physical": is_physical,
            "effective_speed_class": speed_class,
            "role_hints": role_hints,
            "features": features
        })

    report["interfaces"]["total"] = len(intf_objs)
    report["interfaces"]["active_total"] = active_total
    report["interfaces"]["by_type"] = dict(interface_type_counter)
    report["interfaces"]["active_physical_count"] = active_physical_count
    report["interfaces"]["active_physical_by_type"] = dict(active_physical_by_type)
    report["interfaces"]["active_physical_by_speed_class"] = dict(active_physical_by_speed_class)
    report["interfaces"]["active_physical_by_role"] = dict(active_physical_by_role)
    report["interfaces"]["details"] = interfaces_detail
    report["interfaces"]["layer2_access_count"] = layer2_access_count
    report["interfaces"]["layer2_trunk_count"] = layer2_trunk_count
    report["interfaces"]["layer3_count"] = layer3_count
    report["interfaces"]["port_channel_members"] = port_channel_members
    report["interfaces"]["port_channels"] = _count(parse, r"^interface\s+Port-channel")
    report["interfaces"]["subinterfaces"] = subinterfaces
    report["interfaces"]["active_subinterfaces"] = active_subinterfaces
    report["interfaces"]["tunnels"] = tunnels
    report["interfaces"]["active_tunnels"] = active_tunnels
    report["interfaces"]["loopbacks"] = loopbacks
    report["interfaces"]["active_loopbacks"] = active_loopbacks
    report["interfaces"]["active_svis"] = active_svis
    report["interfaces"]["active_port_channels"] = active_port_channels
    report["interfaces"]["active_management_interfaces"] = active_management_interfaces
    report["interfaces"]["active_wan_physical_count"] = active_wan_physical_count
    report["interfaces"]["active_lan_physical_count"] = active_lan_physical_count
    report["interfaces"]["active_uplink_physical_count"] = active_uplink_physical_count
    report["interfaces"]["active_port_channel_member_count"] = active_port_channel_member_count
    report["interfaces"]["shutdown_count"] = shutdown_count
    report["interfaces"]["ipv6_enabled"] = ipv6_enabled_on_interfaces

    # -------------------------
    # Switching / VLAN / STP
    # -------------------------
    vlan_objs = parse.find_objects(r"^vlan\s+\d+")
    vlan_ids = []
    for obj in vlan_objs:
        parts = obj.text.split()
        if len(parts) >= 2:
            vlan_ids.append(parts[1])

    report["switching"] = {
        "vlans_defined_count": len(vlan_ids),
        "vlans_defined": _unique_sorted(vlan_ids),
        "trunking_present": layer2_trunk_count > 0,
        "access_ports_present": layer2_access_count > 0,
        "etherchannel_present": _has(parse, r"^\s*channel-group\s+") or _count(parse, r"^interface\s+Port-channel") > 0,
        "spanning_tree": {
            # `spanning-tree extend system-id` is a universal IOS default that
            # appears on routers which never actually run STP. Only count STP
            # as "present" when there's an explicit mode, per-VLAN config,
            # logging enable, or interface-level portfast/bpduguard usage.
            "present": (
                _has(parse, r"^spanning-tree mode\s+")
                or _has(parse, r"^spanning-tree vlan\s+")
                or _has(parse, r"^spanning-tree logging\s+")
                or _has(parse, r"^\s*spanning-tree portfast")
                or _has(parse, r"^\s*spanning-tree bpduguard")
            ),
            "mode_lines": _find_lines(parse, r"^spanning-tree mode\s+"),
            "portfast": _has(parse, r"^\s*spanning-tree portfast"),
            "bpduguard": _has(parse, r"^\s*spanning-tree bpduguard") or _has(parse, r"^spanning-tree portfast bpduguard")
        }
    }

    # -------------------------
    # VRF / Routing analysis
    # -------------------------
    vrf_names = _extract_vrf_names(parse)

    router_ospf = _find_lines(parse, r"^router ospf\s+")
    router_eigrp = _find_lines(parse, r"^router eigrp\s+")
    router_bgp = _find_lines(parse, r"^router bgp\s+")
    router_ospfv3 = _find_lines(parse, r"^router ospfv3\s+")
    ipv6_router_ospf = _find_lines(parse, r"^ipv6 router ospf\s+")

    static_routes = _find_lines(parse, r"^ip route\s+")
    ipv6_routes = _find_lines(parse, r"^ipv6 route\s+")

    ospf_interface_usage = _find_lines(parse, r"^\s*ip ospf\s+")
    ospf_networks = _find_lines(parse, r"^\s*network\s+\S+\s+\S+\s+area\s+\S+")
    passive_intf = _find_lines(parse, r"^\s*passive-interface\s+")

    bgp_neighbor_lines = _find_lines(parse, r"^\s*neighbor\s+\S+\s+remote-as\s+")
    bgp_neighbor_ids = []
    for line in bgp_neighbor_lines:
        m = re.match(r"^\s*neighbor\s+(\S+)\s+remote-as\s+", line, re.IGNORECASE)
        if m:
            bgp_neighbor_ids.append(m.group(1))

    ipv6_interface_lines = _find_lines(parse, r"^\s*ipv6 address\s+")
    ipv6_enable_lines = _find_lines(parse, r"^\s*ipv6 enable$")
    bgp_ipv6_af = _find_lines(parse, r"^\s*address-family ipv6\s+")

    ipv6_present = bool(
        ipv6_routes or
        ipv6_interface_lines or
        ipv6_enable_lines or
        router_ospfv3 or
        ipv6_router_ospf or
        bgp_ipv6_af or
        ipv6_enabled_on_interfaces
    )

    report["routing"] = {
        "vrf_present": bool(vrf_names),
        "vrfs": vrf_names,
        "ipv6_present": ipv6_present,
        "protocols": {
            "ospf": bool(router_ospf),
            "ospfv3": bool(router_ospfv3 or ipv6_router_ospf),
            "eigrp": bool(router_eigrp),
            "bgp": bool(router_bgp),
            "static_routing": bool(static_routes),
            "ipv6_static_routing": bool(ipv6_routes)
        },
        "ospf": {
            "processes": router_ospf,
            "ospfv3_processes": router_ospfv3,
            "ipv6_router_ospf_processes": ipv6_router_ospf,
            "interface_level_config_present": bool(ospf_interface_usage),
            "network_statements_present": bool(ospf_networks),
            "passive_interfaces_present": bool(passive_intf)
        },
        "eigrp": {
            "processes": router_eigrp
        },
        "bgp": {
            "processes": router_bgp,
            "neighbor_count": len(set(bgp_neighbor_ids)),
            "neighbors": sorted(set(bgp_neighbor_ids)),
            "ipv6_address_family_present": bool(bgp_ipv6_af)
        },
        "static_route_count": len(static_routes),
        "ipv6_static_route_count": len(ipv6_routes)
    }

    # -------------------------
    # High availability / FHRP
    # -------------------------
    hsrp = _find_lines(parse, r"^\s*standby\s+")
    vrrp = _find_lines(parse, r"^\s*vrrp\s+")
    glbp = _find_lines(parse, r"^\s*glbp\s+")

    report["high_availability"] = {
        "fhrp_present": bool(hsrp or vrrp or glbp),
        "hsrp_present": bool(hsrp),
        "vrrp_present": bool(vrrp),
        "glbp_present": bool(glbp),
        "hsrp_line_count": len(hsrp),
        "vrrp_line_count": len(vrrp),
        "glbp_line_count": len(glbp)
    }

    # -------------------------
    # Security / AAA / Access
    # -------------------------
    acl_named = _find_lines(parse, r"^ip access-list\s+")
    acl_numbered = _find_lines(parse, r"^access-list\s+")
    prefix_lists = _find_lines(parse, r"^ip prefix-list\s+")
    route_maps = _find_lines(parse, r"^route-map\s+")
    aaa_lines = _find_lines(parse, r"^aaa\s+")
    usernames = _find_lines(parse, r"^username\s+")
    tacacs = _find_lines(parse, r"^tacacs") + _find_lines(parse, r"^aaa group server tacacs")
    radius = _find_lines(parse, r"^radius") + _find_lines(parse, r"^aaa group server radius")
    ssh = _find_lines(parse, r"^ip ssh\s+")
    telnet_transport = _find_lines(parse, r"^\s*transport input\s+.*telnet")
    http_server = _find_lines(parse, r"^ip http server")
    https_server = _find_lines(parse, r"^ip http secure-server")

    report["security"] = {
        "aaa_present": bool(aaa_lines),
        "local_usernames_present": bool(usernames),
        "tacacs_present": bool(tacacs),
        "radius_present": bool(radius),
        "acl_present": bool(acl_named or acl_numbered),
        "named_acl_count": len(acl_named),
        "numbered_acl_count": len(acl_numbered),
        "prefix_list_count": len(prefix_lists),
        "route_map_count": len(route_maps),
        "ssh_present": bool(ssh),
        "telnet_enabled_on_lines": bool(telnet_transport),
        "http_server_enabled": bool(http_server),
        "https_server_enabled": bool(https_server)
    }

    # -------------------------
    # Services
    # -------------------------
    snmp = _find_lines(parse, r"^snmp-server\s+")
    logging_lines = _find_lines(parse, r"^logging\s+")
    ntp_lines = _find_lines(parse, r"^ntp\s+")
    nat_lines = _find_lines(parse, r"^ip nat\s+")
    dhcp_pool = _find_lines(parse, r"^ip dhcp pool\s+")
    dhcp_excluded = _find_lines(parse, r"^ip dhcp excluded-address\s+")
    ip_sla = _find_lines(parse, r"^ip sla\s+")
    track = _find_lines(parse, r"^track\s+")
    flow_export = _find_lines(parse, r"^(ip flow-export|flow exporter|sampler|monitor session|flow monitor)\s+")

    report["services"] = {
        "snmp_present": bool(snmp),
        "logging_present": bool(logging_lines),
        "ntp_present": bool(ntp_lines),
        "nat_present": bool(nat_lines),
        "dhcp_server_present": bool(dhcp_pool or dhcp_excluded),
        "ip_sla_present": bool(ip_sla),
        "tracking_present": bool(track),
        "flow_monitoring_present": bool(flow_export),
        "snmp_line_count": len(snmp),
        "logging_line_count": len(logging_lines),
        "ntp_line_count": len(ntp_lines),
        "nat_line_count": len(nat_lines)
    }

    # -------------------------
    # Policy / QoS
    # -------------------------
    class_maps = _find_lines(parse, r"^class-map\s+")
    policy_maps = _find_lines(parse, r"^policy-map\s+")
    service_policies = _find_lines(parse, r"^\s*service-policy\s+")

    report["policy"] = {
        "qos_present": bool(class_maps or policy_maps or service_policies),
        "class_map_count": len(class_maps),
        "policy_map_count": len(policy_maps),
        "service_policy_count": len(service_policies),
        "acl_count_total": len(acl_named) + len(acl_numbered),
        "prefix_list_count": len(prefix_lists),
        "route_map_count": len(route_maps)
    }

    # -------------------------
    # Management plane
    # -------------------------
    line_vty = parse.find_objects(r"^line vty ")
    line_con = parse.find_objects(r"^line con ")
    login_methods = []

    for line_obj in line_vty + line_con:
        for child in line_obj.children:
            txt = child.text.strip()
            if re.search(r"^(login|transport input|exec-timeout|access-class)\s+", txt, re.IGNORECASE):
                login_methods.append(txt)

    domain_name = _find_lines(parse, r"^ip domain-name\s+")
    name_servers = _find_lines(parse, r"^ip name-server\s+")
    mgmt_acls = _find_lines(parse, r"^\s*access-class\s+")

    report["management_plane"] = {
        "vty_present": bool(line_vty),
        "console_present": bool(line_con),
        "domain_name_present": bool(domain_name),
        "dns_servers_present": bool(name_servers),
        "management_access_class_present": bool(mgmt_acls),
        "line_controls": _unique_sorted(login_methods)
    }

    # -------------------------
    # Crypto / VPN
    # -------------------------
    crypto_lines = _find_lines(parse, r"^crypto\s+")
    isakmp = _find_lines(parse, r"^crypto isakmp\s+")
    ikev2 = _find_lines(parse, r"^crypto ikev2\s+")
    ipsec = _find_lines(parse, r"^crypto ipsec\s+")
    tunnel_intfs_present = any(d["type"] == "Tunnel" for d in interfaces_detail)

    report["crypto_vpn"] = {
        "crypto_present": bool(crypto_lines),
        "isakmp_present": bool(isakmp),
        "ikev2_present": bool(ikev2),
        "ipsec_present": bool(ipsec),
        "tunnel_interfaces_present": tunnel_intfs_present,
        "crypto_line_count": len(crypto_lines)
    }

    # -------------------------
    # Refresh risk logic
    # -------------------------
    risks = []
    considerations = []

    if interface_type_counter.get("FastEthernet", 0) > 0:
        risks.append("Legacy FastEthernet interfaces detected; verify replacement platform media/speed compatibility.")

    if interface_type_counter.get("Serial", 0) > 0:
        risks.append("Serial interfaces detected; replacement platforms may not support WAN serial modules natively.")

    if report["interfaces"]["subinterfaces"] > 0:
        considerations.append("802.1Q subinterfaces are in use; confirm routed port/subinterface support on target platform.")

    if report["interfaces"]["port_channels"] > 0 or report["interfaces"]["port_channel_members"] > 0:
        considerations.append("EtherChannel/LACP-style design detected; confirm aggregation support and member scale.")

    if report["switching"]["trunking_present"]:
        considerations.append("802.1Q trunking is in use; confirm trunk behavior, native VLAN handling, and allowed VLAN scale.")

    if report["switching"]["vlans_defined_count"] > 0:
        considerations.append("VLAN definitions present; verify VLAN scale and Layer 2 feature parity on target hardware.")

    if report["routing"]["vrf_present"]:
        risks.append("VRF configuration detected; confirm VRF/VRF-Lite support, scale, and syntax compatibility.")

    if report["routing"]["protocols"]["eigrp"]:
        risks.append("EIGRP detected; confirm target platform and software support/licensing if EIGRP must be retained.")

    if report["routing"]["protocols"]["bgp"]:
        considerations.append("BGP is configured; review neighbor scale, policy complexity, and license requirements.")

    if report["routing"]["protocols"]["ospf"] or report["routing"]["protocols"]["ospfv3"]:
        considerations.append("OSPF is configured; confirm OSPF feature support, area design, and interface-based settings.")

    if report["routing"]["ipv6_present"]:
        considerations.append("IPv6 configuration is present; confirm IPv6 routing, management, and policy feature parity.")

    if report["high_availability"]["fhrp_present"]:
        considerations.append("FHRP detected (HSRP/VRRP/GLBP); validate feature support and migration sequencing.")

    if report["services"]["nat_present"]:
        risks.append("NAT is configured; verify NAT scale, throughput, and syntax/feature parity on target platform.")

    if report["policy"]["qos_present"]:
        risks.append("QoS policies detected; hardware refresh must validate QoS model, class/policy limits, and syntax differences.")

    if report["security"]["tacacs_present"] or report["security"]["radius_present"]:
        considerations.append("Centralized AAA is in use; confirm management plane reachability and authentication method compatibility.")

    if report["security"]["telnet_enabled_on_lines"]:
        risks.append("Telnet access appears enabled on line configuration; review secure management requirements during refresh.")

    if report["security"]["http_server_enabled"] and not report["security"]["https_server_enabled"]:
        risks.append("HTTP server enabled without secure-server; review management plane hardening on replacement platform.")

    if report["crypto_vpn"]["crypto_present"] or report["crypto_vpn"]["tunnel_interfaces_present"]:
        risks.append("VPN/crypto features detected; confirm crypto acceleration, tunnel scale, and software support.")

    if report["services"]["flow_monitoring_present"]:
        considerations.append("Flow monitoring/export features detected; verify telemetry/NetFlow/IPFIX support on target device.")

    if report["services"]["ip_sla_present"] or report["services"]["tracking_present"]:
        considerations.append("IP SLA/object tracking detected; review HA/failover logic dependencies.")

    if report["management_plane"]["management_access_class_present"]:
        considerations.append("Management ACLs are applied; preserve remote access controls during migration.")

    if report["interfaces"]["layer3_count"] > 0 and report["interfaces"]["layer2_trunk_count"] > 0:
        considerations.append("Mixed Layer 2/Layer 3 role detected; verify whether target device is intended to preserve both roles.")

    if report["interfaces"]["tunnels"] > 0:
        risks.append("Tunnel interfaces detected; validate GRE/IPsec/tunnel feature support and performance.")

    if report["interfaces"]["details"]:
        uplink_like = sum(
            1 for d in report["interfaces"]["details"]
            if d["type"] in ["TenGigabitEthernet", "FortyGigabitEthernet", "HundredGigE", "TwentyFiveGigE"]
        )
        if uplink_like > 0:
            considerations.append("High-speed uplink interfaces detected; verify transceiver/media compatibility on replacement hardware.")

    report["refresh_risks"] = _unique_sorted(risks)
    report["migration_considerations"] = _unique_sorted(considerations)

    # -------------------------
    # Summary
    # -------------------------
    enabled_protocols = []
    for proto, enabled in report["routing"]["protocols"].items():
        if enabled:
            enabled_protocols.append(proto)

    report["summary"] = {
        "hostname": hostname,
        "interface_count": report["interfaces"]["total"],
        "interface_types": dict(interface_type_counter),
        "routing_protocols_enabled": enabled_protocols,
        "vlans_defined_count": report["switching"]["vlans_defined_count"],
        "vrf_present": report["routing"]["vrf_present"],
        "ipv6_present": report["routing"]["ipv6_present"],
        "fhrp_present": report["high_availability"]["fhrp_present"],
        "qos_present": report["policy"]["qos_present"],
        "nat_present": report["services"]["nat_present"],
        "crypto_present": report["crypto_vpn"]["crypto_present"],
        "aaa_present": report["security"]["aaa_present"],
        "refresh_risk_count": len(report["refresh_risks"]),
        "migration_consideration_count": len(report["migration_considerations"])
    }

    return report


def save_report(report, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)