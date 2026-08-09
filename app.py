from flask import Flask, render_template, request, redirect, session, url_for
import re
import os
from dotenv import dotenv_values, load_dotenv, set_key

# Load environment variables from .env file
load_dotenv()

ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'monitor-master-change-password')

# Get credentials from environment variables
username = os.getenv('SWITCH_USERNAME')
password = os.getenv('SWITCH_PASSWORD')


def reload_switch_credentials():
    global username, password
    load_dotenv(ENV_FILE, override=True)
    username = os.getenv('SWITCH_USERNAME')
    password = os.getenv('SWITCH_PASSWORD')


def get_admin_password():
    env_values = dotenv_values(ENV_FILE)
    admin_password = env_values.get('ADMIN_PASSWORD')
    return admin_password if admin_password else password


def update_switch_credentials(new_username, new_password):
    set_key(ENV_FILE, 'SWITCH_USERNAME', new_username)
    set_key(ENV_FILE, 'SWITCH_PASSWORD', new_password)
    reload_switch_credentials()

def device_suffix(platform_text):
    normalized = (platform_text or "").lower()
    for token, label in (
        ("mikrotik", "MikroTik"),
        ("litebeam", "LiteBeam"),
        ("liteap", "LiteAP"),
        ("rocket", "Rocket"),
        ("cisco", "Cisco"),
    ):
        if token in normalized:
            return label
    return "Unknown"


def sortkey(port_data):
    match = re.match(r"([A-Za-z]+)(\d+)/(\d+)", port_data["port"])
    if not match:
        return (9, 999, 999)
    family_order = {"Fa": 0, "Gi": 1, "Te": 2, "Fo": 3}.get(match.group(1), 9)
    return (family_order, int(match.group(2)), int(match.group(3)))


def normalize_port(port):
    port = (port or "").replace(" ", "")
    aliases = (
        ("FastEthernet", "Fa"),
        ("GigabitEthernet", "Gi"),
        ("TenGigabitEthernet", "Te"),
        ("FortyGigabitEthernet", "Fo"),
    )
    for long_name, short_name in aliases:
        if port.lower().startswith(long_name.lower()):
            return short_name + port[len(long_name):]
    return port


def parse_descriptions(config_output):
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
        neighbors[normalize_port(interface_match.group(1))] = (
            device_match.group(1).strip(),
            device_suffix(platform),
        )

    return neighbors


def parse_interface_status(status_output):
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

        ports.append(
            {
                "port": values["Port"],
                "status": values["Status"] or "Unknown",
                "vlan": values["Vlan"] or "-",
                "speed": values["Speed"] or "-",
            }
        )

    return ports


def parse_transceiver_power(output):
    fiber_power = {}
    fiber_ports = set()

    for line in output.splitlines():
        stripped = line.strip()

        port_match = re.match(r"^((?:Gi|Te|Fo)\S+)", stripped)
        if port_match:
            fiber_ports.add(port_match.group(1))

        power_match = re.match(r"^(?:Gi|Te|Fo)\S+\s+\S+\s+\S+\s+([-\d\.]+)\s+([-\d\.]+)", stripped)
        if power_match and port_match:
            fiber_power[port_match.group(1)] = {
                "tx_dbm": power_match.group(1),
                "rx_dbm": power_match.group(2),
            }

    return fiber_power, fiber_ports


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        host = request.form.get("host", "").strip()
        return monitor_switch(host)
    return render_template("index.html")


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    change_error = None
    change_success = None
    admin_verified = session.get('admin_verified', False)

    if request.method == "POST":
        action = request.form.get('action', '').strip()

        if action == 'verify-admin':
            entered_admin_password = request.form.get('admin_password', '')
            if entered_admin_password == get_admin_password():
                session['admin_verified'] = True
                admin_verified = True
            else:
                change_error = 'Incorrect admin password.'

        elif action == 'save-credentials':
            if not admin_verified:
                change_error = 'Admin verification is required before changing credentials.'
            else:
                new_username = request.form.get('new_username', '').strip()
                new_password = request.form.get('new_password', '')

                if not new_username or not new_password:
                    change_error = 'Both new user and new password are required.'
                else:
                    update_switch_credentials(new_username, new_password)
                    session.pop('admin_verified', None)
                    admin_verified = False
                    change_success = 'Cisco username and password were updated in .env.'

    return render_template(
        'change_password.html',
        admin_verified=admin_verified,
        change_error=change_error,
        change_success=change_success,
        current_username=username,
    )

