# ClientTracker

Real-time wireless client roaming tracker for **Cisco Catalyst 9800** Wireless LAN Controllers and Cisco APs.

The script maintains a persistent SSH session to the WLC, polls client association data, opens on-demand SSH sessions to the connected AP to pull live RSSI/MCS/channel stats, and displays everything in a live-updating terminal UI. When the client roams to a new AP, the event is logged with the last-known signal metrics.

## Requirements

- Python 3.10+
- Network SSH access to the WLC and its APs
- Enable-level access on the APs

Install dependencies:

```
pip install -r requirements.txt
```

## Configuration

Edit `config.yaml` in the project root with your WLC and AP credentials:

```yaml
wlc:
  host: "192.168.2.8"
  username: "admin"
  password: "changeme"

ap:
  username: "admin"
  password: "changeme"
  enable: "changeme"
```


## Usage

```
python client_tracker.py <mac-address>
```

The MAC address can be supplied in any common format — all delimiters (`:`, `-`, `.`) are accepted:

```
python client_tracker.py aa:bb:cc:dd:ee:ff
python client_tracker.py aa-bb-cc-dd-ee-ff
python client_tracker.py aabb.ccdd.eeff
python client_tracker.py aa.bb.cc.dd.ee.ff
python client_tracker.py aabbccddeeff
```

The script will:

1. SSH to the WLC and begin polling the client's association state.
2. SSH to the currently connected AP and pull live RSSI, MCS rate, and channel.
3. If the client roams, close the old AP session, open one to the new AP, and record the event.
4. Display all data in a continuously refreshing terminal UI.

Press **Ctrl+C** to stop.

## Example Output

```
Connected to MyWLC. Tracking client 3c6d.6606.0907...

╭────────────────────── WLC Client Stats ──────────────────────╮
│  WLC:      MyWLC                                             │
│  Client:   3c6d.6606.0907                                    │
│  AP Name:  AP-9166-1             AP IP: 10.1.2.3             │
│  SSID:     DevNet                Protocol: 802.11ax - 5 GHz  │
│  RSSI:     -42 dBm               SNR: 38 dB                  │
│  State:    Run                   Updated: 14:23:05           │
╰──────────────────────────────────────────────────────────────╯
╭──────────── AP Client Stats (AP-9166-1) ─────────────────────╮
│  Live RSSI: -45 dBm    Rate: MCS112SS    Ch: 34              │
│  Updated: 14:23:05                                           │
╰──────────────────────────────────────────────────────────────╯
╭──────────────────── Roaming History ─────────────────────────╮
│  14:20:12  AP-9166-2 -> AP-9166-1  -51 dBm  MCS92SS  Ch 40   │
│  14:15:44  AP-9166-3 -> AP-9166-2  -63 dBm  MCS72SS  Ch 36   │
╰──────────────────────────────────────────────────────────────╯
Ctrl+C to quit
```

## Project Structure

```
ClientTracker/
├── client_tracker.py    # Main script
├── config.yaml          # WLC and AP credentials (not tracked in git)
├── requirements.txt     # Python dependencies
└── README.md
```
