"""
Cisco Switch Port Dashboard
Shows:
- All ports
- Status
- CDP Device
- Device Type
- VLAN
- Speed
- Fiber TX/RX Power
"""

import os
import re

from dotenv import load_dotenv
from netmiko import ConnectHandler

# Use the same credentials as the web app.
load_dotenv()
SWITCH_USERNAME = os.getenv("SWITCH_USERNAME")
SWITCH_PASSWORD = os.getenv("SWITCH_PASSWORD")

if not SWITCH_USERNAME or not SWITCH_PASSWORD:
    raise ValueError("Missing SWITCH_USERNAME or SWITCH_PASSWORD in .env")


def short_to_long(p):
    return p.replace("Fa", "FastEthernet").replace("Gi", "GigabitEthernet").replace("Te", "TenGigabitEthernet")


def draw_line(width=118, char="="):
    return char * width


def truncate(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def format_table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, value in enumerate(row):
            widths[i] = max(widths[i], len(str(value)))

    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header = "| " + " | ".join(f"{headers[i]:<{widths[i]}}" for i in range(len(headers))) + " |"

    lines = [border, header, border]
    for row in rows:
        line = "| " + " | ".join(f"{str(row[i]):<{widths[i]}}" for i in range(len(row))) + " |"
        lines.append(line)
    lines.append(border)
    return "\n".join(lines)


def device_suffix(s):
    for k in ("MikroTik", "LiteBeam", "Rocket", "Cisco"):
        if k in s:
            return k
    return "Unknown"


def sortkey(r):
    m = re.match(r"([A-Za-z]+)(\d+)/(\d+)", r[0])
    if not m:
        return (9, 999, 999)
    t = {"Fa": 0, "Gi": 1, "Te": 2, "Fo": 3}.get(m.group(1), 9)
    return (t, int(m.group(2)), int(m.group(3)))


def normalize_port(port):
    """Convert long and short Cisco interface names to the same form."""
    port = (port or "").replace(" ", "")
    aliases = (("FastEthernet", "Fa"), ("GigabitEthernet", "Gi"),
               ("TenGigabitEthernet", "Te"), ("FortyGigabitEthernet", "Fo"))
    for long_name, short_name in aliases:
        if port.lower().startswith(long_name.lower()):
            return short_name + port[len(long_name):]
    return port


def parse_descriptions(config_output):
    """Return interface descriptions from one running-config command."""
    descriptions = {}
    current_port = None
    for line in config_output.splitlines():
        interface_match = re.match(r"^interface\s+(\S+)", line, re.IGNORECASE)
        if interface_match:
            current_port = normalize_port(interface_match.group(1))
            continue
        description_match = re.match(r"^\s+description\s+(.+)$", line, re.IGNORECASE)
        if current_port and description_match:
            descriptions[current_port] = description_match.group(1).strip()
    return descriptions


def parse_cdp_details(cdp_output):
    """Return relevant CDP devices keyed by their local interface."""
    neighbors = {}
    for block in re.split(r"-{10,}", cdp_output):
        device_match = re.search(r"^Device ID:\s*(.+)$", block, re.MULTILINE | re.IGNORECASE)
        platform_match = re.search(r"^Platform:\s*([^,]+)", block, re.MULTILINE | re.IGNORECASE)
        interface_match = re.search(r"^Interface:\s*([^,]+),\s*Port ID.*?:\s*(.+)$", block, re.MULTILINE | re.IGNORECASE)
        if not (device_match and interface_match):
            continue

        remote_port = interface_match.group(2).lower()
        if "bridge1/ether1" not in remote_port and "br0" not in remote_port:
            continue

        platform = platform_match.group(1).strip() if platform_match else "Unknown"
        neighbors[normalize_port(interface_match.group(1))] = (device_match.group(1).strip(), device_suffix(platform))
    return neighbors


def parse_interface_status(status_output):
    """Parse Cisco fixed-width interface-status table, including VLAN."""
    lines = status_output.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        if re.match(r"^Port\s+Name\s+Status\s+Vlan\s+Duplex\s+Speed\s+Type", line, re.IGNORECASE):
            header_index = index
            break
    if header_index is None:
        return []

    header = lines[header_index]
    columns = [(name, header.find(name)) for name in ("Port", "Name", "Status", "Vlan", "Duplex", "Speed", "Type")]
    ports = []
    for line in lines[header_index + 1:]:
        if not line.strip() or line.startswith("--"):
            continue
        values = {}
        for index, (name, start) in enumerate(columns):
            end = columns[index + 1][1] if index + 1 < len(columns) else len(line)
            values[name] = line[start:end].strip()
        if not values["Port"]:
            continue
        ports.append({
            "port": values["Port"],
            "status": values["Status"] or "Unknown",
            "vlan": values["Vlan"] or "-",
            "speed": values["Speed"] or "-",
        })
    return ports


ip = input("Cisco IP: ").strip()

dev = {
    "device_type": "cisco_ios_telnet",
    "host": ip,
    "username": SWITCH_USERNAME,
    "password": SWITCH_PASSWORD,
    "fast_cli": False,
}

nc = ConnectHandler(**dev)

# Cisco switch identity
hostname_output = nc.send_command("show running-config | include hostname")
hostname_match = re.search(r"^hostname\s+(\S+)", hostname_output, re.MULTILINE | re.IGNORECASE)
hostname = hostname_match.group(1) if hostname_match else "Unknown"

version_output = nc.send_command("show version")
model_match = re.search(r"\b(WS-[A-Za-z0-9-]+)\b", version_output)
model = model_match.group(1) if model_match else "Unknown"

uptime_match = re.search(r"\b(uptime\s+is\s+.+)$", version_output, re.MULTILINE | re.IGNORECASE)
uptime = uptime_match.group(1).strip() if uptime_match else "Unknown"

print(draw_line(72))
print("Cisco Switch Port Dashboard")
print(draw_line(72, "-"))
print(f"Hostname : {hostname}")
print(f"Model    : {model}")
print(f"Uptime   : {uptime}")
print(draw_line(72))

fiber_power = {}
fiber_ports = set()
try:
    out = nc.send_command("show interfaces transceiver")
    for line in out.splitlines():
        # Any interface listed by this command has a transceiver installed.
        port_match = re.match(r"^((?:Gi|Te|Fo)\S+)", line.strip())
        if port_match:
            fiber_ports.add(port_match.group(1))

        power_match = re.match(r"^(?:Gi|Te|Fo)\S+\s+\S+\s+\S+\s+([-\d\.]+)\s+([-\d\.]+)", line.strip())
        if power_match and port_match:
            fiber_power[port_match.group(1)] = {"tx": power_match.group(1), "rx": power_match.group(2)}
except Exception:
    pass

out = nc.send_command("show interfaces status")
ports = parse_interface_status(out)

# Fetch all descriptions and CDP neighbors once to avoid per-port command bursts.
config_output = nc.send_command("show running-config | include ^interface|^ description")
descriptions = parse_descriptions(config_output)
cdp_output = nc.send_command("show cdp neighbors detail")
cdp_neighbors = parse_cdp_details(cdp_output)

results = []
for p in ports:
    port, status, vlan, speed = p["port"], p["status"], p["vlan"], p["speed"]
    devname = "-"
    dtype = "-"
    if status == "connected":
        try:
            normalized_port = normalize_port(port)
            description = descriptions.get(normalized_port, "")

            # Always prefer live neighbor information when available.
            neighbor_name, neighbor_type = cdp_neighbors.get(normalized_port, ("", ""))
            if neighbor_name:
                devname, dtype = neighbor_name, neighbor_type
            elif description:
                devname = description
                dtype = "Fiber" if port in fiber_ports else "Description"
        except Exception:
            pass
    tx = fiber_power.get(port, {}).get("tx", "-")
    rx = fiber_power.get(port, {}).get("rx", "-")
    results.append((port, status, devname, dtype, vlan, speed, tx, rx))

results.sort(key=sortkey)

total_ports = len(results)
connected_ports = sum(1 for r in results if r[1].lower() == "connected")
fiber_detected = sum(1 for r in results if r[0] in fiber_ports)

print("\nPort Summary")
print(draw_line(72, "-"))
print(f"Total Ports     : {total_ports}")
print(f"Connected Ports : {connected_ports}")
print(f"Fiber Ports     : {fiber_detected}")
print(draw_line(72, "-"))

headers = ["Port", "Status", "Neighbor / Description", "Type", "VLAN", "Speed", "TX(dBm)", "RX(dBm)"]
table_rows = []
for r in results:
    table_rows.append((
        r[0],
        r[1],
        truncate(r[2], 36),
        r[3],
        r[4],
        r[5],
        r[6],
        r[7],
    ))

print("\nPort Details")
print(format_table(headers, table_rows))
nc.disconnect()