def monitor_switch(host):
    ports = []
    port_other_text = []
    fiber_other_text = []
    hostname = "Unknown"
    model = "Unknown"
    uptime = "Unknown"
    error = None
    nc = None

    if not username or not password:
        error = "⛔ Missing SWITCH_USERNAME or SWITCH_PASSWORD in .env"
    elif not host:
        error = "⛔ Please provide a switch IP address."

    if not error:
        try:
            from netmiko import ConnectHandler

            device = {
                "device_type": "cisco_ios_telnet",
                "host": host,
                "username": username,
                "password": password,
                "fast_cli": False,
            }

            nc = ConnectHandler(**device)

            hostname_output = nc.send_command("show running-config | include hostname")
            hostname_match = re.search(r"^hostname\s+(\S+)", hostname_output, re.MULTILINE | re.IGNORECASE)
            hostname = hostname_match.group(1) if hostname_match else host

            version_output = nc.send_command("show version")
            model_match = re.search(r"\b(WS-[A-Za-z0-9-]+)\b", version_output)
            model = model_match.group(1) if model_match else "Unknown"

            uptime_match = re.search(r"\b(uptime\s+is\s+.+)$", version_output, re.MULTILINE | re.IGNORECASE)
            uptime = uptime_match.group(1).strip() if uptime_match else "Unknown"

            transceiver_output = nc.send_command("show interfaces transceiver")
            fiber_power, fiber_ports = parse_transceiver_power(transceiver_output)

            status_output = nc.send_command("show interfaces status")
            parsed_ports = parse_interface_status(status_output)

            config_output = nc.send_command("show running-config | include ^interface|^ description")
            descriptions = parse_descriptions(config_output)

            cdp_output = nc.send_command("show cdp neighbors detail")
            cdp_neighbors = parse_cdp_details(cdp_output)

            for parsed_port in parsed_ports:
                port_name = parsed_port["port"]
                status = parsed_port["status"]
                vlan = parsed_port["vlan"]
                speed = parsed_port["speed"]

                display_name = "-"
                device_type = "-"

                if status.lower() == "connected":
                    normalized_port = normalize_port(port_name)
                    description = descriptions.get(normalized_port, "")

                    neighbor_name, neighbor_type = cdp_neighbors.get(normalized_port, ("", ""))
                    if neighbor_name:
                        display_name, device_type = neighbor_name, neighbor_type
                    elif description:
                        display_name = description
                        device_type = "Fiber" if port_name in fiber_ports else "Description"

                tx_dbm = fiber_power.get(port_name, {}).get("tx_dbm", "-")
                rx_dbm = fiber_power.get(port_name, {}).get("rx_dbm", "-")

                ports.append(
                    {
                        "port": port_name,
                        "status": status,
                        "description": display_name,
                        "type": device_type,
                        "vlan": vlan,
                        "speed": speed,
                        "tx_dbm": tx_dbm,
                        "rx_dbm": rx_dbm,
                        "is_connected": status.lower() == "connected",
                        "is_fiber": port_name in fiber_ports,
                    }
                )

            ports.sort(key=sortkey)

        except ImportError:
            error = "⛔ Netmiko is not installed in this environment. Install with: pip install netmiko"
        except Exception as e:
            error = f"⛔ Error: {e}"
        finally:
            if nc is not None:
                nc.disconnect()

    connected_ports = [port for port in ports if port['is_connected']]
    disconnected_ports = [port for port in ports if not port['is_connected']]
    fiber_count = sum(1 for port in ports if port.get('is_fiber'))
    port_summary = {
        'total_ports': len(ports),
        'connected_ports': len(connected_ports),
        'fiber_ports': fiber_count,
    }

    return render_template("results.html",
                         host=host,
                         hostname=hostname,
                         model=model,
                         uptime=uptime,
                         ports=ports,
                         port_summary=port_summary,
                         connected_ports=connected_ports,
                         disconnected_ports=disconnected_ports,
                         port_other_text=port_other_text,
                         fiber_other_text=fiber_other_text,
                         error=error)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3000, debug=True)
